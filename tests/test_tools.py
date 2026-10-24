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

    def test_full_rebuild_refuses_on_live_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "lineStatus"
            data.mkdir(parents=True)
            (data / "2026-10-24.ndjson").write_text('{"slot":"2026-10-24T18:00:00Z","polledAt":"x"}\n')
            export_doc(tmp, "lineStatus", "reading-a", "2026-10-24T17:25:10Z")
            r = run(BACKFILL, "--export", Path(tmp) / "export", "--out", Path(tmp) / "data")
            self.assertEqual(r.returncode, 2)
            self.assertEqual((data / "2026-10-24.ndjson").read_text(), '{"slot":"2026-10-24T18:00:00Z","polledAt":"x"}\n')


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
