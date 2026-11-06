"""Offline regression tests. No network, GitHub secrets or Sheets required."""
import os
import sys
import types
import unittest
from unittest.mock import patch

# The production workflow installs requirements.txt. These regression tests
# can still exercise pure routing/query logic on an offline machine without
# those HTTP/PDF packages. Network functions are never called by this suite.
try:
    import requests  # noqa: F401
except ImportError:
    req = types.ModuleType("requests")
    req.RequestException = OSError
    sys.modules["requests"] = req
try:
    import urllib3  # noqa: F401
except ImportError:
    u3 = types.ModuleType("urllib3")
    u3.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
    u3.disable_warnings = lambda *_: None
    sys.modules["urllib3"] = u3
try:
    import bs4  # noqa: F401
except ImportError:
    bs = types.ModuleType("bs4")
    bs.BeautifulSoup = lambda *_args, **_kwargs: None
    sys.modules["bs4"] = bs

import radar
from adapters import _is_vendor_domain
from adapters.aggregator_tenders import PipeworxAdapter
from adapters.hybrid_intelligence import HybridIntelligenceAdapter
from common import SearchEngine
from event_fingerprint import fingerprint_key
from standard_lead import StandardLead


class FakeSheet:
    def __init__(self):
        self.calls = []

    def post(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"status": "success"}


def lead(source="search", category="news", org="Apex Infrastructure Ltd"):
    return StandardLead(
        source=source, category=category,
        url="https://example.org/news/notice-26",
        title="Apex Infrastructure expands its design team",
        buyer_org=org, country="India",
        raw_text="Apex Infrastructure Ltd announces a new design center in Delhi.",
        metadata={"target_product": "engineering tools"},
    )


def verdict(role="BUYER", valid=True, confidence="MEDIUM", score=75):
    return dict(
        is_valid=valid, entity_role=role, confidence=confidence,
        organization="Apex Infrastructure Ltd",
        genuineness="GENUINE", expiry_status="UNKNOWN",
        signal_freshness="FRESH", lead_score=score,
        why_engage_now="Employer announcement", event_fingerprint={},
    )


class RadarLogicTests(unittest.TestCase):
    def setUp(self):
        self.orig_dry_run = radar.DRY_RUN
        radar.DRY_RUN = True

    def tearDown(self):
        radar.DRY_RUN = self.orig_dry_run

    def test_query_operators_are_preserved(self):
        q = 'site:gov.in ("design tools" OR "GIS") -training'
        self.assertEqual(SearchEngine.clean_query(q), q)

    def test_no_static_autodesk_vendor_classification(self):
        self.assertFalse(_is_vendor_domain("https://autodesk.com/news/customer-story"))
        self.assertFalse(_is_vendor_domain("https://nsearchives.nseindia.com/filing.pdf"))

    def test_indirect_hiring_does_not_promote_on_score(self):
        l = lead(category="hiring")
        v = verdict(confidence="LOW", score=99)
        v["genuineness"] = "HIRING_SIGNAL"
        o, _, _ = radar.Router(FakeSheet(), ["India"]).push(l, v, "Tools", "Engineering")
        self.assertEqual(o, "REVIEW")

    def test_verified_direct_buyer_to_inbox(self):
        l = lead(category="tender")
        v = verdict(confidence="HIGH", score=91)
        o, _, _ = radar.Router(FakeSheet(), ["India"]).push(l, v, "Tools", "Engineering")
        self.assertEqual(o, "INBOX")

    def test_low_confidence_buyer_stays_review(self):
        l = lead(category="news")
        v = verdict(confidence="LOW", score=96)
        o, _, _ = radar.Router(FakeSheet(), ["India"]).push(l, v, "Tools", "Engineering")
        self.assertEqual(o, "REVIEW")

    def test_invalid_seller_is_not_partner(self):
        l = lead(category="news")
        v = verdict(role="SELLER", valid=False)
        o, _, _ = radar.Router(FakeSheet(), ["India"]).push(l, v, "Tools", "Engineering")
        self.assertEqual(o, "TRASH")

    def test_valid_seller_is_partner(self):
        l = lead(category="news")
        v = verdict(role="SELLER", valid=True)
        o, _, _ = radar.Router(FakeSheet(), ["India"]).push(l, v, "Tools", "Engineering")
        self.assertEqual(o, "PARTNER")

    def test_same_company_distinct_projects_not_collapsed(self):
        base = {"event_type": "project", "primary_actor": "Apex Infrastructure",
                "rough_date": "2026-11", "location_key": "Delhi"}
        self.assertEqual(fingerprint_key(base), "")
        self.assertNotEqual(
            fingerprint_key({**base, "project_key": "North Delhi Office"}),
            fingerprint_key({**base, "project_key": "South Delhi Campus"}),
        )

    def test_round_robin_category_and_adapter_fairness(self):
        ls = [lead("pipeworx", "tender") for _ in range(40)]
        ls += [lead("serper", "tender") for _ in range(5)]
        ls += [lead("hybrid", "hiring") for _ in range(5)]
        ls += [lead("hybrid", "expansion") for _ in range(5)]
        ls += [lead("news", "news") for _ in range(5)]
        result = radar.prioritize_candidates(ls)
        self.assertEqual(len(result), len(ls))
        self.assertIn("hiring", {l.category for l in result[:8]})
        self.assertIn("expansion", {l.category for l in result[:8]})
        self.assertIn("news", {l.category for l in result[:8]})
        self.assertIn("serper", {l.source for l in result[:8]})

    def test_hybrid_uses_targets_not_full_special_prompt(self):
        calls = []

        class FakeSearch:
            def search(self, query, country="", pages=1, recent=False):
                calls.append(query)
                return [{"link": "https://example.org/" + str(len(calls)),
                         "title": "Bid notice", "snippet": "Named buyer needs service"}]

        cfg = {"targets": ["laboratory equipment"], "industries": ["healthcare"],
               "special_prompt": "Do not include firms in sector Z; also do ten other tasks",
               "countries": ["India"], "states": []}
        with patch.dict(os.environ, {"RADAR_HYBRID_ENABLED": "1", "RADAR_V32_MAX_QUERIES": "8"}):
            with patch("adapters.hybrid_intelligence.SearchEngine", FakeSearch):
                a = HybridIntelligenceAdapter()
                self.assertTrue(a.is_enabled(cfg))
                result = a.fetch(cfg)
        self.assertEqual(len(calls), 8)
        self.assertTrue(result)
        self.assertFalse(any("Do not include firms" in q for q in calls))
        self.assertTrue({"tender", "hiring", "expansion", "news"}.issubset(
            {l.category for l in result}))

    def test_pipeworx_queries_configured_targets_only(self):
        a = PipeworxAdapter()
        calls = []

        def fake_call(name, args):
            calls.append((name, args))
            return {"tenders": []}

        a._call_tool = fake_call
        with patch.dict(os.environ, {"RADAR_PIPEWORX_DEFENCE_FEED": "0"}):
            self.assertEqual(a.fetch({"targets": ["surgical lamps", "hospital pumps"]}), [])
        self.assertEqual(len(calls), 2)
        self.assertEqual([c[1]["query"] for c in calls], ["surgical lamps", "hospital pumps"])

    def test_pipeworx_preserves_existing_tender_url(self):
        a = PipeworxAdapter()
        original = "https://etenders.gov.in/eprocure/app?page=detail&tid=2026_ABC_12"
        item = a._to_lead({"source_url": original, "tender_id": "2026_ABC_12", "title": "Buy pumps"})
        self.assertEqual(item.url, original)


if __name__ == "__main__":
    unittest.main()
