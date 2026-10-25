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
    put(d, "busLineStatus", DAY, [{"slot": f"{DAY}T09:00:00Z", "totalLines": 675, "goodServiceCount": 470, "disrupted": []}], gz=True)
    put(d, "liftDisruptions", DAY, [{"slot": f"{DAY}T09:00:00Z", "count": 2, "disruptions": [{"stationUniqueId": "A"}, {"stationUniqueId": "A"}]}])
    put(d, "stopDisruptions", DAY, [{"slot": f"{DAY}T09:00:00Z", "disruptions": [{"atcoCode": "X"}, {"atcoCode": "Y"}, {"atcoCode": "Y"}]}])
    put(d, "crowdingReadings", DAY, [{"slot": f"{DAY}T09:00:00Z", "stations": {"bank": {"dataAvailable": True, "percentageOfBaseline": 0.254},
                                                                              "oval": {"dataAvailable": False, "percentageOfBaseline": 0}}}])
    put(d, "nationalRailDepartures", DAY, [{"slot": f"{DAY}T09:00:00Z", "fields": ["std", "etd", "isCancelled"], "stations": {"VIC": {"services": [
        ["2026-10-25T09:00:00", "2026-10-25T09:00:00", False], ["2026-10-25T09:05:00", "2026-10-25T09:12:00", False],
        ["2026-10-25T09:10:00", None, True]]}}}], gz=True)
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
            self.assertNotIn("arrivalsNetwork", R["bytes"])          # no file that day -> no entry

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
