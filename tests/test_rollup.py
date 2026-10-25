import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import rollup  # noqa: E402

DAY = "2026-10-25"


def put(d, ds, day, recs, gz=False):
    p = Path(d) / ds; p.mkdir(parents=True, exist_ok=True)
    f = p / (day + ".ndjson" + (".gz" if gz else ""))
    txt = "".join(json.dumps(r) + "\n" for r in recs) + "{broken\n"
    if gz:
        with gzip.open(f, "at") as fh: fh.write(txt)
    else:
        with open(f, "a") as fh: fh.write(txt)


def sample(d):
    put(d, "lineStatus", DAY, [
        {"slot": f"{DAY}T08:00:00Z", "lines": [
            {"id": "circle", "name": "Circle", "mode": "tube", "statuses": [{"severity": 5, "description": "Part Closure", "reason": "CIRCLE LINE: no service between A and B"}]},
            {"id": "southern", "name": "Southern", "mode": "national-rail", "statuses": [{"severity": 0, "description": "Special Service", "reason": "https://www.nationalrail.co.uk/x/",
                                                                                      "nrIncident": {"summary": "Delays at Wivelsfield"}}]}]},
        {"slot": f"{DAY}T09:00:00Z", "lines": [
            {"id": "circle", "name": "Circle", "mode": "tube", "statuses": [{"severity": 5, "description": "Part Closure", "reason": "CIRCLE LINE: no service between A and B"}]},
            {"id": "southern", "name": "Southern", "mode": "national-rail", "statuses": [{"severity": 10, "description": "Good Service"}]},
            {"id": "c2c", "name": "c2c", "mode": "national-rail", "statuses": [{"severity": 0, "description": "Special Service", "reason": "https://x/y/",
                                                                          "nrIncident": {"summary": "CLEARED: lines reopened at Barking", "planned": False}}]},
            {"id": "jubilee", "name": "Jubilee", "mode": "tube", "statuses": [{"severity": 9, "description": "Minor Delays", "reason": "Minor delays due to a signal failure",
                                                                               "disruption": {"category": "RealTime"}}]}]}])
    put(d, "busLineStatus", DAY, [{"slot": f"{DAY}T09:00:00Z", "totalLines": 675, "goodServiceCount": 470, "disrupted": [
        {"id": "100", "name": "100", "statuses": [{"severity": 0, "description": "Special Service", "reason": "Road closed  due to water works. Buses diverted.",
                                                   "disruption": {"category": "PlannedWork"}}]}]}], gz=True)
    put(d, "liftDisruptions", DAY, [{"slot": f"{DAY}T09:00:00Z", "count": 2, "disruptions": [{"stationUniqueId": "A", "disruptedLiftUniqueIds": ["A-Lift-1", "A-Lift-2"], "message": "Bank: No Step Free Access - faulty lift"}]}])
    put(d, "stopDisruptions", DAY, [{"slot": f"{DAY}T09:00:00Z", "disruptions": [{"atcoCode": "X", "commonName": "Xstation", "description": "Lift closed", "type": "Information"},
        {"atcoCode": "Y", "commonName": "Ystation", "description": "Exit closed"}, {"atcoCode": "Y", "commonName": "Ystation", "description": "Exit closed"}]}])
    put(d, "arrivalsReadings", DAY, [{"slot": f"{DAY}T09:00:00Z", "stations": {"bank": {"totalPredictions": 49}}}])
    put(d, "arrivalsNetwork", DAY, [{"slot": f"{DAY}T09:00:00Z", "fields": ["naptanId", "lineId", "platform", "direction", "destination", "x", "timeToStation"],
        "modes": {"tube": [["S1", "bakerloo", "p", "o", "d", None, "60"], ["S2", "bakerloo", "p", "o", "d", None, "90"], ["S3", "central", "p", "o", "d", None, "30"]], "dlr": []}}], gz=True)
    put(d, "crowdingNetwork", DAY, [{"slot": f"{DAY}T09:00:00Z", "stations": {"940GZZLUBNK": {"dataAvailable": True, "percentageOfBaseline": 0.5}, "940GZZLUOVL": {"dataAvailable": False}}}], gz=True)
    put(d, "crowdingReadings", DAY, [{"slot": f"{DAY}T09:00:00Z", "stations": {"bank": {"dataAvailable": True, "percentageOfBaseline": 0.254},
                                                                              "oval": {"dataAvailable": False, "percentageOfBaseline": 0}}}])
    put(d, "nationalRailDepartures", DAY, [{"slot": f"{DAY}T09:00:00Z", "fields": ["std", "etd", "isCancelled", "operatorCode", "loadingPct"], "stations": {"VIC": {"services": [
        ["2026-10-25T09:00:00", "2026-10-25T09:00:00", False, "SN", 20], ["2026-10-25T09:05:00", "2026-10-25T09:12:00", False, "SN", 40],
        ["2026-10-25T09:10:00", None, True, "SE", None]]}, "CHX": {"services": None}}}], gz=True)
    put(d, "nationalRailIncidents", DAY, [{"slot": f"{DAY}T09:00:00Z", "count": 10, "planned": 7}], gz=True)
    put(d, "_health", DAY, [{"slot": f"{DAY}T09:00:00Z", "startedAt": f"{DAY}T09:00:18Z", "durationS": 249.4, "requests": 120, "retries": 2,
                             "http429": 0, "keyUsed": True, "nrStatus": {"200": 32, "500": 2},
                             "datasets": {"arrivalsNetwork": {"status": "ok"}, "crowdingNetwork": {"status": "failed"},
                                          "stationAccess": {"status": "ok"}}}])


class RollupTests(unittest.TestCase):
    def test_day_rollup_contents(self):
        with tempfile.TemporaryDirectory() as d:
            sample(d)
            built = rollup.update(d)
            self.assertEqual(built, [DAY])
            R = json.loads((Path(d) / "_rollup" / "day" / f"{DAY}.json").read_text())
            c = R["lines"]["circle"]
            self.assertEqual((c["n"], c["m"], c["c"][8], c["c"][9], c["c"][7]), ("Circle", "tube", 3, 3, None))
            self.assertEqual(c["r"], {"no service between A and B": [8, 9]})
            s = R["lines"]["southern"]
            self.assertEqual((s["c"][8], s["c"][9], s["r"]), (2, 0, {"Delays at Wivelsfield": [8]}))
            self.assertEqual((c["k"][8], c["k"][9], c["rk"]), ("p", "p", {"no service between A and B": "p"}))   # Part Closure, no category: planned
            self.assertEqual((s["k"][8], s["k"][9]), ("u", None))          # NR incident, not planned: unplanned
            self.assertEqual((R["lines"]["c2c"]["k"][9], R["lines"]["c2c"]["rk"]), ("i", {"CLEARED: lines reopened at Barking": "i"}))
            self.assertEqual(R["lines"]["jubilee"]["k"][9], "u")
            m = R["m"]
            self.assertEqual((m["busGood"][9], m["busTotal"][9], m["busGood"][8]), (470, 675, None))
            self.assertEqual((m["lifts"][9], m["liftStations"][9]), (2, 1))
            self.assertEqual((R["lf"]["A"]["n"], R["lf"]["A"]["o"][9], R["lf"]["A"]["r"]), ("Bank", 2, {"Bank: No Step Free Access - faulty lift": [9]}))
            self.assertEqual((R["sd"]["Y"]["n"], R["sd"]["Y"]["c"][9], R["sd"]["Y"]["r"]), ("Ystation", 2, {"Exit closed": [9]}))
            self.assertEqual((R["ar"]["bank"][9], R["am"]["tube"][9], R["am"]["dlr"][9], R["al"]["bakerloo"][9], R["al"]["central"][9]), (49, 3, 0, 2, 1))
            CN = json.loads((Path(d) / "_rollup" / "cn" / f"{DAY}.json").read_text())
            self.assertEqual((CN["s"]["940GZZLUBNK"][9], "940GZZLUOVL" in CN["s"]), (50.0, False))
            self.assertEqual((m["notices"][9], m["noticeStations"][9]), (3, 2))
            self.assertEqual((m["nrDeps"][9], m["nrCanc"][9], m["nrLate"][9]), (3, 1, 1))
            self.assertEqual((m["nrInc"][9], m["nrUnpl"][9]), (10, 3))
            self.assertEqual(R["crowd"], {"bank": [None] * 9 + [25.4] + [None] * 14})
            cov = R["cov"]
            self.assertEqual((cov["lineStatus"][8], cov["lineStatus"][9], cov["lineStatus"][10]), (1, 1, 0))
            self.assertEqual((cov["arrivalsNetwork"][9], cov["crowdingNetwork"][9]), (1, 0))
            self.assertEqual(cov["stationAccess"], 1)            # daily dataset: present that day
            self.assertEqual(cov["nationalRailStations"], 0)
            run = R["runs"][0]
            self.assertEqual((run["h"], run["t"], run["d"], run["q"], run["r"], run["x"], run["k"], run["nr"], run["bad"]),
                             (9, f"{DAY}T09:00:18Z", 249.4, 120, 2, 0, True, {"200": 32, "500": 2}, ["crowdingNetwork"]))
            self.assertGreater(R["bytes"]["lineStatus"], 0)
            self.assertNotIn("networkDisruptions", R["bytes"])       # no file that day -> no entry
            self.assertEqual((R["nrs"]["VIC"]["d"][9], R["nrs"]["VIC"]["c"][9], R["nrs"]["VIC"]["l"][9]), (3, 1, 1))
            B = json.loads((Path(d) / "_rollup" / "bus" / f"{DAY}.json").read_text())
            self.assertEqual((B["t"][9], B["g"][9]), (675, 470))
            self.assertEqual(B["routes"]["100"]["r"], {"Road closed due to water works. Buses diverted.": [9]})
            self.assertEqual((B["routes"]["100"]["k"][9], B["routes"]["100"]["rk"]["Road closed due to water works. Buses diverted."]), ("p", "p"))
            self.assertEqual(R["nrs"]["VIC"]["ld"][9], 30.0)
            self.assertEqual((R["nro"]["SN"]["d"][9], R["nro"]["SN"]["l"][9], R["nro"]["SE"]["c"][9]), (2, 1, 1))
            self.assertNotIn("CHX", R["nrs"])          # board unavailable that hour: no figures

    def test_nr_reason_counts(self):
        """Darwin reason codes are counted per station and operator, sparsely as {code: {hour: n}}:
        cancelReason on cancelled services, delayReason on running ones (whatever the delay)."""
        with tempfile.TemporaryDirectory() as d:
            put(d, "nationalRailDepartures", DAY, [
                {"slot": f"{DAY}T09:00:00Z",
                 "fields": ["std", "etd", "isCancelled", "operatorCode", "loadingPct", "cancelReason", "delayReason"],
                 "stations": {"VIC": {"services": [
                     ["2026-10-25T09:00:00", "2026-10-25T09:00:00", False, "SN", None, None, None],      # on time
                     ["2026-10-25T09:05:00", "2026-10-25T09:12:00", False, "SN", None, None, 681],       # 7 min late
                     ["2026-10-25T09:10:00", None, True, "SE", None, 901, None],                          # cancelled with a reason
                     ["2026-10-25T09:15:00", "2026-10-25T09:27:00", False, "SN", None, None, 681],       # 12 min late, same reason
                     ["2026-10-25T09:20:00", None, True, "SN", None, None, None],                         # cancelled, no reason given
                     ["2026-10-25T09:25:00", "2026-10-25T09:28:00", False, "SN", None, None, 820]]},     # 3 min late, reason given
                     "CHX": {"services": None}}},
                {"slot": f"{DAY}T10:00:00Z",
                 "fields": ["std", "etd", "isCancelled", "operatorCode", "loadingPct", "cancelReason", "delayReason"],
                 "stations": {"VIC": {"services": [
                     ["2026-10-25T10:05:00", "2026-10-25T10:20:00", False, "SN", None, None, 681],
                     ["2026-10-25T10:10:00", None, True, "SE", None, 901, None]]}}}], gz=True)
            rollup.update(d)
            R = json.loads((Path(d) / "_rollup" / "day" / f"{DAY}.json").read_text())
            V = R["nrs"]["VIC"]
            self.assertEqual(V["rl"], {"681": {"9": 2, "10": 1}, "820": {"9": 1}})
            self.assertEqual(V["rc"], {"901": {"9": 1, "10": 1}})
            self.assertEqual(R["nro"]["SN"]["rl"], {"681": {"9": 2, "10": 1}, "820": {"9": 1}})
            self.assertEqual(R["nro"]["SN"]["rc"], {})                    # SN's one cancellation had no reason code
            self.assertEqual(R["nro"]["SE"]["rc"], {"901": {"9": 1, "10": 1}})
            self.assertEqual(R["nro"]["SE"]["rl"], {})
            self.assertEqual((V["c"][9], V["d"][9]), (2, 6))              # existing figures unchanged
            self.assertNotIn("CHX", R["nrs"])

    def test_nr_boards_without_reason_fields_still_roll_up(self):
        """Records from before the reason fields existed (or without them) give empty reason maps, not errors."""
        with tempfile.TemporaryDirectory() as d:
            sample(d)
            rollup.update(d)
            R = json.loads((Path(d) / "_rollup" / "day" / f"{DAY}.json").read_text())
            self.assertEqual((R["nrs"]["VIC"]["rc"], R["nrs"]["VIC"]["rl"], R["nro"]["SN"]["rl"]), ({}, {}, {}))

    def test_rebuild_only_when_sources_change_and_never_removes_days(self):
        with tempfile.TemporaryDirectory() as d:
            sample(d)
            put(d, "lineStatus", "2026-10-24", [{"slot": "2026-10-24T23:00:00Z", "lines": []}])
            self.assertEqual(sorted(rollup.update(d)), ["2026-10-24", DAY])
            self.assertEqual(rollup.update(d), [])                       # nothing changed
            put(d, "lineStatus", DAY, [{"slot": f"{DAY}T10:00:00Z", "lines": []}])
            self.assertEqual(rollup.update(d), [DAY])                    # only the day that grew
            self.assertTrue((Path(d) / "_rollup" / "day" / "2026-10-24.json").exists())

    def test_index(self):
        with tempfile.TemporaryDirectory() as d:
            sample(d)
            rollup.update(d)
            idx = rollup.index(d)
            self.assertEqual((idx["days"], idx["first"], idx["v"]), ([DAY], DAY, rollup.VERSION))
            self.assertEqual(idx["firstSlot"], f"{DAY}T08:00:00Z")
            self.assertEqual((idx["covFirst"]["lineStatus"], idx["covFirst"]["busLineStatus"], idx["covFirst"]["stationAccess"]),
                             (f"{DAY}T08:00:00Z", f"{DAY}T09:00:00Z", f"{DAY}T00:00:00Z"))
            self.assertNotIn("nationalRailStations", idx["covFirst"])
            self.assertGreater(idx["bytes"]["lineStatus"], 0)
            self.assertEqual(idx["goLive"], "2026-10-24T21:00:00Z")
            L = idx["live"]
            self.assertEqual(L["through"], f"{DAY}T09:00:00Z")
            self.assertEqual(L["byDs"]["lineStatus"], [2, 2])            # 08:00 and 09:00 recorded
            self.assertEqual(L["byDs"]["arrivalsNetwork"], [1, 1])
            self.assertNotIn("crowdingNetwork", L["byDs"])              # never recorded yet: not counted as missing
            self.assertEqual(L["streak"], 2)                             # 08:00 and 09:00 complete for every started dataset


if __name__ == "__main__":
    unittest.main()
