import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_nr_reasons", ROOT / "tools" / "fetch_nr_reasons.py")
fnr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fnr)


def item(code, late="delayed", canc="cancelled"):
    return {"code": code, "lateReason": late, "cancReason": canc, "Xmlns": {"Count": 2}}


class NormaliseTests(unittest.TestCase):
    def test_maps_codes_to_texts_with_string_keys(self):
        out = fnr.normalise([item(501, "L1", "C1"), item(502, "L2", "C2")])
        self.assertEqual(out, {"canc": {"501": "C1", "502": "C2"}, "late": {"501": "L1", "502": "L2"}})

    def test_skips_rows_without_a_usable_code_and_blank_texts(self):
        out = fnr.normalise([item(None), {"lateReason": "x"}, item(5, "  ", "C5"), "junk", item(6, "L6", None)])
        self.assertEqual(out, {"canc": {"5": "C5"}, "late": {"6": "L6"}})

    def test_rejects_a_non_list_response(self):
        with self.assertRaises(ValueError):
            fnr.normalise({"Message": "Unexpected server error"})


class MergeTests(unittest.TestCase):
    def test_new_texts_win_but_old_codes_are_never_dropped(self):
        old = {"canc": {"1": "old1", "2": "old2"}, "late": {"1": "oldl1"}}
        new = {"canc": {"1": "new1"}, "late": {"3": "new3"}}
        self.assertEqual(fnr.merge(old, new),
                         {"canc": {"1": "new1", "2": "old2"}, "late": {"1": "oldl1", "3": "new3"}})

    def test_merge_with_nothing_before(self):
        new = {"canc": {"1": "a"}, "late": {"1": "b"}}
        self.assertEqual(fnr.merge({}, new), new)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config" / "nrReasonCodes.json"

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, n):
        return [item(500 + i, f"L{i}", f"C{i}") for i in range(n)]

    def test_writes_first_time_with_fetched_date(self):
        changed = fnr.update(self.path, self.rows(150), "2026-10-25")
        self.assertTrue(changed)
        j = json.loads(self.path.read_text())
        self.assertEqual(j["fetched"], "2026-10-25")
        self.assertEqual(len(j["canc"]), 150)
        self.assertEqual(j["canc"]["500"], "C0")

    def test_unchanged_content_does_not_rewrite_or_bump_date(self):
        fnr.update(self.path, self.rows(150), "2026-10-25")
        before = self.path.read_text()
        self.assertFalse(fnr.update(self.path, self.rows(150), "2026-11-01"))
        self.assertEqual(self.path.read_text(), before)

    def test_changed_text_rewrites_and_keeps_removed_codes(self):
        fnr.update(self.path, self.rows(150), "2026-10-25")
        rows = self.rows(149)[1:]  # code 500 disappears from the feed
        rows[0] = item(501, "LNEW", "CNEW")
        self.assertTrue(fnr.update(self.path, rows, "2026-11-01"))
        j = json.loads(self.path.read_text())
        self.assertEqual(j["fetched"], "2026-11-01")
        self.assertEqual(j["canc"]["501"], "CNEW")
        self.assertIn("500", j["canc"])  # never pruned

    def test_implausibly_small_feed_is_refused_and_nothing_written(self):
        with self.assertRaises(ValueError):
            fnr.update(self.path, self.rows(5), "2026-10-25")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
