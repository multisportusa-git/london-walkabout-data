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
            {"id": "southern", "name": "Southern", "mode": "national-rail", "statuses": [{"severity": 10, "description": "Good Service"}]}]}])
    put(d, "busLineStatus", DAY, [{"slot": f"{DAY}T09:00:00Z", "totalLines": 675, "goodServiceCount": 470, "disrupted": []}], gz=True)
    put(d, "liftDisruptions", DAY, [{"slot": f"{DAY}T09:00:00Z", "count": 2, "disruptions": [{"stationUniqueId": "A"}, {"stationUniqueId": "A"}]}])
    put(d, "stopDisruptions", DAY, [{"slot": f"{DAY}T09:00:00Z", "disruptions": [{"atcoCode": "X"}, {"atcoCode": "Y"}, {"atcoCode": "Y"}]}])
    put(d, "crowdingReadings", DAY, [{"slot": f"{DAY}T09:00:00Z", "stations": {"bank": {"dataAvailable": True, "percentageOfBaseline": 0.254},
                                                                              "oval": {"dataAvailable": False, "percentageOfBaseline": 0}}}])
    put(d, "nationalRailDepartures", DAY, [{"slot": f"{DAY}T09:00:00Z", "fields": ["std", "etd", "isCancelled"], "stations": {"VIC": {"services": [
        ["2026-10-25T09:00:00", "2026-10-25T09:00:00", False], ["2026-10-25T09:05:00", "2026-10-25T09:12:00", False],
        ["2026-10-25T09:10:00", None, True]]}}}], gz=True)
    put(d, "nationalRailIncidents", DAY, [{"slot": f"{DAY}T09:00:00Z", "count": 10, "planned": 7}], gz=True)
    put(d, "_health", DAY, [{"slot": f"{DAY}T09:00:00Z", "datasets": {"arrivalsNetwork": {"status": "ok"}, "crowdingNetwork": {"status": "failed"},
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


if __name__ == "__main__":
    unittest.main()
