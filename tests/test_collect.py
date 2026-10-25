import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import collect  # noqa: E402
import verify  # noqa: E402

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def slots(plan):
    return [collect.slot_str(s) for s, _ in plan]


class SlotPlanTests(unittest.TestCase):
    def test_minute_50_waits_for_next_hour_only(self):
        plan = collect.plan_slots(dt("2026-10-24T15:50:10Z"))
        self.assertEqual(slots(plan), ["2026-10-24T16:00:00Z"])
        self.assertAlmostEqual(plan[0][1], 590, delta=1)

    def test_minute_29_is_current_hour_no_wait(self):
        self.assertEqual(collect.plan_slots(dt("2026-10-24T15:29:59Z")), [(dt("2026-10-24T15:00:00Z"), 0.0)])

    def test_minute_30_to_45_fills_current_then_waits_for_next(self):
        plan = collect.plan_slots(dt("2026-10-24T15:35:00Z"))
        self.assertEqual(slots(plan), ["2026-10-24T15:00:00Z", "2026-10-24T16:00:00Z"])
        self.assertEqual(plan[1][1], 1500)  # full wait, no cap that would poll early

    def test_minute_46_next_hour_only(self):
        self.assertEqual(slots(collect.plan_slots(dt("2026-10-24T15:46:00Z"))), ["2026-10-24T16:00:00Z"])

    def test_current_only_for_manual_runs(self):
        self.assertEqual(slots(collect.plan_slots(dt("2026-10-24T15:40:00Z"), current_only=True)),
                         ["2026-10-24T15:00:00Z"])
        self.assertEqual(collect.plan_slots(dt("2026-10-24T15:50:00Z"), current_only=True), [(dt("2026-10-24T15:00:00Z"), 0.0)])

    def test_backup_run_minute_05(self):
        self.assertEqual(collect.plan_slots(dt("2026-10-24T16:05:00Z")), [(dt("2026-10-24T16:00:00Z"), 0.0)])

    def test_midnight_and_year_rollover(self):
        self.assertEqual(slots(collect.plan_slots(dt("2026-12-31T23:55:00Z"))), ["2027-01-01T00:00:00Z"])

    def test_dst_fall_back_slots_are_utc_hourly(self):
        # 2026-10-25 02:00 BST -> 01:00 GMT. UTC slots must stay strictly hourly.
        s1 = collect.plan_slots(dt("2026-10-25T00:50:00Z"))[-1][0]
        s2 = collect.plan_slots(dt("2026-10-25T01:50:00Z"))[-1][0]
        self.assertEqual((s1, s2), (dt("2026-10-25T01:00:00Z"), dt("2026-10-25T02:00:00Z")))

    def test_dst_spring_forward(self):
        self.assertEqual(collect.plan_slots(dt("2026-03-29T00:50:00Z"))[-1][0], dt("2026-03-29T01:00:00Z"))

    def test_closure_flag_london_time(self):
        self.assertTrue(collect.is_closure_window(dt("2026-10-24T01:00:00Z")))    # 02:00 BST
        self.assertFalse(collect.is_closure_window(dt("2026-10-24T15:00:00Z")))
        self.assertTrue(collect.is_closure_window(dt("2026-12-10T00:00:00Z")))    # 00:00 GMT
        self.assertFalse(collect.is_closure_window(dt("2026-12-10T05:00:00Z")))
        self.assertTrue(collect.is_closure_window(dt("2026-10-25T00:00:00Z")))
        self.assertTrue(collect.is_closure_window(dt("2026-10-25T01:00:00Z")))
        self.assertFalse(collect.is_closure_window(dt("2026-10-25T05:00:00Z")))


class RegistryTests(unittest.TestCase):
    def test_derivation_counts(self):
        reg = collect.load_registry(ROOT / "config" / "registry.json")
        d = collect.derive(reg)
        self.assertEqual(len(d["disruptionIds"]), 98)
        self.assertEqual(d["stationsPolled"], 75)
        self.assertEqual(len(d["crowding"]), 33)
        self.assertEqual(len(d["arrivals"]), 33)
        self.assertEqual(dict(d["arrivals"])["liverpool-street"], ["940GZZLULVT", "910GLIVST", "910GLIVSTLL"])
        self.assertEqual(len(reg["lines"]), 14)
        self.assertEqual(len(d["crowdingNetworkIds"]), 315)
        self.assertIn("national-rail", d["statusModes"][0])
        self.assertEqual(len(set(d["disruptionIds"])), 98)


class StorageTests(unittest.TestCase):
    def test_append_and_idempotent_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = dt("2026-10-24T16:00:00Z")
            self.assertFalse(collect.slot_present(tmp, "liftDisruptions", slot))
            rec = collect.make_record(slot, dt("2026-10-24T16:00:03Z"), "anonymous-no-key",
                                      {"source": "x", "count": 0, "disruptions": []})
            p = collect.ndjson_path(tmp, "liftDisruptions", slot)
            collect.append_line(p, rec)
            self.assertTrue(collect.slot_present(tmp, "liftDisruptions", slot))
            self.assertFalse(collect.slot_present(tmp, "liftDisruptions", dt("2026-10-24T17:00:00Z")))
            collect.append_line(p, {**rec, "slot": "2026-10-24T17:00:00Z"})
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["slot"], "2026-10-24T16:00:00Z")
            self.assertNotIn(" ", lines[0].split('"source"')[0])  # compact separators

    def test_corrupt_line_ignored_by_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = dt("2026-10-24T16:00:00Z")
            p = collect.ndjson_path(tmp, "lineStatus", slot)
            p.parent.mkdir(parents=True)
            p.write_text("{not json\n")
            self.assertFalse(collect.slot_present(tmp, "lineStatus", slot))


class GzipStorageTests(unittest.TestCase):
    def test_hourly_gzip_members_read_as_one_stream_and_presence(self):
        import gzip
        with tempfile.TemporaryDirectory() as tmp:
            for h in (16, 17):
                slot = dt(f"2026-10-24T{h}:00:00Z")
                collect.append_line(collect.ndjson_path(tmp, "arrivalsDetail", slot), {"slot": collect.slot_str(slot), "polledAt": "x"})
            p = Path(tmp) / "arrivalsDetail" / "2026-10-24.ndjson.gz"
            self.assertEqual([json.loads(l)["slot"] for l in gzip.open(p, "rt")], ["2026-10-24T16:00:00Z", "2026-10-24T17:00:00Z"])
            self.assertTrue(collect.slot_present(tmp, "arrivalsDetail", dt("2026-10-24T17:00:00Z")))
            self.assertFalse(collect.slot_present(tmp, "arrivalsDetail", dt("2026-10-24T18:00:00Z")))
            res, corrupt = verify.check(tmp, 4, dt("2026-10-24T17:30:00Z"))
            self.assertEqual((res["arrivalsDetail"]["missing"], corrupt), ([], []))


class KeyRedactionTests(unittest.TestCase):
    def test_redact(self):
        s = collect.redact("https://api.tfl.gov.uk/x?foo=1&app_key=SECRET123&b=2", "SECRET123")
        self.assertNotIn("SECRET123", s)
        self.assertIn("app_key=***", s)
        self.assertNotIn("SECRET123", collect.redact("boom SECRET123 boom", "SECRET123"))

    def test_key_added_to_url_but_not_logged(self):
        seen = []

        def fetch(url, timeout):
            seen.append(url)
            return 200, b"[]", {}
        c = collect.Client("SECRET123", fetch=fetch, sleep=lambda s: None)
        c.get("/Disruptions/Lifts/v2/")
        self.assertIn("app_key=SECRET123", seen[0])
        c2 = collect.Client("K", fetch=fetch, sleep=lambda s: None)
        c2.get("/StopPoint/A,B/Disruption?x=1")
        self.assertIn("?x=1&app_key=K", seen[1])


class NationalRailTests(unittest.TestCase):
    BOARD = {"generatedAt": "2026-10-24T08:00:02", "locationName": "Charlton", "crs": "CTN", "platformsAreHidden": False,
             "servicesAreUnavailable": False,
             "trainServices": [{"rid": "R1", "uid": "U1", "operatorCode": "SE", "std": "2026-10-24T08:03:00",
                                "etd": "2026-10-24T08:07:00", "platform": "1", "isCancelled": False,
                                "delayReason": {"Value": 104, "tiploc": "LEWISHM"},
                                "destination": [{"locationName": "London Cannon Street", "crs": "CST"}],
                                "origin": [{"locationName": "Dartford", "crs": "DFD"}], "length": 8,
                                "subsequentLocations": [{"crs": "CST"}],
                                "formation": {"serviceLoading": {"loadingPercentage": {"type": "Typical", "Value": 37}},
                                              "coaches": [{"number": "A1", "loading": {"source": "CIS", "Value": 65}},
                                                          {"number": "A2", "loading": {"Value": 20}}, {"number": "A3"}]}}],
             "nrccMessages": ["Disruption at Lewisham & Hither Green.", {"$type": "x", "Value": "Lifts out of order"}]}

    def run_nr(self, tmp, nr_fetch, key="NRKEY-SECRET-1"):
        import argparse
        os.environ["NR_API_KEY"] = key
        try:
            a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now="2026-10-24T07:50:00Z",
                                   no_sleep=True, datasets="nationalRailDepartures", dry_run=False, current_only=False)
            return collect.run(a, fetch=lambda u, t: 1 / 0, sleep=lambda s: None, clock=lambda: dt("2026-10-24T08:00:01Z"),
                               nr_fetch=nr_fetch)
        finally:
            os.environ.pop("NR_API_KEY", None)

    def test_boards_compacted_key_only_in_header(self):
        calls = []

        def nr_fetch(url, key, timeout):
            calls.append((url, key))
            return 200, json.dumps(self.BOARD).encode()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_nr(tmp, nr_fetch), 0)
            self.assertEqual(len(calls), len(json.load(open(ROOT / "config" / "registry.json"))["nationalRailStations"]))
            url, key = calls[0]
            # full hour of departures from the London local time of the slot (GetDepBoardWithDetails caps at 10)
            self.assertTrue(url.endswith("/GetDepartureBoardByCRS/CTN/20261024T090000?numRows=150&timeWindow=60"), url)   # 08:00 UTC = 09:00 BST
            self.assertNotIn("NRKEY", url)
            self.assertEqual(key, "NRKEY-SECRET-1")
            rec = RunTests.lines(None, tmp, "nationalRailDepartures")[0]
            ctn = rec["stations"]["CTN"]
            row = dict(zip(rec["fields"], ctn["services"][0]))
            self.assertEqual((row["std"], row["etd"], row["platform"], row["delayReason"], row["destination"], row["origin"]),
                             ("2026-10-24T08:03:00", "2026-10-24T08:07:00", "1", 104, ["CST"], ["DFD"]))
            self.assertEqual(ctn["nrccMessages"], ["Disruption at Lewisham & Hither Green.", {"Value": "Lifts out of order"}])
            self.assertEqual((row["loadingPct"], row["coachLoading"]), (37, [["A1", 65], ["A2", 20]]))   # train loading kept
            everything = json.dumps(rec) + (Path(tmp) / "_health" / "2026-10-24.ndjson").read_text()
            self.assertNotIn("NRKEY", everything)

    def test_rejected_key_is_a_failed_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def nr_fetch(url, key, timeout):
                calls.append(url)
                return 401, b"Unauthorized"
            self.assertEqual(self.run_nr(tmp, nr_fetch), 2)
            self.assertEqual(len(calls), 2 * len(json.load(open(ROOT / "config" / "registry.json"))["nationalRailStations"]))   # 401 not retried within a pass; one more pass 45 s later
            h = json.loads((Path(tmp) / "_health" / "2026-10-24.ndjson").read_text())
            self.assertEqual(h["datasets"]["nationalRailDepartures"]["status"], "failed")


class NationalRailIncidentsTests(unittest.TestCase):
    XML = (b'<?xml version="1.0"?><Incidents xmlns="http://nationalrail.co.uk/xml/incident">'
           b'<PtIncident><IncidentNumber>A1</IncidentNumber><Summary>Strike on Southeastern</Summary><Planned>true</Planned>'
           b'<ValidityPeriod><StartTime>2026-10-25T00:00:00Z</StartTime><EndTime>2026-10-26T00:00:00Z</EndTime></ValidityPeriod>'
           b'<Affects><Operators><AffectedOperator><OperatorRef>SE</OperatorRef></AffectedOperator></Operators></Affects></PtIncident>'
           b'<PtIncident><IncidentNumber>B2</IncidentNumber><Summary>Signal failure at Lewisham</Summary><Planned>false</Planned></PtIncident>'
           b'</Incidents>')

    def go(self, tmp, now, body, key="KBKEY-SECRET-9"):
        import argparse
        seen = []
        def nr_fetch(url, k, timeout, accept="application/json"):
            seen.append((url, k, accept))
            return 200, body
        os.environ["NR_KB_INCIDENTS_KEY"] = key
        try:
            a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now=now, no_sleep=True,
                                   datasets="nationalRailIncidents", dry_run=False, current_only=True)
            rc = collect.run(a, fetch=lambda u, t: 1 / 0, sleep=lambda s: None, clock=lambda: dt(now), nr_fetch=nr_fetch)
        finally:
            os.environ.pop("NR_KB_INCIDENTS_KEY", None)
        return rc, seen

    def test_every_hour_has_index_and_only_new_or_changed_incidents_stored(self):
        import gzip as gz
        X = self.XML.replace(b"</IncidentNumber>", b"</IncidentNumber><Version>1</Version>")
        with tempfile.TemporaryDirectory() as tmp:
            rc, seen = self.go(tmp, "2026-10-24T08:00:05Z", gz.compress(X))     # the live gateway gzips the body
            self.assertEqual(rc, 0)
            self.assertTrue(seen[0][0].endswith("/incidents.xml"))
            self.assertEqual((seen[0][1], seen[0][2]), ("KBKEY-SECRET-9", "application/xml"))
            self.go(tmp, "2026-10-24T09:00:05Z", X)                                # same feed, plain body
            changed = X.replace(b"<IncidentNumber>B2</IncidentNumber><Version>1<", b"<IncidentNumber>B2</IncidentNumber><Version>2<")
            self.go(tmp, "2026-10-24T10:00:05Z", changed)                          # B2 revised
            recs = RunTests.lines(None, tmp, "nationalRailIncidents")
            self.assertEqual([r["slot"][11:13] for r in recs], ["08", "09", "10"])  # a record every hour
            self.assertEqual([r["count"] for r in recs], [2, 2, 2])
            self.assertEqual([[x["id"] for x in r["newOrChanged"]] for r in recs], [["A1", "B2"], [], ["B2"]])
            self.assertIn("Strike on Southeastern", recs[0]["newOrChanged"][0]["xml"])
            self.assertEqual(recs[2]["newOrChanged"][0]["version"], "2")
            a1 = recs[0]["active"][0]
            self.assertEqual(dict(zip(recs[0]["activeFields"], a1))["operators"], ["SE"])
            self.assertEqual(dict(zip(recs[0]["activeFields"], a1))["start"], "2026-10-25T00:00:00Z")
            self.assertEqual((recs[0]["planned"], recs[0]["byOperator"]), (1, {"SE": 1}))
            blob = "".join(json.dumps(r) for r in recs) + (Path(tmp) / "_health" / "2026-10-24.ndjson").read_text()
            self.assertNotIn("KBKEY", blob)

    def test_unparseable_feed_is_kept_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _ = self.go(tmp, "2026-10-24T08:00:05Z", b"<not xml")
            rec = RunTests.lines(None, tmp, "nationalRailIncidents")[0]
            self.assertEqual((rec["xml"], rec["parseError"]), ("<not xml", "ParseError"))
            self.assertEqual(rc, 0)   # data stored; health line says why it is partial
            h = json.loads((Path(tmp) / "_health" / "2026-10-24.ndjson").read_text())
            self.assertEqual(h["datasets"]["nationalRailIncidents"]["status"], "partial")

    def test_no_key_means_not_collected_and_no_gap(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now="2026-10-24T08:00:05Z",
                                   no_sleep=True, datasets="nationalRailIncidents", dry_run=False, current_only=True)
            self.assertEqual(collect.run(a, fetch=lambda u, t: 1 / 0, sleep=lambda s: None, clock=lambda: dt("2026-10-24T08:00:05Z")), 0)
            self.assertFalse((Path(tmp) / "nationalRailIncidents").exists())


class LineStatusNationalRailMessageTests(unittest.TestCase):
    KB = (b'<?xml version="1.0"?><Incidents xmlns="http://nationalrail.co.uk/xml/incident">'
          b'<PtIncident><IncidentNumber>A1</IncidentNumber><Version>7</Version><Summary>Buses replace trains between Brighton and Haywards Heath</Summary>'
          b'<Description>&lt;p&gt;Engineering work is &lt;a href="x"&gt;closing&lt;/a&gt; all lines.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Allow extra time&lt;/li&gt;&lt;/ul&gt;</Description>'
          b'<InfoLinks><InfoLink><Uri>https://www.nationalrail.co.uk/service-disruptions/wivelsfield-20261025/</Uri></InfoLink></InfoLinks>'
          b'<Affects><RoutesAffected>&lt;p&gt;Between Brighton and Haywards Heath&lt;/p&gt;</RoutesAffected></Affects></PtIncident>'
          b'</Incidents>')

    def fetch(self, url, timeout):
        if "/Line/Mode/" in url:
            return 200, json.dumps([
                {"id": "southern", "name": "Southern", "modeName": "national-rail", "lineStatuses": [
                    {"statusSeverity": 0, "statusSeverityDescription": "Special Service",
                     "reason": "https://www.nationalrail.co.uk/service-disruptions/wivelsfield-20261025/"}]},
                {"id": "c2c", "name": "c2c", "modeName": "national-rail", "lineStatuses": [
                    {"statusSeverity": 0, "statusSeverityDescription": "Special Service", "reason": "https://www.nationalrail.co.uk/unknown/"}]},
                {"id": "circle", "name": "Circle", "modeName": "tube", "lineStatuses": [
                    {"statusSeverity": 5, "statusSeverityDescription": "Part Closure", "reason": "CIRCLE LINE: no service"}]}]).encode(), {}
        return 404, b"", {}

    def run_with(self, tmp, datasets, kb_key="KBKEY-SECRET-9"):
        import argparse, gzip as gz
        calls = []
        def nr_fetch(url, k, timeout, accept="application/json"):
            calls.append(url)
            return 200, gz.compress(self.KB)
        if kb_key:
            os.environ["NR_KB_INCIDENTS_KEY"] = kb_key
        try:
            a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now="2026-10-25T08:00:05Z", no_sleep=True,
                                   datasets=datasets, dry_run=False, current_only=True)
            rc = collect.run(a, fetch=self.fetch, sleep=lambda s: None, clock=lambda: dt("2026-10-25T08:00:05Z"), nr_fetch=nr_fetch)
        finally:
            os.environ.pop("NR_KB_INCIDENTS_KEY", None)
        return rc, calls

    def test_national_rail_message_stored_with_line_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls = self.run_with(tmp, "lineStatus,nationalRailIncidents")
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)    # one KB request serves both datasets
            rec = RunTests.lines(None, tmp, "lineStatus", "2026-10-25")[0]
            L = {l["id"]: l for l in rec["lines"]}
            st = L["southern"]["statuses"][0]
            self.assertEqual(st["reason"], "https://www.nationalrail.co.uk/service-disruptions/wivelsfield-20261025/")  # TfL's text kept as-is
            m = st["nrIncident"]
            self.assertEqual((m["id"], m["version"]), ("A1", "7"))
            self.assertEqual(m["summary"], "Buses replace trains between Brighton and Haywards Heath")
            self.assertEqual(m["routes"], "Between Brighton and Haywards Heath")
            self.assertEqual(m["message"], "Engineering work is closing all lines.\n\u2022 Allow extra time")
            self.assertNotIn("nrIncident", L["c2c"]["statuses"][0])     # no match: nothing invented
            self.assertNotIn("nrIncident", L["circle"]["statuses"][0])  # TfL lines already carry the full message
            self.assertEqual(L["circle"]["statuses"][0]["reason"], "CIRCLE LINE: no service")
            self.assertEqual(len(RunTests.lines(None, tmp, "nationalRailIncidents", "2026-10-25")), 1)
            self.assertNotIn("KBKEY", json.dumps(rec))

    def test_line_status_alone_still_resolves_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls = self.run_with(tmp, "lineStatus")
            rec = RunTests.lines(None, tmp, "lineStatus", "2026-10-25")[0]
            self.assertEqual(rec["lines"][0]["statuses"][0]["nrIncident"]["id"], "A1")
            self.assertEqual(len(calls), 1)

    def test_no_kb_key_line_status_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls = self.run_with(tmp, "lineStatus", kb_key=None)
            self.assertEqual((rc, calls), (0, []))
            rec = RunTests.lines(None, tmp, "lineStatus", "2026-10-25")[0]
            self.assertNotIn("nrIncident", rec["lines"][0]["statuses"][0])


class NationalRailStationsTests(unittest.TestCase):
    def feed(self, ctn_lifts="ok"):
        return json.dumps({"stations": [
            {"crsCode": "CTN", "name": "Charlton", "lifts": ctn_lifts},
            {"crsCode": "LBG", "name": "London Bridge", "lifts": "ok"}]}).encode()

    def go(self, tmp, now, body, key="KBST-SECRET-7"):
        import argparse
        seen = []
        def nr_fetch(url, k, timeout, accept="application/json"):
            seen.append((url, k))
            return 200, body
        os.environ["NR_KB_STATIONS_KEY"] = key
        try:
            a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now=now, no_sleep=True,
                                   datasets="nationalRailStations", dry_run=False, current_only=True)
            rc = collect.run(a, fetch=lambda u, t: 1 / 0, sleep=lambda s: None, clock=lambda: dt(now), nr_fetch=nr_fetch)
        finally:
            os.environ.pop("NR_KB_STATIONS_KEY", None)
        return rc, seen

    def test_once_a_day_from_03utc_full_first_then_changes_only(self):
        import gzip as gz
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.go(tmp, "2026-10-24T02:00:05Z", self.feed())[1], [])     # before 03:00: not due
            rc, seen = self.go(tmp, "2026-10-24T03:00:05Z", gz.compress(self.feed()))
            self.assertEqual(rc, 0)
            self.assertTrue(seen[0][0].endswith("/stations"))
            self.assertEqual(self.go(tmp, "2026-10-24T04:00:05Z", self.feed())[1], [])     # already done today
            self.go(tmp, "2026-10-25T05:00:05Z", self.feed())                              # missed 03:00: caught up
            self.go(tmp, "2026-10-26T03:00:05Z", self.feed(ctn_lifts="out of service"))
            recs = [json.loads(l) for f in sorted((Path(tmp) / "nationalRailStations").iterdir())
                    for l in gz.open(f, "rt") if l.strip()]
            self.assertEqual([r["slot"] for r in recs], ["2026-10-24T03:00:00Z", "2026-10-25T05:00:00Z", "2026-10-26T03:00:00Z"])
            self.assertEqual([[x["crsCode"] for x in r["changed"]] for r in recs], [["CTN", "LBG"], [], ["CTN"]])
            self.assertEqual(sorted(recs[1]["index"]), ["CTN", "LBG"])
            self.assertEqual(recs[2]["changed"][0]["lifts"], "out of service")
            blob = "".join(json.dumps(r) for r in recs) + "".join(
                f.read_text() for f in (Path(tmp) / "_health").iterdir())
            self.assertNotIn("KBST", blob)


class StationAccessTests(unittest.TestCase):
    def test_zip_kept_once_per_version_and_daily_record(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            def go(now):
                a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now=now, no_sleep=True,
                                       datasets="stationAccess", dry_run=False, current_only=True)
                return collect.run(a, fetch=lambda u, t: (200, RunTests.station_zip(), {}), sleep=lambda s: None, clock=lambda: dt(now))
            self.assertEqual(go("2026-10-24T03:00:05Z"), 0)
            go("2026-10-24T04:00:05Z")                      # same day: not fetched again
            go("2026-10-25T03:00:05Z")                      # next day, same content: record, no new file
            recs = [json.loads(l) for f in sorted((Path(tmp) / "stationAccess").iterdir()) for l in f.read_text().splitlines()]
            self.assertEqual([(r["slot"][:13], r["changed"], r["lifts"]) for r in recs],
                             [("2026-10-24T03", True, 2), ("2026-10-25T03", False, 2)])
            self.assertEqual(len(list((Path(tmp) / "_reference" / "tfl-stationdata").iterdir())), 1)


class PlannedLineStatusTests(unittest.TestCase):
    """plannedLineStatus: once a day from 03:00 UTC, every line's status for today and the next 7 days
    (TfL has no mode-level date-range call, so ids come from /Line/Mode/{m} and go to
    /Line/{ids}/Status/{from}/to/{to} in URL-safe chunks: a long id list is refused with HTTP 400)."""
    OPERATORS = [f"operator-number-{i:02d}" for i in range(30)]           # ~450 characters of ids: needs several chunks
    MODE_LINES = {"tube": ["bakerloo", "central"], "dlr": ["dlr"], "national-rail": OPERATORS, "river-bus": ["woolwich-ferry"]}

    def make_fetch(self, calls, fail_id=None):
        import re
        from urllib.parse import urlparse

        def fetch(url, timeout):
            path = urlparse(url).path
            calls.append(path)
            m = re.match(r"^/Line/Mode/([^/]+)$", path)
            if m:
                lines = [{"id": i, "name": i.title(), "modeName": mode} for mode in m.group(1).split(",") for i in self.MODE_LINES.get(mode, [])]
                return 200, json.dumps(lines).encode(), {}
            m = re.match(r"^/Line/([^/]+)/Status/(\d{4}-\d{2}-\d{2})/to/(\d{4}-\d{2}-\d{2})$", path)
            if m:
                ids = m.group(1).split(",")
                if fail_id and fail_id in ids:
                    return 404, b"{}", {}
                out = []
                for i in ids:
                    st = [{"statusSeverity": 10, "statusSeverityDescription": "Good Service", "reason": None, "validityPeriods": []}]
                    if i == "central":
                        st.append({"statusSeverity": 3, "statusSeverityDescription": "Part Closure", "reason": "Central line: no service Epping to Leytonstone.",
                                   "validityPeriods": [{"fromDate": "2026-10-31T00:30:00Z", "toDate": "2026-11-01T04:00:00Z", "isNow": False}],
                                   "disruption": {"category": "PlannedWork", "categoryDescription": "PlannedWork", "created": "2026-10-20T10:00:00Z",
                                                  "additionalInfo": "", "affectedStops": [{"naptanId": "940GZZLULYN"}]}})
                    out.append({"id": i, "name": i.title(), "modeName": "tube", "lineStatuses": st})
                return 200, json.dumps(out).encode(), {}
            return 404, b"{}", {}
        return fetch

    def go(self, tmp, now, calls, **kw):
        import argparse
        a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now=now, no_sleep=True,
                               datasets="plannedLineStatus", dry_run=False, current_only=True)
        return collect.run(a, fetch=self.make_fetch(calls, **kw), sleep=lambda s: None, clock=lambda: dt(now))

    @staticmethod
    def records(tmp):
        import gzip
        return [json.loads(l) for f in sorted((Path(tmp) / "plannedLineStatus").iterdir()) for l in gzip.open(f, "rt") if l.strip()]

    def test_week_ahead_in_url_safe_chunks_once_a_day_from_03_utc(self):
        import re
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            self.assertEqual(self.go(tmp, "2026-10-25T02:00:05Z", calls), 0)       # before 03:00 UTC: not due yet
            self.assertEqual((calls, (Path(tmp) / "plannedLineStatus").exists()), ([], False))
            self.assertEqual(self.go(tmp, "2026-10-25T05:00:05Z", calls), 0)
            status_calls = [c for c in calls if "/Status/" in c]
            self.assertGreaterEqual(len(status_calls), 3)
            asked = []
            for c in status_calls:
                ids = re.match(r"^/Line/([^/]+)/Status/(.+)$", c).group(1)
                self.assertLessEqual(len(ids), 200)
                asked += ids.split(",")
                self.assertTrue(c.endswith("/Status/2026-10-25/to/2026-11-01"), c)   # today (London) to today + 7
            self.assertEqual(sorted(asked), sorted(i for v in self.MODE_LINES.values() for i in v))   # each line exactly once
            (rec,) = self.records(tmp)
            self.assertEqual((rec["slot"], rec["from"], rec["to"]), ("2026-10-25T05:00:00Z", "2026-10-25", "2026-11-01"))
            self.assertEqual(len(rec["lines"]), 34)
            central = [l for l in rec["lines"] if l["id"] == "central"][0]
            planned = [s for s in central["statuses"] if s["severity"] == 3][0]
            self.assertEqual(planned["validity"], [["2026-10-31T00:30:00Z", "2026-11-01T04:00:00Z"]])
            self.assertEqual((planned["disruption"]["category"], planned["disruption"]["affectedStops"]), ("PlannedWork", ["940GZZLULYN"]))
            n = len(calls)
            self.assertEqual(self.go(tmp, "2026-10-25T06:00:05Z", calls), 0)          # same UTC day: not fetched again
            self.assertEqual((len(calls), len(self.records(tmp))), (n, 1))

    def test_one_failed_chunk_is_partial_and_the_rest_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            self.go(tmp, "2026-10-25T05:00:05Z", calls, fail_id="operator-number-03")
            (rec,) = self.records(tmp)
            self.assertLess(len(rec["lines"]), 34)
            self.assertGreater(len(rec["lines"]), 0)
            health = [json.loads(l) for f in sorted((Path(tmp) / "_health").iterdir()) for l in f.read_text().splitlines()]
            self.assertEqual(health[-1]["datasets"]["plannedLineStatus"]["status"], "partial")


class ArrivalsCoverageTests(unittest.TestCase):
    def run_ds(self, tmp, ds, fetch):
        import argparse
        a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now="2026-10-24T10:00:05Z",
                               no_sleep=True, datasets=ds, dry_run=False, current_only=True)
        return collect.run(a, fetch=fetch, sleep=lambda s: None, clock=lambda: dt("2026-10-24T10:00:05Z"))

    def test_dlr_predictions_sharing_one_id_are_all_kept(self):
        dlr = [{"id": "-937089209", "lineId": "dlr", "naptanId": "940GZZDLCAN", "platformName": p, "destinationNaptanId": d,
                "timeToStation": t, "vehicleId": ""} for p, d, t in [("Platform 6", "BNK", 51), ("Platform 1", "LEW", 111),
                                                                      ("Platform 1", "LEW", 171), ("Platform 2", "LEW", 351)]]
        def fetch(url, t):
            if "940GZZDLCAN" in url:
                return 200, json.dumps(dlr + dlr[:1]).encode(), {}     # last one: same prediction polled twice
            return 200, b"[]", {}
        with tempfile.TemporaryDirectory() as tmp:
            self.run_ds(tmp, "arrivalsDetail", fetch)
            rec = RunTests.lines(None, tmp, "arrivalsDetail")[0]
            self.assertEqual(len(rec["stations"]["canary-wharf"]), 4)

    def test_network_arrivals_one_call_per_mode(self):
        seen = []
        def fetch(url, t):
            seen.append(url)
            m = url.split("/Mode/")[1].split("/")[0]
            return 200, json.dumps([{"lineId": m, "naptanId": "N1", "timeToStation": 60, "id": "x"},
                                    {"lineId": m, "naptanId": "N2", "timeToStation": 30, "id": "x"}]).encode(), {}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_ds(tmp, "arrivalsNetwork", fetch), 0)
            rec = RunTests.lines(None, tmp, "arrivalsNetwork")[0]
            self.assertEqual(sorted(rec["modes"]), sorted(["tube", "elizabeth-line", "overground", "dlr", "tram", "river-bus"]))
            self.assertTrue(all("?count=-1" in u for u in seen) and len(seen) == 6)
            rb = [dict(zip(rec["fields"], r)) for r in rec["modes"]["river-bus"]]
            self.assertEqual([r["naptanId"] for r in rb], ["N1", "N2"])


class InvalidKeyTests(unittest.TestCase):
    def test_invalid_key_dropped_and_run_continues_anonymously(self):
        import argparse
        seen = []

        def fetch(url, timeout):
            seen.append(url)
            if "app_key=" in url:
                return 429, b"Invalid app_key is provided.", {}
            return RunTests.fake_fetch(None, url, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TFL_APP_KEY"] = "BADKEY123"
            try:
                a = argparse.Namespace(out=tmp, registry=str(ROOT / "config" / "registry.json"), now="2026-10-24T15:50:00Z",
                                       no_sleep=True, datasets="liftDisruptions,lineStatus", dry_run=False, current_only=False)
                rc = collect.run(a, fetch=fetch, sleep=lambda s: None, clock=lambda: dt("2026-10-24T16:00:01Z"))
            finally:
                os.environ.pop("TFL_APP_KEY", None)
            self.assertEqual(rc, 3)
            self.assertEqual(sum("app_key=" in u for u in seen), 1)           # one rejected call, then anonymous
            for d in ("liftDisruptions", "lineStatus"):
                p = Path(tmp) / d / "2026-10-24.ndjson"
                txt = p.read_text()
                self.assertNotIn("BADKEY123", txt)
                self.assertEqual(json.loads(txt)["authMode"], "anonymous-no-key")
            h = json.loads((Path(tmp) / "_health" / "2026-10-24.ndjson").read_text())
            self.assertTrue(h["keyRejected"])
            self.assertNotIn("BADKEY123", json.dumps(h))


class ClientTests(unittest.TestCase):
    def test_retry_then_success_and_429(self):
        calls = {"n": 0}
        sleeps = []

        def fetch(url, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, b"", {"Retry-After": "3"}
            if calls["n"] == 2:
                return 503, b"", {}
            return 200, b'{"ok":1}', {}
        c = collect.Client(None, fetch=fetch, sleep=sleeps.append, rng=lambda: 0.0)
        self.assertEqual(c.get("/x"), {"ok": 1})
        self.assertEqual((c.requests, c.retries, c.http429), (3, 2, 1))
        self.assertIn(3.0, sleeps)  # Retry-After honoured

    def test_404_not_retried_and_total_failure(self):
        c = collect.Client(None, fetch=lambda u, t: (404, b"", {}), sleep=lambda s: None)
        self.assertIsNone(c.get("/x"))
        self.assertEqual(c.requests, 1)
        c2 = collect.Client(None, fetch=lambda u, t: (500, b"", {}), sleep=lambda s: None)
        self.assertIsNone(c2.get("/x"))
        self.assertEqual(c2.requests, collect.ATTEMPTS)

    def test_network_exception_retried(self):
        def fetch(u, t):
            raise OSError("down")
        c = collect.Client(None, fetch=fetch, sleep=lambda s: None)
        self.assertIsNone(c.get("/x"))
        self.assertEqual(c.requests, collect.ATTEMPTS)


class RunTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("TFL_APP_KEY", None)

    @staticmethod
    def station_zip():
        import io, zipfile
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("Lifts.csv", "StationUniqueId,LiftUniqueId,FromAreas,ToAreas\nS,S-Lift-1,A,B\nS,S-Lift-2,A,B\n")
            z.writestr("Stations.csv", "UniqueId,Name\nS,Somewhere\n")
        return b.getvalue()

    def fake_fetch(self, url, timeout):
        if "/stationdata/" in url:
            return 200, self.station_zip(), {}
        if "Lifts" in url:
            return 200, json.dumps([{"stationUniqueId": "S", "message": "m"}]).encode(), {}
        if "/Line/Mode/" in url:
            return 200, json.dumps([{"id": "central", "name": "Central", "modeName": "tube",
                                     "lineStatuses": [{"statusSeverity": 10, "statusSeverityDescription": "Good Service"}]}]).encode(), {}
        if "/Status/" in url and "/to/" in url:       # plannedLineStatus: the week-ahead call for the ids listed above
            return 200, json.dumps([{"id": "central", "name": "Central", "modeName": "tube",
                                     "lineStatuses": [{"statusSeverity": 10, "statusSeverityDescription": "Good Service"}]}]).encode(), {}
        if "/Disruption" in url:
            return 200, b"[]", {}
        if "/crowding/" in url:
            return 200, json.dumps({"dataAvailable": True, "percentageOfBaseline": 0.2}).encode(), {}
        if "/Arrivals" in url:
            return 200, json.dumps([{"lineName": "Bakerloo", "timeToStation": 120}, {"lineName": "Bakerloo", "timeToStation": 60}]).encode(), {}
        return 404, b"", {}

    def args(self, out, now="2026-10-24T15:50:00Z", **kw):
        import argparse
        d = dict(out=out, registry=str(ROOT / "config" / "registry.json"), now=now, no_sleep=True,
                 datasets=None, dry_run=False, current_only=False)
        d.update(kw)
        return argparse.Namespace(**d)

    def lines(self, tmp, d, day="2026-10-24"):
        p = Path(tmp) / d / f"{day}.ndjson"
        if not p.exists() and Path(str(p) + ".gz").exists():
            import gzip
            return [json.loads(l) for l in gzip.open(str(p) + ".gz", "rt").read().splitlines()]
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_full_run_and_idempotent_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = collect.run(self.args(tmp), fetch=self.fake_fetch, sleep=lambda s: None,
                             clock=lambda: dt("2026-10-24T16:00:01Z"))
            self.assertEqual(rc, 0)
            self.assertEqual(self.lines(tmp, "nationalRailDepartures"), [])   # no NR_API_KEY: skipped, not a gap
            self.assertEqual(self.lines(tmp, "nationalRailIncidents"), [])
            for d in [x for x in collect.DATASETS if x not in ("nationalRailDepartures", "nationalRailIncidents", "nationalRailStations", "stationAccess", "plannedLineStatus")]:
                recs = self.lines(tmp, d)
                self.assertEqual([r["slot"] for r in recs], ["2026-10-24T16:00:00Z"])
                self.assertEqual(recs[0]["authMode"], "anonymous-no-key")
            arr = self.lines(tmp, "arrivalsReadings")[0]
            self.assertEqual(arr["stations"]["baker-street"]["nextByLine"]["Bakerloo"], {"count": 2, "minSeconds": 60})
            health = self.lines(tmp, "_health")
            self.assertEqual(len(health), 1)
            self.assertEqual(health[0]["datasets"]["crowdingReadings"]["status"], "ok")
            self.assertEqual(health[0]["requests"], 1 + 2 + 5 + 33 + 47 + 1 + 5 + 315 + 1 + 6 + 3)  # + daily station data ZIP + 6 network arrivals modes + plannedLineStatus (2 mode lists, 1 status call)  # arrivals calls shared with arrivalsDetail
            # backup run: nothing to do, nothing fetched, nothing appended
            rc2 = collect.run(self.args(tmp, now="2026-10-24T16:05:00Z"), fetch=lambda u, t: 1 / 0,
                              sleep=lambda s: None, clock=lambda: dt("2026-10-24T16:05:01Z"))
            self.assertEqual(rc2, 0)
            for d in [x for x in collect.DATASETS if x not in ("nationalRailDepartures", "nationalRailIncidents", "nationalRailStations", "stationAccess", "plannedLineStatus")] + ["_health"]:
                self.assertEqual(len(self.lines(tmp, d)), 1, d)

    def test_backup_fills_only_missing_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            collect.run(self.args(tmp, datasets="liftDisruptions"), fetch=self.fake_fetch,
                        sleep=lambda s: None, clock=lambda: dt("2026-10-24T16:00:01Z"))
            collect.run(self.args(tmp, now="2026-10-24T16:05:00Z"), fetch=self.fake_fetch,
                        sleep=lambda s: None, clock=lambda: dt("2026-10-24T16:05:01Z"))
            for d in [x for x in collect.DATASETS if x not in ("nationalRailDepartures", "nationalRailIncidents", "nationalRailStations", "stationAccess", "plannedLineStatus")]:
                self.assertEqual(len(self.lines(tmp, d)), 1, d)

    def test_late_backup_fills_missed_hour_then_next_hour(self):
        # :05 backup delayed to :35 after the :50 run was dropped: 15:00 is late-filled, then 16:00 on time.
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []
            rc = collect.run(self.args(tmp, now="2026-10-24T15:35:00Z", no_sleep=False), fetch=self.fake_fetch,
                             sleep=sleeps.append, clock=lambda: dt("2026-10-24T15:35:00Z"))
            self.assertEqual(rc, 0)
            for d in [x for x in collect.DATASETS if x not in ("nationalRailDepartures", "nationalRailIncidents", "nationalRailStations", "stationAccess", "plannedLineStatus")]:
                self.assertEqual([r["slot"] for r in self.lines(tmp, d)],
                                 ["2026-10-24T15:00:00Z", "2026-10-24T16:00:00Z"], d)
            self.assertIn(1500.0, sleeps)  # waited until 16:00:00, never polled early

    def test_current_only_does_not_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []
            collect.run(self.args(tmp, now="2026-10-24T15:40:00Z", no_sleep=False, current_only=True),
                        fetch=self.fake_fetch, sleep=sleeps.append, clock=lambda: dt("2026-10-24T15:40:00Z"))
            self.assertEqual([r["slot"] for r in self.lines(tmp, "lineStatus")], ["2026-10-24T15:00:00Z"])
            self.assertFalse([s for s in sleeps if s > 60])

    def test_total_failure_exit_2_and_health_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = collect.run(self.args(tmp), fetch=lambda u, t: (500, b"", {}), sleep=lambda s: None,
                             clock=lambda: dt("2026-10-24T16:00:01Z"))
            self.assertEqual(rc, 2)
            h = self.lines(tmp, "_health")
            self.assertEqual(len(h), 1)
            self.assertEqual(h[0]["datasets"]["liftDisruptions"]["status"], "failed")

    def test_one_dataset_missing_is_exit_2(self):
        def fetch(url, timeout):
            if "Lifts" in url:
                return 503, b"", {}
            return self.fake_fetch(url, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            rc = collect.run(self.args(tmp), fetch=fetch, sleep=lambda s: None, clock=lambda: dt("2026-10-24T16:00:01Z"))
            self.assertEqual(rc, 2)
            self.assertEqual(len(self.lines(tmp, "lineStatus")), 1)

    def test_multi_id_arrivals_dedupe_and_detail_rows(self):
        def fetch(url, timeout):
            if "/Arrivals" in url:
                return 200, json.dumps([{"id": "p1", "lineId": "elizabeth", "lineName": "Elizabeth line", "timeToStation": 90,
                                         "platformName": "B", "destinationNaptanId": "910GSHENFLD", "vehicleId": "v1"},
                                        {"id": "p2", "lineId": "elizabeth", "lineName": "Elizabeth line", "timeToStation": 30}]).encode(), {}
            return self.fake_fetch(url, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            rc = collect.run(self.args(tmp, datasets="arrivalsReadings,arrivalsDetail"), fetch=fetch, sleep=lambda s: None,
                             clock=lambda: dt("2026-10-24T16:00:01Z"))
            self.assertEqual(rc, 0)
            s = self.lines(tmp, "arrivalsReadings")[0]["stations"]["liverpool-street"]
            self.assertEqual(s["totalPredictions"], 2)        # same predictions from 3 ids counted once
            det = self.lines(tmp, "arrivalsDetail")[0]
            self.assertEqual(det["fields"][:3], ["naptanId", "lineId", "platformName"])
            rows = det["stations"]["liverpool-street"]
            self.assertEqual([r[det["fields"].index("timeToStation")] for r in rows], [30, 90])
            self.assertEqual(self.lines(tmp, "_health")[0]["requests"], 47)   # one pass shared by both datasets

    def test_bus_status_counts_good_and_keeps_disrupted(self):
        def fetch(url, timeout):
            if "/Line/Mode/bus/Status" in url:
                return 200, json.dumps([{"id": "1", "name": "1", "lineStatuses": [{"statusSeverity": 10, "statusSeverityDescription": "Good Service"}]},
                                        {"id": "68", "name": "68", "lineStatuses": [{"statusSeverity": 0, "statusSeverityDescription": "Special Service",
                                                                                      "reason": "Diversion via York Way"}]}]).encode(), {}
            return self.fake_fetch(url, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            collect.run(self.args(tmp, datasets="busLineStatus"), fetch=fetch, sleep=lambda s: None, clock=lambda: dt("2026-10-24T16:00:01Z"))
            b = self.lines(tmp, "busLineStatus")[0]
            self.assertEqual((b["totalLines"], b["goodServiceCount"], [x["id"] for x in b["disrupted"]]), (2, 1, ["68"]))
            self.assertEqual(b["disrupted"][0]["statuses"][0]["reason"], "Diversion via York Way")

    def test_line_status_keeps_validity_and_disruption(self):
        def fetch(url, timeout):
            if "/Line/Mode/" in url:
                return 200, json.dumps([{"id": "district", "name": "District", "modeName": "tube", "lineStatuses": [{
                    "statusSeverity": 5, "statusSeverityDescription": "Part Closure", "reason": "Works",
                    "validityPeriods": [{"fromDate": "2026-10-24T05:00:00Z", "toDate": "2026-10-25T23:59:00Z", "isNow": True}],
                    "disruption": {"category": "PlannedWork", "categoryDescription": "PlannedWork", "created": "2026-10-01T10:00:00Z",
                                   "affectedStops": [{"naptanId": "940GZZLUWIM"}], "description": "x"}}]}]).encode(), {}
            return self.fake_fetch(url, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            collect.run(self.args(tmp, datasets="lineStatus"), fetch=fetch, sleep=lambda s: None, clock=lambda: dt("2026-10-24T16:00:01Z"))
            st = self.lines(tmp, "lineStatus")[0]["lines"][0]["statuses"][0]
            self.assertEqual(st["validity"], [["2026-10-24T05:00:00Z", "2026-10-25T23:59:00Z"]])
            self.assertEqual(st["disruption"]["category"], "PlannedWork")
            self.assertEqual(st["disruption"]["affectedStops"], ["940GZZLUWIM"])

    def test_partial_crowding_flagged_not_failed(self):
        def fetch(url, timeout):
            if "/crowding/940GZZLUBST/" in url:
                return 404, b"", {}
            return self.fake_fetch(url, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            rc = collect.run(self.args(tmp, datasets="crowdingReadings"), fetch=fetch, sleep=lambda s: None,
                             clock=lambda: dt("2026-10-24T16:00:01Z"))
            self.assertEqual(rc, 0)
            rec = self.lines(tmp, "crowdingReadings")[0]
            self.assertIsNone(rec["stations"]["baker-street"])
            self.assertEqual(self.lines(tmp, "_health")[0]["datasets"]["crowdingReadings"]["status"], "partial")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = collect.run(self.args(tmp, dry_run=True), fetch=lambda u, t: 1 / 0)
            self.assertEqual(rc, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class VerifyTests(unittest.TestCase):
    def test_missing_slots_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for d in verify.DATASETS:
                for h in range(0, 9):
                    if d == "lineStatus" and h in (3, 4, 5):
                        continue
                    slot = dt("2026-10-24T00:00:00Z") + timedelta(hours=h)
                    collect.append_line(collect.ndjson_path(tmp, d, slot), {"slot": collect.slot_str(slot), "polledAt": collect.iso(slot)})
            res, corrupt = verify.check(tmp, 48, dt("2026-10-24T08:30:00Z"))
            self.assertEqual(corrupt, [])
            self.assertEqual(len(res["lineStatus"]["missing"]), 3)
            self.assertEqual(len(res["liftDisruptions"]["missing"]), 0)
            issue = Path(tmp) / "issue.md"
            self.assertEqual(verify.main(["--data", tmp, "--now", "2026-10-24T08:30:00Z", "--issue-file", str(issue)]), 0)
            self.assertIn("lineStatus", issue.read_text())

    def test_new_dataset_checked_from_its_first_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            for d in verify.DATASETS:
                for h in range(0, 9):
                    if d == "arrivalsDetail" and h < 6:
                        continue   # dataset introduced at 06:00
                    slot = dt("2026-10-24T00:00:00Z") + timedelta(hours=h)
                    collect.append_line(collect.ndjson_path(tmp, d, slot), {"slot": collect.slot_str(slot), "polledAt": collect.iso(slot)})
            res, _ = verify.check(tmp, 48, dt("2026-10-24T08:30:00Z"))
            self.assertEqual(res["arrivalsDetail"]["missing"], [])
            self.assertEqual(res["arrivalsDetail"]["expected"], 3)

    def test_imported_history_does_not_trip_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            for h in (0, 5, 6, 7, 8):   # legacy hours 0 and 5 (gap 1-4), collector from 06:00
                slot = dt("2026-10-24T00:00:00Z") + timedelta(hours=h)
                rec = {"slot": collect.slot_str(slot), "polledAt": collect.iso(slot)}
                if h < 6:
                    rec["legacyDocId"] = f"reading-{h}"
                collect.append_line(collect.ndjson_path(tmp, "lineStatus", slot), rec)
            res, _ = verify.check(tmp, 48, dt("2026-10-24T08:30:00Z"))
            self.assertEqual((res["lineStatus"]["expected"], res["lineStatus"]["missing"]), (3, []))

    def test_corrupt_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "lineStatus" / "2026-10-24.ndjson"
            p.parent.mkdir(parents=True)
            p.write_text("garbage\n")
            self.assertEqual(verify.main(["--data", tmp, "--now", "2026-10-24T08:30:00Z"]), 3)


if __name__ == "__main__":
    unittest.main()
