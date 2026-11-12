
"""Offline regression tests for Radar Scout lead-quality filters."""

import unittest

from standard_lead import StandardLead
from radar import _low_value_tender_result, _low_value_browse_result


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


if __name__ == "__main__":
    unittest.main()
