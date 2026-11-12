import unittest
from standard_lead import StandardLead
from qualification_v5 import qualify

class TestV5Qualification(unittest.TestCase):
    def lead(self,title,category='news',snippet='',url='https://example.org/article',tender_id=''):
        return StandardLead(source='test',category=category,url=url,title=title,snippet=snippet,tender_id=tender_id)
    def test_lottery_rejected(self):
        self.assertEqual(qualify(self.lead('Punjab man wins lottery after daughter born'))[0],'REJECTED')
    def test_promotional_rejected(self):
        self.assertEqual(qualify(self.lead('Why Simulation Should Be Part of Every Factory Expansion - Autodesk'))[0],'REJECTED')
    def test_specific_gem_remains_unverified(self):
        r=qualify(self.lead('GEM/2026/B/8175444 — AutoCAD contract','tender'))
        self.assertEqual((r[0],r[2]),('PROVISIONAL',90))
    def test_named_expansion(self):
        r=qualify(self.lead('Astrobase expands its rocket testing infrastructure in Andhra Pradesh'))
        self.assertEqual((r[0],r[3]),('PROVISIONAL','Astrobase'))
    def test_named_project(self):
        r=qualify(self.lead('Reliance plans $4.6 billion coal gasification project in Andhra Pradesh'))
        self.assertEqual((r[0],r[3]),('PROVISIONAL','Reliance'))
    def test_micron_issuer(self):
        r=qualify(self.lead('Micron Technology, Inc. - Micron Celebrates Opening of India semiconductor facility'))
        self.assertEqual((r[0],r[3]),('PROVISIONAL','Micron Technology, Inc.'))
    def test_no_unnamed_hiring(self):
        self.assertEqual(qualify(self.lead('17 Job Openings Apply Free','hiring'))[0],'REJECTED')
    def test_named_gmr_development(self):
        r=qualify(self.lead('GMR Group developing 500-acre aviation and aerospace ecosystem in Andhra Pradesh'))
        self.assertEqual((r[0],r[3]),('PROVISIONAL','GMR Group'))
if __name__=='__main__':unittest.main()
