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


if __name__ == "__main__":
    unittest.main()
