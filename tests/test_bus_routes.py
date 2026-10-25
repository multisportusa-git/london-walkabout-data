import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_bus_routes", ROOT / "tools" / "fetch_bus_routes.py")
fbr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fbr)


def rows(n, start=1):
    return [{"id": str(i), "name": str(i), "modeName": "bus"} for i in range(start, start + n)]


class RoutesTests(unittest.TestCase):
    def test_ids_are_unique_sorted_naturally_and_non_bus_or_blank_rows_skipped(self):
        data = [{"id": "N29"}, {"id": "100"}, {"id": "9"}, {"id": "9"}, {"id": " "}, {"id": None}, "junk", {"id": "C1"}]
        self.assertEqual(fbr.route_ids(data), ["9", "100", "C1", "N29"])

    def test_non_list_is_rejected(self):
        with self.assertRaises(ValueError):
            fbr.route_ids({"message": "error"})


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config" / "busRoutes.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_write_has_routes_and_date(self):
        self.assertTrue(fbr.update(self.path, rows(400), "2026-10-25"))
        j = json.loads(self.path.read_text())
        self.assertEqual(j["fetched"], "2026-10-25")
        self.assertEqual(len(j["routes"]), 400)

    def test_unchanged_does_not_rewrite(self):
        fbr.update(self.path, rows(400), "2026-10-25")
        before = self.path.read_text()
        self.assertFalse(fbr.update(self.path, rows(400), "2026-11-01"))
        self.assertEqual(self.path.read_text(), before)

    def test_withdrawn_routes_are_kept_and_new_ones_added(self):
        fbr.update(self.path, rows(400), "2026-10-25")
        self.assertTrue(fbr.update(self.path, rows(399, 2) + [{"id": "X99"}], "2026-11-01"))  # route 1 gone, X99 new
        j = json.loads(self.path.read_text())
        self.assertIn("1", j["routes"])
        self.assertIn("X99", j["routes"])
        self.assertEqual(j["fetched"], "2026-11-01")

    def test_small_feed_refused(self):
        with self.assertRaises(ValueError):
            fbr.update(self.path, rows(10), "2026-10-25")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
