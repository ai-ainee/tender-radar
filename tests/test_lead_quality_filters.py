
"""Offline regression tests for Radar Scout lead-quality filters."""

import unittest

from standard_lead import StandardLead
from radar import _low_value_tender_result, _low_value_browse_result, _low_value_signal_result


def make_lead(category, url, **kwargs):
    return StandardLead(
        source="quality_test",
        category=category,
        url=url,
        **kwargs
    )


class TestLeadQualityFilters(unittest.TestCase):

    def test_generic_firsttender_category_rejected(self):
        lead = make_lead(
            "tender",
            "https://www.firsttender.com/tenders/engineering-software",
            title="Engineering software tenders in India"
        )
        self.assertTrue(_low_value_tender_result(lead))

    def test_generic_tenderdetail_results_rejected(self):
        lead = make_lead(
            "tender",
            "https://www.tenderdetail.com/tenders/auto-cad-tenders",
            title="AutoCAD tenders list"
        )
        self.assertTrue(_low_value_tender_result(lead))

    def test_structured_tender_id_protected(self):
        lead = make_lead(
            "tender",
            "https://www.firsttender.com/tenders/engineering-software",
            tender_id="GEM/2026/B/1234567"
        )
        self.assertEqual(_low_value_tender_result(lead), "")

    def test_named_buyer_protected(self):
        lead = make_lead(
            "tender",
            "https://www.firsttender.com/tenders/engineering-software",
            buyer_org="Example Infrastructure Ltd"
        )
        self.assertEqual(_low_value_tender_result(lead), "")

    def test_specific_tender_detail_protected(self):
        lead = make_lead(
            "tender",
            "https://example.gov.in/tender/details/12345",
            title="Supply of engineering design software"
        )
        self.assertEqual(_low_value_tender_result(lead), "")

    def test_firsttender_bid_category_rejected(self):
        lead = make_lead("tender", "https://www.firsttender.com/bids/autocad-drafting-tenders.html")
        self.assertEqual(_low_value_tender_result(lead), "firsttender-category-listing")

    def test_firsttender_bid_category_with_id_preserved(self):
        lead = make_lead("tender", "https://www.firsttender.com/bids/autocad-drafting-tenders.html",
                         tender_id="GEM/2026/B/1234567")
        self.assertEqual(_low_value_tender_result(lead), "")

    def test_tenderdetail_category_rejected(self):
        lead = make_lead("tender", "https://www.tenderdetail.com/Indian-tender/autocad-tenders")
        self.assertTrue(_low_value_tender_result(lead))

    def test_tendernews_category_rejected(self):
        lead = make_lead("tender", "https://www.tendernews.com/tenders/latest-tender/autocad-drawing.html")
        self.assertTrue(_low_value_tender_result(lead))

    def test_tenderdetail_specific_notice_preserved(self):
        lead = make_lead("tender", "https://www.tenderdetail.com/Indian-Tenders/TenderNotice/123456")
        self.assertEqual(_low_value_tender_result(lead), "")

    def test_unattributed_homepage_rejected(self):
        lead = make_lead("tender", "https://techzoidtechnologies.com/")
        self.assertTrue(_low_value_browse_result(lead))

    def test_named_buyer_homepage_preserved(self):
        lead = make_lead("tender", "https://example.org/", buyer_org="Example Ltd")
        self.assertEqual(_low_value_browse_result(lead), "")

    def test_general_information_not_tender(self):
        lead = make_lead("tender", "https://www.india.gov.in/explore-india/facts-of-india/india-at-a-glance")
        self.assertTrue(_low_value_browse_result(lead))

    def test_misclassified_tender_directory_rejected(self):
        lead = make_lead("news", "https://www.tenderdetail.com/Indian-tender/autocad-tenders")
        self.assertTrue(_low_value_browse_result(lead))

    def test_news_article_preserved(self):
        lead = make_lead("news", "https://example.org/companies/company-wins-project")
        self.assertEqual(_low_value_browse_result(lead), "")

    def test_generic_topic_page_rejected(self):
        lead = make_lead(
            "expansion",
            "https://example.com/topic/construction",
            title="Construction industry news"
        )
        self.assertTrue(_low_value_browse_result(lead))

    def test_named_company_expansion_protected(self):
        lead = make_lead(
            "expansion",
            "https://example.com/news/new-factory",
            title="Example Manufacturing announces new factory",
            buyer_org="Example Manufacturing"
        )
        self.assertEqual(_low_value_browse_result(lead), "")



    def test_v3_encyclopedia_rejected(self):
        lead = make_lead("expansion", "https://www.britannica.com/place/India", title="India")
        self.assertEqual(_low_value_signal_result(lead), "encyclopedia-reference")

    def test_v3_autodesk_tutorial_rejected(self):
        lead = make_lead("news", "https://www.autodesk.com/learn/ondemand", title="AutoCAD tutorial")
        self.assertEqual(_low_value_signal_result(lead), "vendor-product-content")

    def test_v3_sports_franchise_not_hiring(self):
        lead = make_lead("hiring", "https://example.org/sports/ipl", title="IPL cricket franchise adds new team")
        self.assertEqual(_low_value_signal_result(lead), "sports-not-business-signal")

    def test_v3_generic_hiring_without_evidence_rejected(self):
        lead = make_lead("hiring", "https://example.org/news/industry", title="Industry overview")
        self.assertEqual(_low_value_signal_result(lead), "hiring-without-recruitment-evidence")

    def test_v3_named_hiring_preserved(self):
        lead = make_lead("hiring", "https://example.org/news/jobs", title="Example Industries hiring engineers", buyer_org="Example Industries")
        self.assertEqual(_low_value_signal_result(lead), "")

    def test_v3_expansion_article_preserved(self):
        lead = make_lead("expansion", "https://example.org/news/new-plant", title="Example Industries opens manufacturing plant")
        self.assertEqual(_low_value_signal_result(lead), "")

    def test_v3_tender_id_preserved(self):
        lead = make_lead("tender", "https://help.autodesk.com/article", tender_id="GEM/2026/B/1234567")
        self.assertEqual(_low_value_signal_result(lead), "")

    def test_v3_actual_job_board_post_preserved(self):
        lead = make_lead("hiring", "https://www.linkedin.com/jobs/view/1234567", title="Civil Engineer")
        self.assertEqual(_low_value_signal_result(lead), "")

    def test_v31_country_tender_directories_rejected(self):
        urls = (
            "https://www.globaltenders.com/india-tenders",
            "https://freetender.net/tenders/india",
            "https://tender84.com/etenders/",
            "https://www.tendersontime.com/india-tenders/infrastructure-tenders/",
            "https://etenders.gov.in/eprocure/app?page=FrontEndTendersByLocation&service=page",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(_low_value_tender_result(make_lead("tender", url)))

    def test_v31_named_buyer_or_id_keeps_tender(self):
        url = "https://www.globaltenders.com/india-tenders"
        self.assertEqual(_low_value_tender_result(make_lead("tender", url, buyer_org="Example Ltd")), "")
        self.assertEqual(_low_value_tender_result(make_lead("tender", url, tender_id="GEM/2026/B/1234567")), "")

    def test_v31_detail_tender_remains(self):
        self.assertEqual(_low_value_tender_result(make_lead("tender", "https://tender84.com/tender/123456")), "")

    def test_v31_educational_article_rejected(self):
        lead = make_lead("expansion", "https://www.kaarwan.com/blog/architecture/revit-architecture-2026-salary-analysis", title="Revit learning salary guide")
        self.assertEqual(_low_value_signal_result(lead), "educational-career-content")

    def test_v31_market_report_not_expansion(self):
        lead = make_lead("expansion", "https://example.com/news/india-ai-report", title="India leads globally in planned AI investment: industry report")
        self.assertEqual(_low_value_signal_result(lead), "market-report-not-company-event")

    def test_v31_real_company_expansion_preserved(self):
        lead = make_lead("expansion", "https://example.com/news/example-factory", title="Example Industries opens new factory in Gujarat")
        self.assertEqual(_low_value_signal_result(lead), "")

    def test_v31_product_page_rejected(self):
        lead = make_lead("news", "https://example.com/products/revit", title="Revit — Buy in India")
        self.assertEqual(_low_value_signal_result(lead), "retail-product-page")

    def test_v31_structured_filing_protected(self):
        lead = make_lead("expansion", "https://example.com/products/revit", filing_id="NSE123", title="Revit — Buy in India")
        self.assertEqual(_low_value_signal_result(lead), "")

if __name__ == "__main__":
    unittest.main()

class TestQualificationV4(unittest.TestCase):
    def check(self, category, url, **kwargs):
        from radar import _qualification_gate_v4
        return _qualification_gate_v4(make_lead(category, url, **kwargs))

    def test_reference_article_rejected_even_as_tender(self):
        self.assertEqual(self.check('tender', 'https://www.britannica.com/place/India', title='India facts')[0], 'REJECTED')

    def test_specific_gem_reference_preserved_unverified(self):
        status, reason, priority = self.check('tender', 'https://example.com/bids/123', tender_id='GEM/2026/B/1234567')
        self.assertEqual(status, 'PROVISIONAL')
        self.assertIn('unverified', reason)
        self.assertGreater(priority, 0)

    def test_generic_tender_no_evidence_rejected(self):
        self.assertEqual(self.check('tender', 'https://example.com/info', title='Industry news')[0], 'REJECTED')

    def test_tender_notice_without_id_remains_provisional(self):
        self.assertEqual(self.check('tender', 'https://example.com/notice/abc', title='Tender notice for software supply')[0], 'PROVISIONAL')

    def test_hiring_with_employer_preserved(self):
        self.assertEqual(self.check('hiring', 'https://example.com/jobs', title='Example Ltd hiring civil engineers', buyer_org='Example Ltd')[0], 'PROVISIONAL')

    def test_hiring_without_vacancy_rejected(self):
        self.assertEqual(self.check('hiring', 'https://example.com/news', title='Market outlook')[0], 'REJECTED')

    def test_named_company_expansion_preserved(self):
        self.assertEqual(self.check('expansion', 'https://example.com/article', title='Example Ltd opens new factory', buyer_org='Example Ltd')[0], 'PROVISIONAL')

    def test_generic_expansion_without_event_rejected(self):
        self.assertEqual(self.check('expansion', 'https://example.com/overview', title='Industry overview')[0], 'REJECTED')

    def test_official_filing_still_unverified(self):
        self.assertEqual(self.check('filing', 'https://example.com/filing/1', filing_id='NSE-123')[0], 'PROVISIONAL')

    def test_government_detail_query_kept_provisional(self):
        status = self.check('tender', 'https://mptenders.gov.in/nicgep/app?page=FrontEndTenderDetails&tnid=77', title='AutoCAD tender')[0]
        self.assertEqual(status, 'PROVISIONAL')
