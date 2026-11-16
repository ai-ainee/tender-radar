"""Offline Step52 coverage report tests."""
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('source_coverage_report',root/'tools/source_coverage_report.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

LOG='''
[CORP-COVERAGE] track=direct_rfq scope=broad indexed=7 qualified=2
[CORP-COVERAGE] track=capex scope=broad indexed=4 qualified=0
[CORP-COVERAGE] track=tech_adoption search_error=quota
[CORP-COVERAGE] total=2 direct=2 signals=0 queries=3 errors=1 raw=11
[CORP-FILINGS] bse_rss_relevant=1
[gov-index] CENTRAL_PMGSY 9 indexed item(s), 1 provisional detail(s)
[cppp-index] eprocure.gov.in 2 provisional details from 7 indexed hits
[adapter] corporate_intelligence: 2 lead(s)
[adapter] tenders: 1 lead(s)
After gates: 3 leads survive (from 11)
[DRY RUN] Would evaluate 3 leads via AI
'''
class ReportTests(unittest.TestCase):
    def test_track_counts(self):self.assertEqual(module.summarize(LOG)['corporate_tracks']['direct_rfq / broad']['qualified'],2)
    def test_zero_tracks(self):self.assertIn('capex / broad',module.summarize(LOG)['corporate_zero_tracks'])
    def test_errors(self):self.assertEqual(module.summarize(LOG)['corporate_errors'],['tech_adoption'])
    def test_direct_indirect(self):self.assertEqual(module.summarize(LOG)['corporate_summary']['direct'],2)
    def test_bse(self):self.assertEqual(module.summarize(LOG)['bse_rss_relevant'],1)
    def test_cppp(self):self.assertEqual(module.summarize(LOG)['cppp_hosts']['eprocure.gov.in']['indexed'],7)
    def test_state_counts(self):self.assertEqual(module.summarize(LOG)['government_portals']['CENTRAL_PMGSY']['provisional'],1)
    def test_adapter(self):self.assertEqual(module.summarize(LOG)['adapters']['corporate_intelligence'],2)
    def test_final_gates(self):self.assertEqual(module.summarize(LOG)['after_gates']['selected'],3)
    def test_zip_input(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'logs.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('run/harvester.txt',LOG)
            self.assertIn('capex / broad',module.summarize(module.read_logs(p))['corporate_zero_tracks'])
    def test_empty_valid(self):self.assertIsNone(module.summarize('')['after_gates'])
if __name__=='__main__':unittest.main()
