import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKFILL = ROOT / "tools" / "backfill_from_export.py"
AGG = ROOT / "collector" / "aggregate.py"


def export_doc(tmp, ds, doc_id, polled):
    d = Path(tmp) / "export" / ds
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc_id}.json").write_text(json.dumps({"polledAt": polled, "source": "x", "count": 0, "disruptions": []}))


def run(*args, env=None):
    return subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True, text=True, env=env)


class AppendMissingTests(unittest.TestCase):
    def test_appends_only_missing_and_never_rewrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "liftDisruptions"
            data.mkdir(parents=True)
            live = '{"slot":"2026-10-24T18:00:00Z","polledAt":"2026-10-24T18:00:02Z","authMode":"app-key"}\n'
            (data / "2026-10-24.ndjson").write_text(live)
            export_doc(tmp, "liftDisruptions", "reading-a", "2026-10-24T17:25:10Z")   # missing hour -> appended
            export_doc(tmp, "liftDisruptions", "reading-b", "2026-10-24T18:25:10Z")   # hour present -> skipped
            r = run(BACKFILL, "--export", Path(tmp) / "export", "--out", Path(tmp) / "data", "--append-missing")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            lines = (data / "2026-10-24.ndjson").read_text().splitlines()
            self.assertEqual(lines[0] + "\n", live)                     # original line untouched
            self.assertEqual([json.loads(l)["slot"] for l in lines], ["2026-10-24T18:00:00Z", "2026-10-24T17:00:00Z"])
            self.assertEqual(json.loads(lines[1])["legacyDocId"], "reading-a")
            r2 = run(BACKFILL, "--export", Path(tmp) / "export", "--out", Path(tmp) / "data", "--append-missing")
            self.assertIn("0 records appended", r2.stdout)              # idempotent
            self.assertEqual(len((data / "2026-10-24.ndjson").read_text().splitlines()), 2)

    def test_gzip_dataset_topped_up_in_its_own_gz_file(self):
        import gzip
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "busLineStatus"
            data.mkdir(parents=True)
            gz = data / "2026-10-24.ndjson.gz"
            gz.write_bytes(gzip.compress(b'{"slot":"2026-10-24T18:00:00Z","polledAt":"2026-10-24T18:00:02Z"}\n'))
            export_doc(tmp, "busLineStatus", "reading-a", "2026-10-24T18:25:10Z")   # present (inside .gz) -> skipped
            export_doc(tmp, "busLineStatus", "reading-b", "2026-10-24T19:25:10Z")   # missing -> appended to .gz
            r = run(BACKFILL, "--export", Path(tmp) / "export", "--out", Path(tmp) / "data", "--append-missing")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse((data / "2026-10-24.ndjson").exists())     # no plain twin beside the .gz
            slots = [json.loads(l)["slot"] for l in gzip.open(gz, "rt") if l.strip()]
            self.assertEqual(slots, ["2026-10-24T18:00:00Z", "2026-10-24T19:00:00Z"])

    def test_full_rebuild_refuses_on_live_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "lineStatus"
            data.mkdir(parents=True)
            (data / "2026-10-24.ndjson").write_text('{"slot":"2026-10-24T18:00:00Z","polledAt":"x"}\n')
            export_doc(tmp, "lineStatus", "reading-a", "2026-10-24T17:25:10Z")
            r = run(BACKFILL, "--export", Path(tmp) / "export", "--out", Path(tmp) / "data")
            self.assertEqual(r.returncode, 2)
            self.assertEqual((data / "2026-10-24.ndjson").read_text(), '{"slot":"2026-10-24T18:00:00Z","polledAt":"x"}\n')


class LiftBoardTests(unittest.TestCase):
    def test_stoplight_counts_working_out_of_total(self):
        import io, zipfile
        sys.path.insert(0, str(ROOT / "collector")); import aggregate
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "_reference" / "tfl-stationdata"; ref.mkdir(parents=True)
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w") as z:
                z.writestr("Lifts.csv", "StationUniqueId,LiftUniqueId,FromAreas,ToAreas\n"
                           "ONE,ONE-Lift-1,St,Pl\nFOUR,FOUR-Lift-1,St,P1\nFOUR,FOUR-Lift-2,St,P2\nFOUR,FOUR-Lift-3,St,P3\nFOUR,FOUR-Lift-4,St,P4\n"
                           "OK,OK-Lift-1,St,Pl\nOK,OK-Lift-2,St,Pl\n")
                z.writestr("Stations.csv", "UniqueId,Name\nONE,One Lift\nFOUR,Four Lifts\nOK,All Fine\n")
            (ref / "x.zip").write_bytes(b.getvalue())
            (Path(tmp) / "stationAccess").mkdir()
            (Path(tmp) / "stationAccess" / "2026-10-24.ndjson").write_text(
                json.dumps({"slot": "2026-10-24T03:00:00Z", "polledAt": "x", "file": "_reference/tfl-stationdata/x.zip"}) + "\n")
            cur = {"disruptions": [
                {"stationUniqueId": "FOUR", "disruptedLiftUniqueIds": ["FOUR-Lift-2"], "message": "Four Lifts: No Step Free Access - Step free access is not available to platform 2."},
                {"stationUniqueId": "ONE", "disruptedLiftUniqueIds": ["ONE-Lift-1"], "message": "One Lift: lift out"}]}
            runs = {"ONE-Lift-1": {"start": "2026-10-24T05:00:00Z", "last": "2026-10-24T09:00:00Z"}}
            B = aggregate.lift_board(tmp, cur, runs, "2026-10-24T09:00:00Z", lambda s: s, ["OK"])
            got = [(x["name"], x["colour"], x["working"], x["total"], x["stepFreeLost"]) for x in B["stations"]]
            self.assertEqual(got, [("One Lift", "red", 0, 1, False), ("Four Lifts", "amber", 3, 4, True), ("All Fine", "green", 2, 2, False)])
            self.assertEqual(B["stations"][0]["since"], "2026-10-24T05:00:00Z")
            self.assertEqual(B["counts"], {"red": 1, "amber": 1, "green": 1})


class AggregateApiTests(unittest.TestCase):
    def test_api_files_and_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data" / "lineStatus"
            d.mkdir(parents=True)
            rec = {"slot": "2026-10-24T18:00:00Z", "polledAt": "2026-10-24T18:00:01Z", "lines": [
                {"id": "central", "name": "Central", "mode": "tube", "statuses": [{"severity": 10, "description": "Good Service"}]},
                {"id": "district", "name": "District", "mode": "tube", "statuses": [{"severity": 6, "description": "Severe Delays", "reason": "Signal failure"}]}]}
            (d / "2026-10-24.ndjson").write_text(json.dumps(rec) + "\n")
            env = dict(os.environ, GITHUB_REPOSITORY="Someone/london-walkabout")
            r = run(AGG, "--data", Path(tmp) / "data", "--out", Path(tmp) / "docs", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            api = Path(tmp) / "docs" / "api" / "v1"
            latest = json.loads((api / "latest.json").read_text())
            self.assertEqual(latest["datasets"]["lineStatus"]["slot"], "2026-10-24T18:00:00Z")
            man = json.loads((api / "manifest.json").read_text())
            self.assertEqual(man["rawDataBase"], "https://raw.githubusercontent.com/Someone/london-walkabout/main/data/")
            self.assertEqual(man["pagesBase"], "https://someone.github.io/london-walkabout/")
            self.assertEqual(man["datasets"]["lineStatus"]["days"], ["2026-10-24"])
            S = json.loads((Path(tmp) / "docs" / "summary.json").read_text())
            self.assertEqual(S["now"]["lines"][0]["name"], "District")      # worst first
            self.assertEqual(S["now"]["lines"][0]["reason"], "Signal failure")
            net = [o for o in S["observations"] if o["key"] == "network"][0]
            self.assertEqual(net["value"], "1/2")
            self.assertIn("District (Severe Delays)", net["text"])


if __name__ == "__main__":
    unittest.main()
