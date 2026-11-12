"""Step 71 offline tests: bounded recovery, no false verification, no secrets in errors."""
import importlib.util
import os
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
logger = types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)

common = types.ModuleType('common')
common.log = logger
common.SearchEngine = lambda: None

spec = importlib.util.spec_from_file_location(
    'step71_gov',
    ROOT / 'sources' / 'india_gov_portals.py'
)
gov = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gov

with patch.dict(sys.modules, {'common': common}):
    spec.loader.exec_module(gov)

class Search:
    def __init__(self, recovery_items=None):
        self.queries = []
        self.recovery_items = recovery_items if recovery_items is not None else []
    def search(self, query, **kwargs):
        self.queries.append(query)
        host = query.split('site:',1)[1].split(' ',1)[0]
        if 'inurl:FrontEndTenderDetails' in query:
            return self.recovery_items or []
        return [{'link': f'https://{host}/nicgep/app', 'title': 'AutoCAD tender', 'snippet': 'AutoCAD tender notices'}]

class TestStep71(unittest.TestCase):
    WHEN = date(2026,11,11)
    def test_recovers_detail_without_claiming_active_tender(self):
        portal = gov.plan_portals(self.WHEN, 1)[0]
        # Force NIC-style fixture because the daily rotation may choose a non-NIC portal.
        portal = gov.CENTRAL_PORTALS[0]
        good = {'link': f'https://{portal.host}/eprocure/app?page=FrontEndTenderDetailsExternal&tnid=123',
                'title':'AutoCAD tender for licenses', 'snippet':'AutoCAD procurement'}
        s = Search([good])
        with patch.object(gov,'plan_portals',return_value=[portal]), patch.dict(os.environ,{'RADAR_GOV_DETAIL_RETRIES':'1'}):
            out = gov.fetch_india_government_tenders(['AutoCAD'],max_queries=1,searcher=s,when=self.WHEN)
        self.assertEqual(len(s.queries),2)
        self.assertEqual(len(out),1)
        self.assertTrue(out[0]['metadata']['review_required'])
        self.assertFalse(out[0]['metadata']['active_tender_verified'])
        self.assertFalse(out[0]['metadata']['official_source_verified'])

    def test_recovery_can_be_disabled(self):
        portal = gov.CENTRAL_PORTALS[0]
        s = Search()
        with patch.object(gov,'plan_portals',return_value=[portal]), patch.dict(os.environ,{'RADAR_GOV_DETAIL_RETRIES':'0'}):
            out = gov.fetch_india_government_tenders(['AutoCAD'],max_queries=1,searcher=s,when=self.WHEN)
        self.assertEqual(out,[])
        self.assertEqual(len(s.queries),1)

    def test_limited_retries_and_offdomain_rejected(self):
        p1,p2 = gov.CENTRAL_PORTALS[:2]
        s = Search([{'link':'https://impostor.example/tender/12345', 'title':'AutoCAD tender', 'snippet':'AutoCAD'}])
        with patch.object(gov,'plan_portals',return_value=[p1,p2]), patch.dict(os.environ,{'RADAR_GOV_DETAIL_RETRIES':'1'}):
            out = gov.fetch_india_government_tenders(['AutoCAD'],max_queries=2,searcher=s,when=self.WHEN)
        self.assertEqual(out,[])
        self.assertEqual(len(s.queries),3)

    def test_exception_does_not_log_provider_secret(self):
        captured=[]
        class Log:
            def info(self,*a):pass
            def warning(self,fmt,*a):captured.append(fmt%a)
        class Bad:
            def search(self,*a,**kw):raise RuntimeError('api_key=PRIVATE_TEST_VALUE')
        with patch.object(gov,'log',Log()),patch.object(gov,'plan_portals',return_value=[gov.CENTRAL_PORTALS[0]]):
            out = gov.fetch_india_government_tenders(['AutoCAD'],max_queries=1,searcher=Bad(),when=self.WHEN)
        self.assertEqual(out,[])
        self.assertNotIn('PRIVATE_TEST_VALUE',' '.join(captured))

if __name__ == '__main__':unittest.main(verbosity=2)
