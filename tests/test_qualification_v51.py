import unittest
from types import SimpleNamespace
from qualification_v51 import qualify, event_key

def lead(title, category='tender', url='https://example.com/list', org='', snippet='', tender_id=''):
    return SimpleNamespace(title=title, category=category, url=url, buyer_org=org, snippet=snippet, tender_id=tender_id, filing_id='')

class V51QualityTests(unittest.TestCase):
    def test_generic_directory_rejected(self):
        self.assertEqual(qualify(lead('Tender Results - View Latest Contract Awarded - Tender Detail'))[0], 'REJECTED')
    def test_unidentified_listing_rejected(self):
        self.assertEqual(qualify(lead('M9742534732 - (AUTODES.. Tender in India, ID 16068949'))[0], 'REJECTED')
    def test_gem_reference_provisional(self):
        self.assertEqual(qualify(lead('GEM/2026/B/7842239 - NTPC Mining AutoCAD tender'))[0], 'PROVISIONAL')
    def test_rfQ_document_provisional(self):
        self.assertEqual(qualify(lead('Request for Quotation (RFQ) for BIM Consultancy',url='https://example.com/rfq.pdf'))[0], 'PROVISIONAL')
    def test_document_not_verified(self):
        self.assertNotEqual(qualify(lead('Request for Quotation (RFQ) for BIM Consultancy',url='https://example.com/rfq.pdf'))[0], 'VERIFIED')
    def test_duplicate_reliance_project(self):
        a=lead('Reliance plans ₹43,000 crore coal gasification project in Andhra Pradesh','news',org='Reliance')
        b=lead('Reliance plans ₹43,000 cr project in Andhra Pradesh','news',org='Reliance')
        # Missing project description is not enough to collapse unrelated events.
        self.assertEqual(event_key(b), '')
        self.assertTrue(event_key(a))
    def test_different_projects_not_collapsed(self):
        a=lead('Reliance plans ₹43,000 crore coal gasification project in Andhra Pradesh','news',org='Reliance')
        b=lead('Reliance plans ₹43,000 crore solar project in Andhra Pradesh','news',org='Reliance')
        self.assertNotEqual(event_key(a), event_key(b))
if __name__=='__main__': unittest.main()
