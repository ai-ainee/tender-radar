"""Step 52 tests: corporate direct buying + signals. No internet/CRM writes."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from adapters import get_all
from adapters.corporate_intelligence import CorporateIntelligenceAdapter, _to_lead
from sources import corporate_intelligence as ci
from standard_lead import StandardLead
import radar
import pending_candidates


SETTINGS = {"targets": ["Industrial Pumps", "Safety Gear", "ERP Software", "CAD Tools", "Services"],
            "industries": ["Manufacturing", "Logistics"], "countries": ["India"],
            "company_watchlist": ["https://buyer.example.com/tenders", "Corporate Holdings Ltd"]}


class FakeSearch:
    def __init__(self, results=None, exception=False):
        self.results=results or {}; self.exception=exception; self.calls=[]
    def search(self, q, **kwargs):
        self.calls.append((q,kwargs))
        if self.exception: raise RuntimeError("quota")
        for term, items in self.results.items():
            if term in q: return items
        return []


class FakeSheet:
    def __init__(self): self.posts=[]
    def post(self, action, **kwargs):
        self.posts.append((action,kwargs))
        return {"status":"success"}


def record(category="tender", verified=False, track="direct_rfq"):
    d = {"url":"https://buyer.example.org/procurement/rfq-2026-55",
         "title":"Industrial Pumps public RFQ for procurement",
         "snippet":"Public index reports Industrial Pumps RFQ, check official source",
         "category":category,
         "metadata":{
             "internal_source":"corporate_public_index", "source_kind":"corporate_public_search",
             "corporate_signal_track":track,"review_required": not verified,
             "verification_status":"unverified_corporate_index",
             "snippet_only":True, "relevance_keyword_match":True,
             "official_source_verified":verified,"buyer_verified":verified,
             "active_tender_verified":verified,"current_deadline_verified":verified,
         }}
    return _to_lead(d) if not verified else StandardLead(
        source="corporate_intelligence",category=category,url=d["url"],title=d["title"],
        snippet=d["snippet"],raw_text=d["snippet"],metadata={**d["metadata"],
            "review_required":False,"official_source_verified":True,"buyer_verified":True,
            "active_tender_verified":True,"current_deadline_verified":True,
            "snippet_only":False})


def verdict(is_valid=True,score=99):
    return {"organization":"Example Holdings India", "is_valid":is_valid,
            "confidence":"HIGH", "entity_role":"BUYER", "lead_score":score,
            "why_engage_now":"Deadline tomorrow — BID NOW at 10 AM",
            "upcoming_events":"Tender closes tomorrow", "signal_freshness":"RECENT"}


class TestPlanning(unittest.TestCase):
    def test_track_count(self): self.assertGreaterEqual(len(ci.TRACKS), 12)
    def test_each_track_unique(self): self.assertEqual(len({x.name for x in ci.TRACKS}),len(ci.TRACKS))
    def test_direct_types(self): self.assertGreaterEqual(len(ci.DIRECT_TRACKS),5)
    def test_indirect_types(self): self.assertGreaterEqual(sum(t.category!="tender" for t in ci.TRACKS),7)
    def test_all_track_types_in_normal_plan(self):
        plan=ci.plan_queries(SETTINGS,24,date(2026,11,10))
        self.assertTrue({t.name for t in ci.TRACKS}.issubset({p['track'] for p in plan}))
    def test_cap_is_respected(self): self.assertLessEqual(len(ci.plan_queries(SETTINGS,7)),7)
    def test_watchlist_domains_included(self):
        plan=ci.plan_queries(SETTINGS,30,date(2026,11,10))
        self.assertTrue(any('site:buyer.example.com' in p['query'] for p in plan))
    def test_watchlist_names_included(self):
        plan=ci.plan_queries(SETTINGS,30,date(2026,11,10))
        self.assertTrue(any('"Corporate Holdings Ltd"' in p['query'] for p in plan))
    def test_no_targets_or_industries(self):
        self.assertEqual(ci.plan_queries({"targets":[],"industries":[]}),[])
    def test_industries_without_products(self):
        rows=ci.plan_queries({"targets":[],"industries":["Industrial logistics"]})
        self.assertTrue(rows)
        self.assertTrue(all(p["category"]!="tender" for p in rows))
    def test_product_rotation(self):
        first=ci.plan_queries(SETTINGS,1,date(2026,11,10))[0]['query']
        second=ci.plan_queries(SETTINGS,1,date(2026,11,11))[0]['query']
        self.assertNotEqual(first,second)
    def test_watchlist_host_dedupe(self):
        self.assertEqual(ci._watchlist_sites(['example.com','https://example.com/buy','example.org']),['example.com','example.org'])
    def test_no_guessed_watchlist_domains(self):
        self.assertEqual(ci._watchlist_sites(['Corporate Holdings Ltd']),[])
    def test_invalid_port_watchlist_does_not_crash(self):
        self.assertEqual(ci._watchlist_sites(['https://buyer.example.com:invalid/tenders']),[])


class TestPublicCollection(unittest.TestCase):
    def test_targeted_rfq_retained(self):
        fake=FakeSearch({'"request for quotation"':[
            {"link":"https://buyer.example.com/rfq/129","title":"Industrial Pumps RFQ announcement","snippet":"Request for quotation"}]})
        rows=ci.collect_indexed(SETTINGS,1,searcher=fake,when=date(2026,11,10))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['category'],'tender')
    def test_provenance_unverified(self):
        fake=FakeSearch({'"request for quotation"':[
            {"link":"https://buyer.example.com/rfq/129","title":"Industrial Pumps RFQ announcement","snippet":"Request for quotation"}]})
        row=ci.collect_indexed(SETTINGS,1,searcher=fake)[0]
        self.assertTrue(row['metadata']['review_required'])
        self.assertFalse(row['metadata']['buyer_verified'])
        self.assertFalse(row['metadata']['active_tender_verified'])
    def test_discard_results_without_product_or_industry(self):
        fake=FakeSearch({'"request for quotation"':[
            {"link":"https://buyer.example.com/rfq/99","title":"Pen purchase RFQ","snippet":"Request for quotation"}]})
        self.assertEqual(ci.collect_indexed(SETTINGS,1,searcher=fake),[])
    def test_no_evidence_drops_result(self):
        fake=FakeSearch({'"request for quotation"':[
            {"link":"https://buyer.example.com/rfq/99","title":"Industrial Pumps product guide","snippet":"For your business"}]})
        self.assertEqual(ci.collect_indexed(SETTINGS,1,searcher=fake),[])
    def test_same_url_not_multi_counted(self):
        url='https://buyer.example.com/rfq/2026'
        fake=FakeSearch({'(': [{"link":url,"title":"Industrial Pumps public RFQ", "snippet":"Quotation"}]})
        rows=ci.collect_indexed(SETTINGS,12,searcher=fake)
        self.assertEqual(len(rows),1)
    def test_distinct_unknown_duplicate_urls_retained(self):
        fake=FakeSearch({'"request for quotation"':[
            {"link":"https://buyer.example.com/rfq/1", "title":"Industrial Pumps RFQ", "snippet":"Quotation"},
            {"link":"https://buyer.example.net/rfq/2", "title":"Industrial Pumps RFQ", "snippet":"Quotation"}]})
        rows=ci.collect_indexed(SETTINGS,1,searcher=fake)
        self.assertEqual(len(rows),2)
    def test_scoped_search_keeps_site_operator(self):
        settings={"targets":["Pumps"],"industries":[],"company_watchlist":["buyer.example.com"]}
        fake=FakeSearch({'site:buyer.example.com':[{'link':'https://buyer.example.com/tenders/131',
            'title':'Pumps RFQ','snippet':'Submit vendor quotation'}]})
        rows=ci.collect_indexed(settings,30,searcher=fake)
        self.assertTrue(any(r['metadata']['source_scope']=='company_site' for r in rows))
        self.assertTrue(all(x[1]['preserve_operators'] for x in fake.calls))
    def test_scoped_domain_rejects_spoof(self):
        settings={"targets":["Pumps"],"company_watchlist":["buyer.example.com"]}
        fake=FakeSearch({'site:buyer.example.com':[{'link':'https://buyer.example.com.attacker.net/tenders/131',
            'title':'Pumps RFQ','snippet':'Submit quotation'}]})
        rows=ci.collect_indexed(settings,30,searcher=fake)
        self.assertEqual(rows,[])
    def test_non_http_url_rejected(self):
        self.assertFalse(ci._valid_public_url('javascript:alert(1)'))
    def test_localhost_url_rejected(self):
        self.assertFalse(ci._valid_public_url('http://localhost/contract'))
    def test_query_errors_do_not_halt_source(self):
        rows=ci.collect_indexed(SETTINGS,3,searcher=FakeSearch(exception=True))
        self.assertEqual(rows,[])
    def test_result_cap(self):
        fake=FakeSearch({'"request for quotation"':[
            {'link':f'https://example.com/bid/{i}','title':f'Industrial Pumps RFQ {i}',
             'snippet':'Quotation'} for i in range(30)]})
        self.assertEqual(len(ci.collect_indexed(SETTINGS,1,5,searcher=fake)),5)
    def test_indirect_hiring_category(self):
        fake=FakeSearch({'hiring OR vacancy':[
            {'link':'https://career.example.org/job/444', 'title':'Industrial Pumps engineer hiring',
             'snippet':'Vacancy for company employee'}]})
        rows=ci.collect_indexed(SETTINGS,11,searcher=fake)
        self.assertEqual(rows[0]['category'],'hiring')
    def test_indirect_capex_category(self):
        fake=FakeSearch({'capital expenditure':[
            {'link':'https://example.org/news/investment','title':'Manufacturing plant capex investment',
             'snippet':'Capital expenditure approved'}]})
        rows=ci.collect_indexed(SETTINGS,8,searcher=fake)
        self.assertEqual(rows[0]['category'],'filing')
    def test_no_fake_buyer(self):
        lead=_to_lead({'url':'https://buyer.example.com/tender','title':'Pumps RFQ','category':'tender','metadata':{}})
        self.assertEqual(lead.buyer_org,'')
    def test_registration(self):
        self.assertIn('corporate_intelligence',{a.name for a in get_all()})
    def test_adapter_enabled_by_industries(self):
        a=CorporateIntelligenceAdapter()
        self.assertTrue(a.is_enabled({"industries":["Construction"]}))
        self.assertFalse(a.is_enabled({"targets":[],"industries":[]}))
    def test_all_configurable_products(self):
        item=ci.plan_queries({"targets":["Medical instruments"],"industries":[]},1)[0]
        self.assertIn('Medical instruments',item['query'])
        self.assertNotIn('Autocad',item['query'])


class TestPublicBSEFeed(unittest.TestCase):
    def test_rss_relevant_announcement(self):
        feed='''<rss><channel><item><title>Manufacturing contract awarded</title>
        <link>https://www.bseindia.com/announcements/1234</link>
        <description>New manufacturing contract and plant investment</description>
        </item></channel></rss>'''
        rows=ci.parse_public_filings(feed,SETTINGS)
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['metadata']['review_required'])
    def test_unrelated_financial_reports_rejected(self):
        feed='''<rss><channel><item><title>Financial results</title>
        <link>https://www.bseindia.com/announcements/1234</link>
        <description>Quarterly meeting</description></item></channel></rss>'''
        self.assertEqual(ci.parse_public_filings(feed,SETTINGS),[])
    def test_bse_spoof_rejected(self):
        feed='''<rss><channel><item><title>Manufacturing contract awarded</title>
        <link>https://www.bseindia.com.attacker.net/announcements/1234</link>
        <description>Manufacturing contract investment</description></item></channel></rss>'''
        self.assertEqual(ci.parse_public_filings(feed,SETTINGS),[])
    def test_malformed_xml_returns_empty(self):
        self.assertEqual(ci.parse_public_filings('<xml',SETTINGS),[])
    def test_bse_no_direct_purchase_claim(self):
        feed='''<rss><channel><item><title>Manufacturing capex</title>
        <link>https://www.bseindia.com/announcements/1234</link>
        <description>Manufacturing investment</description></item></channel></rss>'''
        rows=ci.parse_public_filings(feed,SETTINGS)
        self.assertFalse(rows[0]['metadata']['buyer_verified'])
    def test_feed_dedupe(self):
        item='''<item><title>Manufacturing capex</title><link>https://www.bseindia.com/announcements/1234</link><description>Manufacturing investment</description></item>'''
        self.assertEqual(len(ci.parse_public_filings(f'<rss><channel>{item}{item}</channel></rss>',SETTINGS)),1)
    def test_bse_env_optout(self):
        with patch.dict(os.environ,{"CORP_BSE_RSS_ENABLED":"0"}):
            with patch.object(ci,'collect_indexed',return_value=[]):
                def fail():raise AssertionError('network must not happen')
                self.assertEqual(ci.fetch_corporate_intelligence(SETTINGS,http_get=fail),[])
    def test_fetch_merges_feed(self):
        feed='''<rss><channel><item><title>Manufacturing investment</title><link>https://www.bseindia.com/announcements/777</link><description>Manufacturing contract awarded</description></item></channel></rss>'''
        with patch.object(ci,'collect_indexed',return_value=[]):
            rows=ci.fetch_corporate_intelligence(SETTINGS,http_get=lambda:feed)
        self.assertEqual(len(rows),1)


class TestRoutingAndBacklog(unittest.TestCase):
    def _route(self,lead,verdict0=None):
        sheet=FakeSheet()
        with patch.object(radar,'DRY_RUN',False):
            result=radar.Router(sheet).push(lead,verdict0 or verdict(),'Industrial Pumps','ALL_SECTORS')
        return result,sheet
    def test_public_index_identification(self):
        self.assertTrue(radar._unverified_corporate_public(record()))
    def test_provisional_corporate_not_government(self):
        self.assertFalse(radar._other_unverified_government_tender(record()))
    def test_unverified_corporate_tender_id_not_cached(self):
        lead=record();lead.tender_id='REF-123'
        self.assertFalse(radar._can_cache_tender_id(lead))
    def test_ai_prompt_contains_policy(self):
        self.assertIn('PURCHASE_INTENT_VERIFIED: NO',radar._format_lead(record(),0))
    def test_direct_high_routes_review(self):
        out,sheet=self._route(record())
        self.assertEqual(out[0],'REVIEW')
        self.assertEqual(sheet.posts[0][1]['target_sheet'],radar.SHEET_REVIEW)
    def test_no_unverified_deadline_urgency(self):
        _,sheet=self._route(record())
        row=sheet.posts[0][1]['row_data']
        self.assertNotIn('tomorrow',row[8].lower())
        self.assertNotIn('bid now',row[8].lower())
        self.assertIn('NOT VERIFIED',row[8])
    def test_keeps_source_title_as_claim(self):
        _,sheet=self._route(record())
        self.assertIn('Industrial Pumps public RFQ',sheet.posts[0][1]['row_data'][8])
    def test_indirect_high_routes_review(self):
        lead=record('expansion',track='facility_expansion')
        out,sheet=self._route(lead)
        self.assertEqual(out[0],'REVIEW')
        self.assertIn('POTENTIAL BUYING SIGNAL',sheet.posts[0][1]['row_data'][8])
    def test_hiring_high_routes_review(self):
        out,sheet=self._route(record('hiring',track='hiring'))
        self.assertEqual(out[0],'REVIEW')
    def test_bse_filing_high_routes_review(self):
        out,sheet=self._route(record('filing',track='bse_public_filing'))
        self.assertEqual(out[0],'REVIEW')
    def test_indirect_industry_only_reject_not_manual(self):
        lead=record('expansion')
        lead.metadata['matched_target']=''
        out,sheet=self._route(lead,verdict(is_valid=False))
        self.assertEqual(out[0],'TRASH')
    def test_target_matched_indirect_reject_goes_manual(self):
        lead=record('expansion')
        lead.metadata['matched_target']='Industrial Pumps'
        out,sheet=self._route(lead,verdict(is_valid=False))
        self.assertEqual(out[0],'MANUAL')
        self.assertIn('NOT verified'.upper(),sheet.posts[0][1]['signal_brief'].upper())
    def test_target_matched_indirect_manual_can_disable(self):
        lead=record('expansion')
        lead.metadata['matched_target']='Industrial Pumps'
        with patch.dict(os.environ,{'CORP_SIGNAL_REJECT_TO_MANUAL':'0'}):
            out,sheet=self._route(lead,verdict(is_valid=False))
        self.assertEqual(out[0],'TRASH')
    def test_direct_ai_reject_to_manual(self):
        out,sheet=self._route(record(),verdict(is_valid=False))
        self.assertEqual(out[0],'MANUAL')
        self.assertIn('NOT verified'.upper(),sheet.posts[0][1]['signal_brief'].upper())
    def test_direct_manual_can_disable(self):
        with patch.dict(os.environ,{"CORP_DIRECT_REJECT_TO_MANUAL":"0"}):
            out,sheet=self._route(record(),verdict(is_valid=False))
        self.assertEqual(out[0],'TRASH')
    def test_no_keyword_match_rejected_goes_to_trash(self):
        lead=record();lead.metadata['relevance_keyword_match']=False
        out,sheet=self._route(lead,verdict(is_valid=False))
        self.assertEqual(out[0],'TRASH')
    def test_verified_corporate_not_forced_to_review(self):
        lead=record(verified=True)
        out,sheet=self._route(lead)
        self.assertEqual(out[0],'INBOX')
    def test_noncorporate_unchanged(self):
        lead=StandardLead(source='news',category='news',url='https://example.org/news/a',title='Investment')
        out,sheet=self._route(lead)
        self.assertEqual(out[0],'INBOX')
    def test_pending_keeps_corporate_metadata(self):
        lead=record('expansion',track='facility_expansion')
        with tempfile.TemporaryDirectory() as tmp:
            path=tmp+'/pending.json'
            pending_candidates.save_pending([lead],path=path,today=date(2026,11,10))
            loaded=pending_candidates.load_pending(path=path,today=date(2026,11,10))
        self.assertEqual(len(loaded),1)
        self.assertEqual(loaded[0].metadata['corporate_signal_track'],'facility_expansion')
        self.assertTrue(radar._unverified_corporate_public(loaded[0]))
    def test_current_government_review_safeguard(self):
        md={'internal_source':'government_portal_index','review_required':True,
            'official_source_verified':False,'active_tender_verified':False}
        lead=StandardLead(source='tenders',category='tender',url='https://example.gov.in/tender/11',
                          metadata=md)
        self.assertTrue(radar._other_unverified_government_tender(lead))
    def test_step44_gem_recheck_unchanged(self):
        md={'internal_source':'gem','verification_status':'unverified_index',
            'provisional_tender':True,'active_tender_verified':False}
        lead=StandardLead(source='tenders',category='tender',url='https://indexed.example.org/abc',
                          tender_id='GEM/2026/B/7777777',metadata=md)
        self.assertTrue(radar._unverified_gem_reference(lead))
        self.assertFalse(radar._can_cache_tender_id(lead))

if __name__ == '__main__':
    unittest.main(verbosity=2)
