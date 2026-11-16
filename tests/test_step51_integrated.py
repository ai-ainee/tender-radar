"""Step 51 integrated offline tests: no network, no API calls, no CRM writes.

Run from repository root: python -m unittest discover -s tests -p 'test_step51_integrated.py' -v
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from standard_lead import StandardLead
from sources import cppp, india_gov_portals as gov
from adapters.aggregator_tenders import PipeworxAdapter, _queries_from_settings
from adapters.tenders import TendersAdapter, _dict_to_lead
import pending_candidates as pending
import radar
import common


class FakeSearcher:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
    def search(self, query, **kw):
        self.calls.append((query, kw))
        host = query.split('site:',1)[1].split(' ',1)[0] if 'site:' in query else ''
        return list(self.rows.get(host, []))


class TestCPPP(unittest.TestCase):
    BASE = 'https://eprocure.gov.in/eprocure/app'
    DETAIL = BASE + '?page=FrontEndTenderDetailsExternal&tnid=123456'

    def test_actual_detail_url(self):
        self.assertTrue(cppp._detail_url(self.DETAIL))
    def test_reject_home(self):
        self.assertFalse(cppp._detail_url(self.BASE))
    def test_reject_malicious_domain(self):
        self.assertFalse(cppp._detail_url('https://eprocure.gov.in.evil.example/eprocure/app?page=FrontEndTenderDetails&tnid=123'))
    def test_reject_search(self):
        self.assertFalse(cppp._detail_url(self.BASE + '?page=FrontEndListTendersbyDate&service=page'))
    def test_html_listing_extract(self):
        html = '''<table><tr><td><a href="?page=FrontEndTenderDetailsExternal&amp;tnid=123456">Industrial pump acquisition</a></td><td>13-Nov-2026</td></tr></table>'''
        rows = cppp._parse_html_list(html, self.BASE, 'cppp', ['industrial pump'], 10)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['url'],self.DETAIL)
        self.assertTrue(rows[0]['metadata']['review_required'])
        self.assertFalse(rows[0]['metadata']['active_tender_verified'])
    def test_html_no_keyword_no_match(self):
        html = '<a href="?page=FrontEndTenderDetails&tnid=12345">Milk tender</a>'
        self.assertEqual(cppp._parse_html_list(html,self.BASE,'cppp',['Tractor'],10),[])
    def test_html_not_made_up_from_title(self):
        html = '<table><tr><td>Industrial pump tender</td></tr></table>'
        self.assertEqual(cppp._parse_html_list(html,self.BASE,'cppp',['pump'],10),[])
    def test_rss_parse(self):
        xml = f'<rss><channel><item><title>Industrial pump RFQ</title><link>{self.DETAIL.replace("&", "&amp;")}</link><description>Pump for public works</description></item></channel></rss>'
        rows=cppp._parse_feed(xml,['industrial pump'],10)
        self.assertEqual(len(rows),1)
        self.assertFalse(rows[0]['metadata']['active_tender_verified'])
    def test_atom_namespaced(self):
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Tractor parts supply bid</title><link href="https://etenders.gov.in/eprocure/app?page=FrontEndTenderDetails&amp;tnid=4"/><summary>Tractor spare parts</summary></entry></feed>'
        rows=cppp._parse_feed(xml,['tractor'],10)
        self.assertEqual(len(rows),1)
    def test_index_search_scoped(self):
        url=self.DETAIL
        fake=FakeSearcher({'eprocure.gov.in':[{'link':url,'title':'Industrial pump tender','snippet':'RFQ'}]})
        rows=cppp._index_fallback(['industrial pump'],10,searcher=fake)
        self.assertEqual(len(rows),1)
        self.assertTrue(all(c[1]['preserve_operators'] for c in fake.calls))
        self.assertIn('site:eprocure.gov.in',fake.calls[0][0])
        self.assertFalse(rows[0]['metadata']['official_source_verified'])
    def test_html_and_index_merge_distinct(self):
        html = '<a href="?page=FrontEndTenderDetailsExternal&amp;tnid=123456">Industrial pump tender</a>'
        fake=FakeSearcher({'eprocure.gov.in':[{'link':self.DETAIL,'title':'Industrial pump tender'},
            {'link':self.BASE+'?page=FrontEndTenderDetailsExternal&tnid=987','title':'Industrial pump procurement'}]})
        with patch.object(cppp,'_try_direct',return_value=('', '')),patch.object(cppp,'_try_jina',return_value=('', '')),patch.object(cppp,'_try_scraperapi',return_value=('', '')):
            def http(url): return html if url==self.BASE else ''
            rows=cppp.fetch_cppp(['industrial pump'],10,searcher=fake,http_get=http)
        self.assertEqual(len(rows),2)
    def test_unavailable_returns_empty_not_raises(self):
        fake=FakeSearcher({})
        with patch.object(cppp,'_try_direct',return_value=('', '')),patch.object(cppp,'_try_jina',return_value=('', '')),patch.object(cppp,'_try_scraperapi',return_value=('', '')):
            self.assertEqual(cppp.fetch_cppp(['something'],10,searcher=fake,http_get=lambda u:''),[])


class TestPortalAndSearch(unittest.TestCase):
    def test_portal_inventory_count(self):
        self.assertEqual((len(gov.STATE_PORTALS),len(gov.UT_PORTALS),len(gov.PSU_PORTALS),len(gov.CENTRAL_PORTALS)),(28,9,12,6))
    def test_pseudo_portal_rejected(self):
        p=gov.STATE_PORTALS[0]
        self.assertFalse(gov._host_match(f'https://{p.host}.attacker.example/tender/abc',p))
    def test_rotation_covers_all(self):
        covered=set()
        for n in range(30):
            covered.update(p.code for p in gov.plan_portals(date(2026,11,10)+timedelta(days=n),12))
        self.assertEqual(covered,{p.code for p in gov.ALL_PORTALS})
    def test_query_preserves_scope(self):
        target=gov.plan_portals(date(2026,11,10),1)[0]
        fake=FakeSearcher({target.host:[{'link':f'https://{target.host}/nicgep/app?page=FrontEndTenderDetailsExternal&tnid=123',
                                           'title':'Industrial pump tender','snippet':'Public procurement'}]})
        rows=gov.fetch_india_government_tenders(['Industrial pump'],max_queries=1,searcher=fake,when=date(2026,11,10))
        self.assertEqual(len(rows),1)
        self.assertTrue(fake.calls[0][1]['preserve_operators'])
        self.assertTrue(rows[0]['metadata']['review_required'])
    def test_search_engine_operator_opt_in(self):
        eng=common.SearchEngine.__new__(common.SearchEngine)
        eng.serper=True;eng.brave=False
        eng._serper=lambda q,gl,page,recent:[{'link':'https://example.net/tender','title':q,'snippet':''}]
        eng._ddgs=lambda *a, **kw:[]
        with patch.object(common.time,'sleep',lambda x:None):
            raw=eng.search('site:eprocure.gov.in "industrial pumps"',country='india',preserve_operators=True)
            legacy=eng.search('site:eprocure.gov.in "industrial pumps"',country='india')
        self.assertIn('site:eprocure.gov.in',raw[0]['title'])
        self.assertNotIn('site:eprocure.gov.in',legacy[0]['title'])
    def test_all_government_candidates_provisional(self):
        p=gov.plan_portals(date(2026,11,10),1)[0]
        fake=FakeSearcher({p.host:[{'link':f'https://{p.host}/nicgep/app?page=FrontEndTenderDetails&tnid=123',
                                   'title':'Pump parts tender','snippet':'Pump parts requisition'}]})
        rows=gov.fetch_india_government_tenders(['Pump'],1,1,searcher=fake,when=date(2026,11,10))
        self.assertEqual(len(rows),1)
        self.assertFalse(rows[0]['metadata']['buyer_verified'])
        self.assertFalse(rows[0]['metadata']['active_tender_verified'])


class TestAdapter(unittest.TestCase):
    def test_government_adapter_connected(self):
        self.assertTrue(hasattr(TendersAdapter,'_try_government_portals'))
    def test_cppp_metadata_roundtrip(self):
        record={'url':TestCPPP.DETAIL,'title':'Pump tender','snippet':'Pump purchase',
                'metadata':cppp._meta('official_html_listing','cppp',True)}
        lead=_dict_to_lead('cppp',record)
        self.assertTrue(lead.metadata['review_required'])
        self.assertFalse(lead.metadata['active_tender_verified'])
    def test_government_index_no_buyer_invention(self):
        record={'url':'https://mptenders.gov.in/nicgep/app?page=FrontEndTenderDetails&tnid=23',
                'title':'Pump tender','snippet':'Pump request',
                'metadata':{'review_required':True,'buyer_verified':False}}
        lead=_dict_to_lead('government_portal_index',record)
        self.assertEqual(lead.buyer_org,'')
        self.assertTrue(lead.metadata['review_required'])
    def test_real_adapter_collects_government_index(self):
        source={'url':'https://mptenders.gov.in/nicgep/app?page=FrontEndTenderDetails&tnid=56',
                'title':'Tractor parts procurement', 'snippet':'Tractor requisition',
                'metadata':{'verification_status':'unverified_official_domain_index',
                            'relevance_keyword_match':True}}
        leads=[]
        with patch.object(gov,'fetch_india_government_tenders',return_value=[source]):
            count=TendersAdapter()._try_government_portals(['Tractor'],leads)
        self.assertEqual(count,1)
        self.assertEqual(len(leads),1)
        self.assertTrue(leads[0].metadata['review_required'])
        self.assertFalse(leads[0].metadata['buyer_verified'])

    def test_public_source_not_cached_id(self):
        lead=StandardLead(source='tenders',category='tender',url='https://a.gov.in/tender/item',
            tender_id='2026_ABC_123456_1',metadata={'internal_source':'cppp','review_required':True})
        self.assertFalse(radar._can_cache_tender_id(lead))
    def test_other_source_cache_unchanged(self):
        lead=StandardLead(source='tenders',category='tender',url='https://a.gov.in/tender/item',
            tender_id='2026_ABC_123456_1',metadata={'internal_source':'legacy'})
        self.assertTrue(radar._can_cache_tender_id(lead))
    def test_government_review_guard(self):
        class Sheet:
            def __init__(self): self.posts=[]
            def post(self, action, **kw):
                self.posts.append((action,kw));return {'status':'success'}
        lead=_dict_to_lead('government_portal_index',{
            'url':'https://mptenders.gov.in/nicgep/app?page=FrontEndTenderDetails&tnid=99',
            'title':'Industrial pumps','snippet':'Public tender pumps',
            'metadata':{'review_required':True,'government_portal_name':'Madhya Pradesh',
                        'active_tender_verified':False,'buyer_verified':False,
                        'current_deadline_verified':False}})
        sheet=Sheet();router=radar.Router(sheet)
        verdict={'organization':'Government of Madhya Pradesh','is_valid':True,
                 'confidence':'HIGH','entity_role':'BUYER','lead_score':96,
                 'why_engage_now':'Closing tomorrow submit bid immediately!',
                 'upcoming_events':'Deadline tomorrow','signal_freshness':'RECENT'}
        old=radar.DRY_RUN
        try:
            radar.DRY_RUN=False
            outcome=router.push(lead,verdict,'Pumps','ALL_SECTORS')
        finally:
            radar.DRY_RUN=old
        self.assertEqual(outcome[0],'REVIEW')
        self.assertEqual(len(sheet.posts),1)
        kw=sheet.posts[0][1]
        self.assertIn('NOT VERIFIED',kw['row_data'][8])
        self.assertNotIn('tomorrow',kw['row_data'][8].lower())
    def test_ai_rejected_keyword_matched_government_notice_goes_to_manual(self):
        class Sheet:
            def __init__(self): self.posts=[]
            def post(self, action, **kw):
                self.posts.append((action,kw));return {'status':'success'}
        lead=_dict_to_lead('government_portal_index',{
            'url':'https://mptenders.gov.in/nicgep/app?page=FrontEndTenderDetails&tnid=77',
            'title':'Industrial pumps tender', 'snippet':'Industrial pumps supply RFQ',
            'metadata':{'review_required':True,'relevance_keyword_match':True,
                        'buyer_verified':False,'active_tender_verified':False}})
        sheet=Sheet();router=radar.Router(sheet)
        old=radar.DRY_RUN
        try:
            radar.DRY_RUN=False
            outcome=router.push(lead,{'organization':'Unknown','is_valid':False,
                'entity_role':'IRRELEVANT','confidence':'LOW'},'Pumps','ALL_SECTORS')
        finally:
            radar.DRY_RUN=old
        self.assertEqual(outcome[0],'MANUAL')
        self.assertEqual(sheet.posts[0][1]['target_sheet'],radar.SHEET_MANUAL)

    def test_distinct_tenders_ignore_ai_fp_collision(self):
        self.assertIn('if lead.category == "tender":',Path('radar.py').read_text())
        self.assertIn('lead.category != "tender" and fp',Path('radar.py').read_text())


class TestPipeworx(unittest.TestCase):
    def test_settings_driven_queries(self):
        q=_queries_from_settings({'targets':['Industrial pumps','Industrial valves','Motor']},2,date(2026,11,10))
        self.assertEqual(len(q),2)
        self.assertTrue(set(q)<=set(['Industrial pumps','Industrial valves','Motor']))
    def test_not_hardcoded_software(self):
        self.assertNotIn('software',str(_queries_from_settings({'targets':['Motor']},1)).lower())
    def test_source_url_unchanged(self):
        url='https://eprocure.gov.in/eprocure/app?page=FrontEndTenderDetails&tnid=42'
        lead=PipeworxAdapter()._to_lead({'source_url':url,'title':'Pump tender','tender_id':'ABC/5', 'organisation':'Water board'})
        self.assertEqual(lead.url,url)
        self.assertEqual(lead.tender_id,'')
        self.assertTrue(lead.metadata['review_required'])
    def test_geM_id_can_be_preserved(self):
        lead=PipeworxAdapter()._to_lead({'source_url':'https://a.example/bid/2344','title':'Pump tender','tender_id':'GEM/2026/B/8231919'})
        self.assertEqual(lead.tender_id,'GEM/2026/B/8231919')
        self.assertFalse(radar._can_cache_tender_id(lead))
    def test_missing_url_not_emitted(self):
        self.assertIsNone(PipeworxAdapter()._to_lead({'title':'Pump tender'}))


class TestPending(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'pending.json')
        self.lead=StandardLead(source='tenders',category='tender',url='https://gov.example/tender/235234',
            title='Pump tender',raw_text='Public reference only',metadata={'review_required':True},dm_phone='+911234567890')
    def tearDown(self):self.tmp.cleanup()
    def test_roundtrip(self):
        self.assertEqual(pending.save_pending([self.lead],self.path),1)
        self.assertEqual(len(pending.load_pending(self.path)),1)
    def test_no_personal_contact_fields_saved(self):
        pending.save_pending([self.lead],self.path)
        self.assertNotIn('+911234567890',Path(self.path).read_text())
    def test_exact_url_dedup_only(self):
        lead2=StandardLead(source='tenders',category='tender',url='https://gov.example/tender/other',title='Pump tender')
        self.assertEqual(pending.save_pending([self.lead,self.lead,lead2],self.path),2)
    def test_max_entries(self):
        leads=[StandardLead(source='tenders',category='tender',url=f'https://a.example/t/{i}') for i in range(30)]
        self.assertEqual(pending.save_pending(leads,self.path,max_entries=7),7)
    def test_expiry(self):
        old=date(2026,10,1)
        pending.save_pending([self.lead],self.path,today=old)
        self.assertEqual(pending.load_pending(self.path,today=date(2026,11,10)),[])
    def test_invalid_file_safe(self):
        Path(self.path).write_text('{broken')
        self.assertEqual(pending.load_pending(self.path),[])
    def test_recheck_metadata_roundtrip(self):
        self.lead.metadata['verification_status']='unverified_index'
        pending.save_pending([self.lead],self.path)
        loaded=pending.load_pending(self.path)
        self.assertEqual(loaded[0].metadata['verification_status'],'unverified_index')
    def test_main_reinjects_previous_pending_before_gates(self):
        source=Path('radar.py').read_text()
        self.assertIn('pending_prior + all_leads',source)
        self.assertIn('pending_next.append(lead)',source)
        self.assertIn('failed_for_pending.append(lead)',source)

    def test_no_queue_write_in_dry_run_by_control_flow(self):
        source=Path('radar.py').read_text()
        self.assertLess(source.index('    if DRY_RUN:\n        log.info("[DRY RUN]'),source.index('    queued_count = save_pending('))


class TestDiscoveryRecall(unittest.TestCase):
    def test_tender_queries_do_not_exclude_services(self):
        from adapters.discovery import _NEG_TENDER
        self.assertNotIn('-services',_NEG_TENDER)
        self.assertNotIn('-training',_NEG_TENDER)
        self.assertNotIn('-outsourcing',_NEG_TENDER)
    def test_google_news_candidate_marked_for_review(self):
        from adapters.discovery import _dict_to_lead
        lead=_dict_to_lead('google_news',{
            'url':'https://news.google.com/rss/articles/ABC',
            'title':'Company ABC acquires industrial pump maker',
            'snippet':'Company ABC expanding','source_url':'https://publisher.example',
            'vendor_publisher':True})
        self.assertTrue(lead.metadata['google_news_rss'])
        self.assertTrue(lead.metadata['snippet_only'])
        self.assertTrue(lead.metadata['vendor_publisher'])
    def test_news_redirect_not_unconditionally_dropped(self):
        source=Path('radar.py').read_text()
        self.assertIn('(lead.metadata or {}).get("google_news_rss") is True',source)
        self.assertIn('lead.category == "news"',source)
    def test_ad_click_redirect_still_detected(self):
        self.assertTrue(radar._is_google_redirect('https://google.com/url?q=abc'))



if __name__=='__main__':
    unittest.main(verbosity=2)
