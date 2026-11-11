"""Offline-only Step 51 tests. Run: python tests/test_india_gov_portals.py"""
import importlib.util
from datetime import date, timedelta
from pathlib import Path
import sys
import types
import unittest

# No network access, no repository secrets, no production writes.
common = types.ModuleType('common')
common.log = types.SimpleNamespace(info=lambda *a, **kw: None,
                                   warning=lambda *a, **kw: None)
common.SearchEngine = lambda: None
sys.modules.setdefault('common', common)
p = Path(__file__).resolve().parents[1] / 'sources' / 'india_gov_portals.py'
spec = importlib.util.spec_from_file_location('india_gov_portals', p)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class FakeSearch:
    def __init__(self, items):
        self.items = items
        self.queries = []
    def search(self, query, **kw):
        self.queries.append((query, kw))
        host = query.split('site:', 1)[1].split(' ', 1)[0]
        return self.items.get(host, [])


class TestPortalCatalog(unittest.TestCase):
    WHEN = date(2026, 11, 10)

    def test_all_states(self):
        self.assertEqual(len(mod.STATE_PORTALS), 28)
        self.assertEqual(len({x.code for x in mod.STATE_PORTALS}), 28)

    def test_all_union_territories(self):
        self.assertEqual(len(mod.UT_PORTALS), 9)
        self.assertEqual(len({x.code.removesuffix('2') for x in mod.UT_PORTALS}), 8)

    def test_central_and_psu(self):
        self.assertEqual((len(mod.CENTRAL_PORTALS), len(mod.PSU_PORTALS)), (6, 12))

    def test_each_portal_uses_https(self):
        self.assertTrue(all(x.url.startswith('https://') and x.host for x in mod.ALL_PORTALS))

    def test_no_duplicate_codes(self):
        self.assertEqual(len({x.code for x in mod.ALL_PORTALS}), len(mod.ALL_PORTALS))

    def test_bounded_query_plan(self):
        self.assertEqual(len(mod.plan_portals(self.WHEN, 12)), 12)
        self.assertEqual(len(mod.plan_portals(self.WHEN, 1)), 1)
        self.assertEqual(len(mod.plan_portals(self.WHEN, 0)), 0)

    def test_plan_rotates(self):
        a = [x.code for x in mod.plan_portals(self.WHEN, 12)]
        b = [x.code for x in mod.plan_portals(self.WHEN + timedelta(days=1), 12)]
        self.assertNotEqual(a, b)

    def test_several_days_cover_all_states_and_psus(self):
        covered = set()
        for i in range(30):
            covered.update(p.code for p in mod.plan_portals(self.WHEN + timedelta(days=i), 12))
        self.assertEqual(covered, {p.code for p in mod.ALL_PORTALS})

    def test_target_terms_configurable(self):
        self.assertIn('"Civil 3D"', mod._target_expression(['Civil 3D', 'Autodesk'], self.WHEN))
        self.assertNotIn('Autodesk', mod._target_expression(['Tractor parts'], self.WHEN))

    def test_bad_domain_rejected(self):
        portal = mod.CENTRAL_PORTALS[0]
        self.assertFalse(mod._host_match('https://eprocure.gov.in.evil.com/tender/1', portal))
        self.assertFalse(mod._host_match('http://eprocure.gov.in/tender/1', portal))
        self.assertTrue(mod._host_match('https://eprocure.gov.in/eprocure/app', portal))

    def test_specific_detail_only(self):
        self.assertFalse(mod._is_specific_tender('https://eprocure.gov.in/eprocure/app', 'Home Tenders', ''))
        self.assertFalse(mod._is_specific_tender('https://gov.in/login', 'Autodesk tender', ''))
        self.assertTrue(mod._is_specific_tender('https://eprocure.gov.in/eprocure/app?page=FrontEndTenderDetailsExternal&tnid=123', 'AutoCAD tender', ''))

    def test_configured_search(self):
        chosen = mod.plan_portals(self.WHEN, 2)
        host = chosen[0].host
        item = {'link': f'https://{host}/eprocure/app?page=FrontEndTenderDetailsExternal&tnid=12',
                'title': 'Procurement of industrial pumps', 'snippet': 'RFP for pump purchase'}
        fake = FakeSearch({host: [item]})
        result = mod.fetch_india_government_tenders(['industrial pumps'],
                    max_queries=2, when=self.WHEN, searcher=fake)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['metadata']['review_required'])
        self.assertFalse(result[0]['metadata']['buyer_verified'])
        self.assertFalse(result[0]['metadata']['current_deadline_verified'])
        self.assertFalse(result[0]['metadata']['active_tender_verified'])
        self.assertTrue(all(q[1]['recent'] is False for q in fake.queries))

    def test_unrelated_search_result_dropped(self):
        chosen = mod.plan_portals(self.WHEN, 1)[0]
        host = chosen.host
        fake = FakeSearch({host: [{'link': f'https://{host}/tender/long-key',
                                   'title': 'Water treatment tender', 'snippet': 'RFP and bidding'}]})
        self.assertEqual(mod.fetch_india_government_tenders(['Autodesk'],
                           max_queries=1, when=self.WHEN, searcher=fake), [])

    def test_unknown_duplicate_kept_when_distinct_urls(self):
        selected = mod.plan_portals(self.WHEN, 1)
        host = selected[0].host
        arr = []
        for ix in (1, 2):
            arr.append({'link': f'https://{host}/eprocure/app?page=FrontEndTenderDetailsExternal&tnid={ix}',
                        'title': f'GEM/2026/B/8231919 AutoCAD tender copy {ix}',
                        'snippet': 'AutoCAD procurement'})
        arr.append(dict(arr[0]))  # repeat exact URL
        fake = FakeSearch({host: arr})
        result = mod.fetch_india_government_tenders(['AutoCAD'], max_queries=1,
                    when=self.WHEN, searcher=fake)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['metadata']['possible_gem_bid_refs'], ['GEM/2026/B/8231919'])

    def test_outside_domain_rejected(self):
        portal = mod.plan_portals(self.WHEN, 1)[0]
        fake = FakeSearch({portal.host: [{'link': 'https://tenderkart.in/tender/cool-item',
                           'title': 'AutoCAD RFP', 'snippet': 'AutoCAD renewal'}]})
        self.assertEqual(mod.fetch_india_government_tenders(['AutoCAD'],
                        max_queries=1, when=self.WHEN, searcher=fake), [])

    def test_network_error_is_isolated(self):
        class Broken:
            def search(self, *a, **kw):
                raise RuntimeError('unreachable')
        self.assertEqual(mod.fetch_india_government_tenders(['AutoCAD'],
                    max_queries=2, when=self.WHEN, searcher=Broken()), [])

    def test_empty_targets_no_calls(self):
        fake = FakeSearch({})
        self.assertEqual(mod.fetch_india_government_tenders([], searcher=fake), [])
        self.assertFalse(fake.queries)

    def test_limit_outputs(self):
        chosen = mod.plan_portals(self.WHEN, 1)[0]
        host = chosen.host
        fake = FakeSearch({host: [{'link': f'https://{host}/tender/long-key-{i}',
                    'title': 'CAD tender renewal', 'snippet': 'CAD supply'} for i in range(8)]})
        self.assertEqual(len(mod.fetch_india_government_tenders(['CAD'],
                         max_queries=1, max_results=3, when=self.WHEN, searcher=fake)), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
