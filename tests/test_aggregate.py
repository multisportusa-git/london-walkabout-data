import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import aggregate  # noqa: E402

XML = """<ns0:PtIncident xmlns:ns0="http://nationalrail.co.uk/xml/incident" xmlns:ns1="http://nationalrail.co.uk/xml/common">
<ns0:ChangeHistory><ns1:LastChangedDate>2026-10-25T07:57:11.495Z</ns1:LastChangedDate></ns0:ChangeHistory>
<ns0:IncidentNumber>ABC</ns0:IncidentNumber><ns0:Version>{v}</ns0:Version>
<ns0:ValidityPeriod><ns1:StartTime>2026-10-24T00:00:00.000+00:00</ns1:StartTime><ns1:EndTime>2026-10-26T23:59:00.000+00:00</ns1:EndTime></ns0:ValidityPeriod>
<ns0:Planned>true</ns0:Planned>
<ns0:Summary>{s}</ns0:Summary>
<ns0:Description>&lt;p&gt;Engineering work is &lt;a href="x"&gt;closing&lt;/a&gt; all lines.&lt;/p&gt;&lt;h4&gt;&lt;strong&gt;Southern:&lt;/strong&gt;&lt;/h4&gt;&lt;ul&gt;&lt;li&gt;Buses replace trains&lt;/li&gt;&lt;/ul&gt;</ns0:Description>
<ns0:InfoLinks><ns0:InfoLink><ns0:Uri>https://www.nationalrail.co.uk/service-disruptions/wivelsfield-20261025/</ns0:Uri></ns0:InfoLink></ns0:InfoLinks>
<ns0:Affects><ns0:Operators><ns0:AffectedOperator><ns0:OperatorRef>SN</ns0:OperatorRef></ns0:AffectedOperator></ns0:Operators>
<ns0:RoutesAffected>&lt;p&gt;Between Brighton and Haywards Heath&lt;/p&gt;</ns0:RoutesAffected></ns0:Affects>
</ns0:PtIncident>"""


def write_incidents(d, versions):
    p = Path(d) / "nationalRailIncidents"; p.mkdir(parents=True)
    with gzip.open(p / "2026-10-25.ndjson.gz", "wt") as f:
        for v, s in versions:
            f.write(json.dumps({"slot": "x", "newOrChanged": [{"id": "ABC", "version": v, "xml": XML.format(v=v, s=s)}]}) + "\n")
        f.write("{corrupt\n")


class HtmlBlocksTests(unittest.TestCase):
    def test_structure_and_links_flattened(self):
        b = aggregate.html_blocks('<p>Engineering work is <a href="x">closing</a> all lines.</p><h4><strong>Southern:</strong></h4><ul><li>Buses</li></ul>')
        self.assertEqual(b, [{"p": "Engineering work is closing all lines."}, {"h": "Southern:"}, {"li": "Buses"}])

    def test_empty_and_cap(self):
        self.assertEqual(aggregate.html_blocks(""), [])
        b = aggregate.html_blocks("<p>" + "word " * 3000 + "</p>", cap=500)
        self.assertLessEqual(sum(len(list(x.values())[0]) for x in b), 510)
        self.assertTrue(list(b[-1].values())[0].endswith("…"))


class TidyReasonTests(unittest.TestCase):
    def test_strips_line_prefix(self):
        self.assertEqual(aggregate.tidy_reason("CIRCLE LINE: Saturday 24, no service", "Circle"), "Saturday 24, no service")
        self.assertEqual(aggregate.tidy_reason("Suffragette Line: No service  ", "Suffragette"), "No service")
        self.assertEqual(aggregate.tidy_reason("ELIZABETH LINE: Sunday", "Elizabeth line"), "Sunday")

    def test_strips_caps_network_label(self):
        self.assertEqual(aggregate.tidy_reason("DOCKLANDS LIGHT RAILWAY: Sunday 25 October, no service", "DLR"), "Sunday 25 October, no service")
        self.assertEqual(aggregate.tidy_reason("LONDON TRAMS: Saturday 24 October", "Tram"), "Saturday 24 October")
        self.assertEqual(aggregate.tidy_reason("Note: trains run", "Jubilee"), "Note: trains run")

    def test_all_caps_sentence_case(self):
        self.assertEqual(aggregate.tidy_reason("WOOLWICH FERRY NO SERVICE", "Woolwich Ferry"), "Woolwich ferry no service")

    def test_keeps_other_text(self):
        self.assertEqual(aggregate.tidy_reason("Minor delays due to a signal failure", "Jubilee"), "Minor delays due to a signal failure")


class IncidentLookupTests(unittest.TestCase):
    def test_latest_version_by_url(self):
        with tempfile.TemporaryDirectory() as d:
            write_incidents(d, [("20261025070000", "Old summary"), ("20261025075711", "New summary")])
            look = aggregate.nr_incident_lookup(d)
            inc = look("https://www.nationalrail.co.uk/service-disruptions/wivelsfield-20261025")
            self.assertEqual(inc["summary"], "New summary")
            self.assertEqual(inc["routes"], "Between Brighton and Haywards Heath")
            self.assertEqual(inc["body"][0], {"p": "Engineering work is closing all lines."})
            self.assertEqual(inc["link"], "https://www.nationalrail.co.uk/service-disruptions/wivelsfield-20261025/")
            self.assertTrue(inc["planned"])
            self.assertEqual(inc["updated"], "2026-10-25T07:57:11.495Z")
            self.assertIsNone(look("https://example.com/nope"))
            self.assertIsNone(look(None))

    def test_missing_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(aggregate.nr_incident_lookup(d)("https://x"))


class LineDetailsTests(unittest.TestCase):
    def test_tfl_full_reason_dedupes(self):
        r = "Suffragette Line: No service between A and B, and MINOR DELAYS on the rest of the line due to an operational incident. " * 3
        x = {"id": "suffragette", "name": "Suffragette", "mode": "overground", "statuses": [
            {"severity": 9, "description": "Minor Delays", "reason": r, "validity": [["2026-10-25T05:08:28Z", "2026-10-26T01:29:00Z"]],
             "disruption": {"category": "RealTime", "created": None}},
            {"severity": 3, "description": "Part Suspended", "reason": r, "validity": [["2026-10-25T05:08:28Z", "2026-10-25T10:59:25Z"]],
             "disruption": {"category": "RealTime"}}]}
        det = aggregate.line_details(x, lambda u: None)
        self.assertEqual(len(det), 1)
        self.assertEqual(det[0]["statuses"], ["Part Suspended", "Minor Delays"])
        self.assertEqual(det[0]["kind"], "live")
        self.assertGreater(len(det[0]["text"]), 240)  # no truncation
        self.assertEqual(det[0]["until"], "2026-10-26T01:29:00Z")
        self.assertEqual(det[0]["source"], "TfL")

    def test_good_service_has_no_details(self):
        x = {"id": "jubilee", "name": "Jubilee", "statuses": [{"severity": 10, "description": "Good Service"}]}
        self.assertEqual(aggregate.line_details(x, lambda u: None), [])

    def test_national_rail_url_replaced_by_incident(self):
        with tempfile.TemporaryDirectory() as d:
            write_incidents(d, [("20261025075711", "Southern: buses between Brighton and Haywards Heath")])
            x = {"id": "southern", "name": "Southern", "mode": "national-rail", "statuses": [
                {"severity": 0, "description": "Special Service", "reason": "https://www.nationalrail.co.uk/service-disruptions/wivelsfield-20261025/",
                 "validity": [["2026-10-25T00:00:00Z", "2026-10-25T23:59:59Z"]], "disruption": {"category": "Information"}}]}
            det = aggregate.line_details(x, aggregate.nr_incident_lookup(d))
            self.assertEqual(det[0]["source"], "National Rail")
            self.assertEqual(det[0]["incident"]["summary"], "Southern: buses between Brighton and Haywards Heath")
            self.assertIsNone(det[0]["text"])  # bare URL is not shown as the message
            self.assertEqual(det[0]["kind"], "planned")

    def test_national_rail_unmatched_url_keeps_link_only(self):
        x = {"id": "c2c", "name": "c2c", "mode": "national-rail", "statuses": [
            {"severity": 0, "description": "Special Service", "reason": "https://www.nationalrail.co.uk/x/"}]}
        det = aggregate.line_details(x, lambda u: None)
        self.assertIsNone(det[0]["text"]); self.assertIsNone(det[0]["incident"])
        self.assertEqual(det[0]["link"], "https://www.nationalrail.co.uk/x/")


class TextTests(unittest.TestCase):
    def test_uk_time_and_lift_label(self):
        self.assertEqual(aggregate.uk_time("2026-10-25T07:01:13Z"), "25 Oct 07:01 UK")   # after clocks went back
        self.assertEqual(aggregate.uk_time("2026-09-30T10:00:00Z"), "30 Sep 11:00 UK")   # BST
        self.assertEqual(aggregate.lift_label("HUBEPH-Lift-5"), " (TfL lift 5)")
        self.assertEqual(aggregate.lift_label(None), "")


class HistoryTests(unittest.TestCase):
    def test_last_24_hours(self):
        LS = {"2026-10-25T08:00:00Z": {"lines": [{"id": "circle", "statuses": [{"severity": 5, "description": "Part Closure"}]}]},
              "2026-10-25T06:00:00Z": {"lines": [{"id": "circle", "statuses": [{"severity": 10, "description": "Good Service"}]}]}}
        h = aggregate.line_history(LS, "2026-10-25T08:00:00Z")
        self.assertEqual(len(h["circle"]), 24)
        self.assertEqual(h["circle"][-1], 3)
        self.assertIsNone(h["circle"][-2])
        self.assertEqual(h["circle"][-3], 0)


class UpcomingTests(unittest.TestCase):
    """upcoming.json: planned works for the next 7 days, from the daily plannedLineStatus capture (TfL modes) and
    the National Rail Knowledgebase incidents (planned engineering works only), as day-by-day windows."""
    NOW = "2026-10-25T13:00:00Z"                     # 25 Oct 2026: clocks went back at 02:00, London = UTC from here

    def put(self, d, ds, recs, gz=True):
        p = Path(d) / ds; p.mkdir(parents=True, exist_ok=True)
        f = p / ("2026-10-25.ndjson.gz" if gz else "2026-10-25.ndjson")
        with (gzip.open(f, "wt") if gz else open(f, "w")) as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def inc_xml(self, iid, start, end, planned, ops, summary):
        return ("<ns0:PtIncident xmlns:ns0=\"http://nationalrail.co.uk/xml/incident\" xmlns:ns1=\"http://nationalrail.co.uk/xml/common\">"
                f"<ns0:IncidentNumber>{iid}</ns0:IncidentNumber><ns0:Version>1</ns0:Version>"
                f"<ns0:ValidityPeriod><ns1:StartTime>{start}</ns1:StartTime><ns1:EndTime>{end}</ns1:EndTime></ns0:ValidityPeriod>"
                f"<ns0:Planned>{'true' if planned else 'false'}</ns0:Planned><ns0:Summary>{summary}</ns0:Summary>"
                "<ns0:Affects><ns0:Operators>" + "".join(f"<ns0:AffectedOperator><ns0:OperatorRef>{o}</ns0:OperatorRef></ns0:AffectedOperator>" for o in ops)
                + "</ns0:Operators></ns0:Affects></ns0:PtIncident>")

    def build(self, d):
        self.put(d, "plannedLineStatus", [{"slot": "2026-10-25T05:00:00Z", "from": "2026-10-25", "to": "2026-11-01", "lines": [
            {"id": "central", "name": "Central", "mode": "tube", "statuses": [
                {"severity": 10, "description": "Good Service", "reason": None},
                {"severity": 5, "description": "Part Closure", "reason": "CENTRAL LINE: no service between Epping and Leytonstone.",
                 "validity": [["2026-10-31T00:30:00Z", "2026-11-01T04:00:00Z"]],
                 "disruption": {"category": "PlannedWork", "affectedStops": ["940GZZLULYN", "940GZZLUEPG"]}},
                {"severity": 5, "description": "Part Closure", "reason": "CENTRAL LINE: no service between Epping and Leytonstone.",   # duplicate with other stops
                 "validity": [["2026-10-31T00:30:00Z", "2026-11-01T04:00:00Z"]],
                 "disruption": {"category": "PlannedWork", "affectedStops": ["940GZZLULYN"]}}]},
            {"id": "jubilee", "name": "Jubilee", "mode": "tube", "statuses": [
                {"severity": 9, "description": "Minor Delays", "reason": "Minor delays due to a signal failure",
                 "validity": [["2026-10-25T12:00:00Z", "2026-10-26T01:00:00Z"]], "disruption": {"category": "RealTime"}}]}]}])
        act = [["PLAN1", "1", True, "2", "2026-10-24T00:00:00.000+00:00", "2026-10-26T23:59:00.000+00:00", ["SN", "TL"], False],
               ["LATER", "1", True, "2", "2026-12-20T00:00:00.000+00:00", "2026-12-20T23:59:00.000+00:00", ["SN"], False],
               ["FAULT", "1", False, "1", "2026-10-25T08:00:00.000+00:00", "2026-10-25T18:00:00.000+00:00", ["SE"], False],
               ["DONE", "1", True, "2", "2026-10-25T00:00:00.000+00:00", "2026-10-27T00:00:00.000+00:00", ["SW"], True]]
        xml = lambda i, a, b, pl, ops, t: {"id": i, "version": "1", "xml": self.inc_xml(i, a, b, pl, ops, t)}
        self.put(d, "nationalRailIncidents", [{"slot": "2026-10-25T12:00:00Z", "count": 4, "active": act, "newOrChanged": [
            xml("PLAN1", act[0][4], act[0][5], True, ["SN", "TL"], "Buses replace trains between Brighton and Haywards Heath"),
            xml("LATER", act[1][4], act[1][5], True, ["SN"], "Christmas works"),
            xml("FAULT", act[2][4], act[2][5], False, ["SE"], "Signalling fault at Lewisham"),
            xml("DONE", act[3][4], act[3][5], True, ["SW"], "CLEARED: works finished")]}])
        return aggregate.build_upcoming(d, self.NOW)

    def test_tfl_planned_closure_by_day_deduped_and_unplanned_left_out(self):
        with tempfile.TemporaryDirectory() as d:
            U = self.build(d)
            self.assertEqual((U["v"], U["from"], U["days"][0], U["days"][-1], len(U["days"])), (1, "2026-10-25", "2026-10-25", "2026-10-31", 7))
            self.assertEqual(U["tflSlot"], "2026-10-25T05:00:00Z")          # when TfL's planned works were last read
            self.assertNotIn("jubilee", U["lines"])                       # real-time delay: not planned works
            c = U["lines"]["central"]
            self.assertEqual((c["n"], c["m"]), ("Central", "tube"))
            self.assertEqual(list(c["d"]), ["2026-10-31"])
            self.assertEqual((c["d"]["2026-10-31"]["k"], c["d"]["2026-10-31"]["h"], c["d"]["2026-10-31"]["s"]), ("p", 23.5, "Part Closure"))
            self.assertEqual(len(c["r"]), 1)                              # the duplicate message appears once
            self.assertEqual((c["r"][0]["t"], c["r"][0]["w"]), ("no service between Epping and Leytonstone.", [["2026-10-31T00:30:00Z", "2026-11-01T04:00:00Z"]]))
            self.assertEqual(c["d"]["2026-10-31"]["r"], [0])
            self.assertEqual(U["stops"], {"940GZZLULYN": {"2026-10-31": "p"}, "940GZZLUEPG": {"2026-10-31": "p"}})
            self.assertEqual(U["stopLines"], {"940GZZLULYN": ["central"], "940GZZLUEPG": ["central"]})

    def test_national_rail_planned_works_only_within_the_week(self):
        with tempfile.TemporaryDirectory() as d:
            U = self.build(d)
            self.assertEqual(sorted(U["nr"]), ["SN", "TL"])                 # not SE (unplanned fault), SW (cleared)
            sn = U["nr"]["SN"]
            self.assertEqual(sorted(sn["d"]), ["2026-10-25", "2026-10-26"])
            self.assertEqual((sn["d"]["2026-10-25"]["k"], sn["d"]["2026-10-25"]["n"]), ("p", 1))
            self.assertEqual(sn["r"][0]["t"], "Buses replace trains between Brighton and Haywards Heath")
            self.assertEqual(sn["r"][0]["w"], [["2026-10-24T00:00:00.000+00:00", "2026-10-26T23:59:00.000+00:00"]])      # as published
            self.assertNotIn("Christmas works", [x["t"] for x in sn["r"]])

    def test_missing_data_gives_an_empty_but_valid_file(self):
        with tempfile.TemporaryDirectory() as d:
            U = aggregate.build_upcoming(d, self.NOW)
            self.assertEqual((U["v"], U["lines"], U["nr"], U["stops"], U["stopLines"], len(U["days"])), (1, {}, {}, {}, {}, 7))
            self.assertIsNone(U["tflSlot"])                                  # nothing captured yet: the page says so rather than "none planned"


class UpcomingPublishTests(unittest.TestCase):
    def test_file_always_published_even_with_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); out = Path(d) / "out"
            aggregate.publish_upcoming(str(Path(d) / "data"), str(out))
            U = json.loads((out / "rollups" / "upcoming.json").read_text())
            self.assertEqual((U["v"], U["lines"], U["nr"], U["stops"], len(U["days"])), (1, {}, {}, {}, 7))


class NrReasonsPublishTests(unittest.TestCase):
    """The page always fetches rollups/nrReasons.json, so it must always exist (an empty object until the
    Darwin reason list has been fetched) and never cause a 404 in the browser console."""

    def test_empty_object_when_no_reference_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); out = Path(d) / "out"
            aggregate.publish_nr_reasons(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "nrReasons.json").read_text()), {})

    def test_copies_the_reference_file_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); (Path(d) / "config").mkdir(); out = Path(d) / "out"
            ref = {"canc": {"681": "cancelled text"}, "late": {"681": "late text"}, "fetched": "2026-10-25"}
            (Path(d) / "config" / "nrReasonCodes.json").write_text(json.dumps(ref))
            aggregate.publish_nr_reasons(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "nrReasons.json").read_text()), ref)

    def test_broken_reference_file_gives_empty_object_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); (Path(d) / "config").mkdir(); out = Path(d) / "out"
            (Path(d) / "config" / "nrReasonCodes.json").write_text("{not json")
            aggregate.publish_nr_reasons(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "nrReasons.json").read_text()), {})


class BusRoutesPublishTests(unittest.TestCase):
    """The page always fetches rollups/busRoutes.json: it must exist even before the list has been fetched."""

    def test_empty_list_when_no_reference_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); out = Path(d) / "out"
            aggregate.publish_bus_routes(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "busRoutes.json").read_text()), {"routes": []})

    def test_copies_the_reference_file_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); (Path(d) / "config").mkdir(); out = Path(d) / "out"
            ref = {"routes": ["1", "100", "N29"], "fetched": "2026-10-25"}
            (Path(d) / "config" / "busRoutes.json").write_text(json.dumps(ref))
            aggregate.publish_bus_routes(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "busRoutes.json").read_text()), ref)

    def test_broken_reference_file_gives_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); (Path(d) / "config").mkdir(); out = Path(d) / "out"
            (Path(d) / "config" / "busRoutes.json").write_text("{nope")
            aggregate.publish_bus_routes(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "busRoutes.json").read_text()), {"routes": []})


class LineRoutesPublishTests(unittest.TestCase):
    """The page fetches rollups/lineRoutes.json for the favourites route map: it must always exist."""

    def test_empty_when_no_reference_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); out = Path(d) / "out"
            aggregate.publish_line_routes(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "lineRoutes.json").read_text()), {"lines": {}, "stops": {}})

    def test_copies_the_reference_file_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); (Path(d) / "config").mkdir(); out = Path(d) / "out"
            ref = {"lines": {"x": {"n": "X", "m": "tube", "b": [["A", "B"]]}}, "stops": {"A": ["A", 1, 2], "B": ["B", 3, 4]}, "fetched": "2026-10-25"}
            (Path(d) / "config" / "lineRoutes.json").write_text(json.dumps(ref))
            aggregate.publish_line_routes(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "lineRoutes.json").read_text()), ref)

    def test_broken_reference_file_gives_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); (Path(d) / "config").mkdir(); out = Path(d) / "out"
            (Path(d) / "config" / "lineRoutes.json").write_text("{nope")
            aggregate.publish_line_routes(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "lineRoutes.json").read_text()), {"lines": {}, "stops": {}})


class NrStationsPublishTests(unittest.TestCase):
    """The route builder fetches rollups/nrStations.json: it must always exist and merge every day."""

    def rec(self, *items):
        return {"slot": "x", "changed": [{"crsCode": c, "name": n, "location": {"latitude": la, "longitude": lo}, "stationOperator": {"slug": op}} for c, n, la, lo, op in items]}

    def test_empty_when_no_dataset(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "data").mkdir(); out = Path(d) / "out"
            aggregate.publish_nr_stations(str(Path(d) / "data"), str(out))
            self.assertEqual(json.loads((out / "rollups" / "nrStations.json").read_text()), {"s": {}})

    def test_merges_days_later_wins_and_skips_bad_records(self):
        import gzip
        with tempfile.TemporaryDirectory() as d:
            ds = Path(d) / "data" / "nationalRailStations"; ds.mkdir(parents=True); out = Path(d) / "out"
            with gzip.open(ds / "2026-10-24.ndjson.gz", "wt") as fh:
                fh.write(json.dumps(self.rec(("CTN", "Charlton", 51.48876, 0.03139, "southeastern"), ("LBG", "London Bridge", 51.5, -0.08, "southern"))) + "\n")
                fh.write("not json\n")
            (ds / "2026-10-25.ndjson").write_text(json.dumps(self.rec(("LBG", "London Bridge", 51.50505, -0.08604, "thameslink"))) + "\n"
                                                  + json.dumps({"changed": [{"crsCode": "BAD"}]}) + "\n")
            aggregate.publish_nr_stations(str(Path(d) / "data"), str(out))
            j = json.loads((out / "rollups" / "nrStations.json").read_text())
            self.assertEqual(sorted(j["s"]), ["CTN", "LBG"])
            self.assertEqual(j["s"]["LBG"], ["London Bridge", 51.50505, -0.08604, "thameslink"])


if __name__ == "__main__":
    unittest.main()
