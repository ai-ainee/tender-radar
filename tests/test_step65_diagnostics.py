"""Step 65: diagnostic counters and URL-secret redaction, offline only."""
import importlib.util
import io
import logging
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class CaptureLog:
    def __init__(self): self.lines = []
    def info(self, fmt, *args): self.lines.append(fmt % args if args else fmt)
    def warning(self, fmt, *args): self.lines.append(fmt % args if args else fmt)

log = CaptureLog()
common = types.ModuleType('common')
common.log = log
common.SearchEngine = lambda: None

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'sources'/filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod

    with patch.dict(sys.modules, {'common': common}):
        spec.loader.exec_module(mod)
    return mod

gov = load('step65_gov', 'india_gov_portals.py')

class FakeSearch:
    def search(self, query, **kwargs):
        host = query.split('site:')[1].split(' ')[0]
        return [
            {'link': f'https://{host}/eprocure/app', 'title':'AutoCAD tender', 'snippet':'AutoCAD'},
            {'link': f'https://{host}/eprocure/app?page=FrontEndTenderDetailsExternal&tnid=123', 'title':'AutoCAD tender', 'snippet':'Autodesk'},
            {'link': 'https://untrusted.example/tender/ref', 'title':'AutoCAD tender', 'snippet':'AutoCAD'},
            {'link': f'https://{host}/eprocure/app?page=FrontEndTenderDetailsExternal&tnid=124', 'title':'Pump tender', 'snippet':'Pump'},
        ]

class TestStep65(unittest.TestCase):
    def test_rejection_diagnostics_and_provisional_safety(self):
        log.lines.clear()
        out = gov.fetch_india_government_tenders(['AutoCAD'], max_queries=1,
                 when=date(2026,11,11), searcher=FakeSearch())
        self.assertEqual(len(out), 1)
        md = out[0]['metadata']
        self.assertTrue(md['review_required'])
        self.assertFalse(md['active_tender_verified'])
        self.assertFalse(md['official_source_verified'])
        d = next(x for x in log.lines if '[gov-index-diagnostics]' in x)
        for item in ("'raw': 4", "'off_domain': 1", "'not_detail': 1", "'keyword_miss': 1", "'accepted': 1"):
            self.assertIn(item, d)

    def test_http_errors_never_log_api_key(self):
        # BeautifulSoup is installed by the GitHub offline test workflow.
        try:
            import bs4
        except ImportError:
            self.skipTest('beautifulsoup4 not installed in this environment')
        cppp = load('step65_cppp', 'cppp.py')
        log.lines.clear()
        url = 'https://api.scraperapi.com?api_key=PRIVATE_EXAMPLE_SECRET&url=https%3A%2F%2Feprocure.gov.in'
        with patch.object(cppp.urllib.request, 'urlopen', side_effect=RuntimeError('PRIVATE_EXAMPLE_SECRET')):
            self.assertEqual(cppp._http(url), '')
        self.assertNotIn('PRIVATE_EXAMPLE_SECRET', '\n'.join(log.lines))
        self.assertIn('RuntimeError', '\n'.join(log.lines))

if __name__ == '__main__': unittest.main(verbosity=2)
