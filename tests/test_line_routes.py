import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_line_routes", ROOT / "tools" / "fetch_line_routes.py")
flr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flr)


def sp(i, name=None, lat=51.5, lon=-0.1):
    return {"id": i, "name": name or ("Stop " + i), "lat": lat, "lon": lon}


def resp(line_id, name, branches, mode="tube"):
    return {"lineId": line_id, "lineName": name, "mode": mode,
            "stopPointSequences": [{"branchId": n, "stopPoint": b} for n, b in enumerate(branches)]}


class BuildTests(unittest.TestCase):
    def test_builds_stop_table_and_branch_id_lists(self):
        r = resp("jubilee", "Jubilee", [[sp("A"), sp("B"), sp("C")]])
        out = flr.build({"jubilee": [r]})
        self.assertEqual(out["lines"]["jubilee"]["b"], [["A", "B", "C"]])
        self.assertEqual(out["lines"]["jubilee"]["n"], "Jubilee")
        self.assertEqual(out["stops"]["B"], ["Stop B", 51.5, -0.1])

    def test_a_branch_and_its_reverse_are_kept_once(self):
        fwd = resp("x", "X", [[sp("A"), sp("B"), sp("C")]])
        back = resp("x", "X", [[sp("C"), sp("B"), sp("A")]])
        out = flr.build({"x": [fwd, back]})
        self.assertEqual(len(out["lines"]["x"]["b"]), 1)

    def test_rows_without_coordinates_or_id_are_skipped(self):
        r = resp("x", "X", [[sp("A"), {"id": "B", "name": "No coords"}, {"name": "No id", "lat": 1, "lon": 1}, sp("C")]])
        out = flr.build({"x": [r]})
        self.assertEqual(out["lines"]["x"]["b"], [["A", "C"]])
        self.assertNotIn("B", out["stops"])

    def test_single_stop_branches_are_dropped(self):
        out = flr.build({"x": [resp("x", "X", [[sp("A")], [sp("A"), sp("B")]])]})
        self.assertEqual(out["lines"]["x"]["b"], [["A", "B"]])

    def test_coordinates_are_rounded(self):
        out = flr.build({"x": [resp("x", "X", [[sp("A", lat=51.123456789, lon=-0.987654321), sp("B")]])]})
        self.assertEqual(out["stops"]["A"][1:], [51.12346, -0.98765])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config" / "lineRoutes.json"

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, n=12, extra=None):
        d = {}
        for k in range(n):
            br = [sp(f"L{k}A"), sp(f"L{k}B"), sp(f"L{k}C")]
            d[f"line{k}"] = [resp(f"line{k}", f"Line {k}", [br])]
        if extra:
            d.update(extra)
        return d

    def test_first_write_then_unchanged_does_not_rewrite(self):
        self.assertTrue(flr.update(self.path, self.feed(), "2026-10-25"))
        before = self.path.read_text()
        self.assertFalse(flr.update(self.path, self.feed(), "2026-11-01"))
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(json.loads(before)["fetched"], "2026-10-25")

    def test_withdrawn_lines_and_branches_are_kept_new_ones_added(self):
        flr.update(self.path, self.feed(), "2026-10-25")
        feed = self.feed(11)  # line11 disappears
        feed["new"] = [resp("new", "New", [[sp("N1"), sp("N2")]])]
        self.assertTrue(flr.update(self.path, feed, "2026-11-01"))
        j = json.loads(self.path.read_text())
        self.assertIn("line11", j["lines"])
        self.assertIn("new", j["lines"])
        self.assertEqual(j["fetched"], "2026-11-01")

    def test_too_few_lines_refused_and_nothing_written(self):
        with self.assertRaises(ValueError):
            flr.update(self.path, self.feed(3), "2026-10-25")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
