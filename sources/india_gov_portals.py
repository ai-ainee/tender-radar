"""India government procurement portal discovery (Step 51, ADAPTER NOT WIRED).

Coverage: 28 states, 8 union territories (37 distinct state/UT sites), 6 central
systems and 12 central PSU portals. The portal registry is a discovery TARGET
catalogue, not a statement that each site is reachable or searchable.

This module searches only PUBLIC results from official/officially-listed portal
hosts using the project's existing common.SearchEngine. It does not log in,
defeat CAPTCHAs, simulate a bidder, or fabricate an official tender-document URL.
Search snippets are *not* evidence of an active bid, confirmed buyer or deadline.

Product/industry terms come exclusively from Settings via `keywords`.
Integration into adapters/tenders.py is intentionally a separate, tested step.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse, parse_qs
from typing import Optional

from common import SearchEngine, log


@dataclass(frozen=True)
class Portal:
    code: str
    name: str
    kind: str  # central, state, ut, psu
    url: str

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or '').lower().removeprefix('www.')


# The first 31 state/UT endpoints and 12 PSUs are based on the Government of
# India's official participating-sites directory (eprocure.gov.in/epublish).
# Separately confirmed state-specific platforms are used where applicable.
CENTRAL_PORTALS = (
    Portal('CENTRAL_CPPP', 'Central eProcurement (CPPP)', 'central', 'https://eprocure.gov.in/eprocure/app'),
    Portal('CENTRAL_EPUBLISH', 'Central ePublishing (CPPP)', 'central', 'https://eprocure.gov.in/epublish/app'),
    Portal('CENTRAL_ETENDERS', 'Central Government eTenders', 'central', 'https://etenders.gov.in/eprocure/app'),
    Portal('CENTRAL_DEFENCE', 'Defence eProcurement', 'central', 'https://defproc.gov.in/nicgep/app'),
    Portal('CENTRAL_PMGSY', 'PMGSY eProcurement', 'central', 'https://pmgsytenders.gov.in/nicgep/app'),
    Portal('CENTRAL_IREPS', 'Indian Railways IREPS', 'central', 'https://www.ireps.gov.in/'),
)

STATE_PORTALS = (
    Portal('AP', 'Andhra Pradesh', 'state', 'https://tender.apeprocurement.gov.in/'),
    Portal('AR', 'Arunachal Pradesh', 'state', 'https://arunachaltenders.gov.in/nicgep/app'),
    Portal('AS', 'Assam', 'state', 'https://assamtenders.gov.in/nicgep/app'),
    Portal('BR', 'Bihar', 'state', 'https://eproc2.bihar.gov.in/contract/'),
    Portal('CG', 'Chhattisgarh', 'state', 'https://cgeproc.cgstate.gov.in/'),
    Portal('GA', 'Goa', 'state', 'https://eprocure.goa.gov.in/nicgep/app'),
    Portal('GJ', 'Gujarat', 'state', 'https://www.nprocure.com/'),
    Portal('HR', 'Haryana', 'state', 'https://etenders.hry.nic.in/nicgep/app'),
    Portal('HP', 'Himachal Pradesh', 'state', 'https://hptenders.gov.in/nicgep/app'),
    Portal('JH', 'Jharkhand', 'state', 'https://jharkhandtenders.gov.in/nicgep/app'),
    Portal('KA', 'Karnataka KPPP', 'state', 'https://kppp.karnataka.gov.in/'),
    Portal('KL', 'Kerala', 'state', 'https://etenders.kerala.gov.in/nicgep/app'),
    Portal('MP', 'Madhya Pradesh', 'state', 'https://mptenders.gov.in/nicgep/app'),
    Portal('MH', 'Maharashtra', 'state', 'https://mahatenders.gov.in/nicgep/app'),
    Portal('MN', 'Manipur', 'state', 'https://manipurtenders.gov.in/nicgep/app'),
    Portal('ML', 'Meghalaya', 'state', 'https://meghalayatenders.gov.in/nicgep/app'),
    Portal('MZ', 'Mizoram', 'state', 'https://mizoramtenders.gov.in/nicgep/app'),
    Portal('NL', 'Nagaland', 'state', 'https://nagalandtenders.gov.in/nicgep/app'),
    Portal('OD', 'Odisha', 'state', 'https://tendersodisha.gov.in/nicgep/app'),
    Portal('PB', 'Punjab', 'state', 'https://eproc.punjab.gov.in/nicgep/app'),
    Portal('RJ', 'Rajasthan', 'state', 'https://eproc.rajasthan.gov.in/nicgep/app'),
    Portal('SK', 'Sikkim', 'state', 'https://sikkimtender.gov.in/nicgep/app'),
    Portal('TN', 'Tamil Nadu', 'state', 'https://tntenders.gov.in/nicgep/app'),
    Portal('TS', 'Telangana', 'state', 'https://tender.telangana.gov.in/'),
    Portal('TR', 'Tripura', 'state', 'https://tripuratenders.gov.in/nicgep/app'),
    Portal('UP', 'Uttar Pradesh', 'state', 'https://etender.up.nic.in/nicgep/app'),
    Portal('UK', 'Uttarakhand', 'state', 'https://uktenders.gov.in/nicgep/app'),
    Portal('WB', 'West Bengal', 'state', 'https://wbtenders.gov.in/nicgep/app'),
)

# Dadra & Nagar Haveli and Daman & Diu is ONE union territory, with two public
# tender portals listed separately on the central directory.
UT_PORTALS = (
    Portal('AN', 'Andaman and Nicobar Islands', 'ut', 'https://eprocure.andamannicobar.gov.in/nicgep/app'),
    Portal('CH', 'Chandigarh', 'ut', 'https://etenders.chd.nic.in/nicgep/app'),
    Portal('DH', 'Dadra and Nagar Haveli and Daman and Diu (DNH)', 'ut', 'https://dnhtenders.gov.in/nicgep/app'),
    Portal('DH2', 'Dadra and Nagar Haveli and Daman and Diu (Daman)', 'ut', 'https://ddtenders.gov.in/nicgep/app'),
    Portal('DL', 'Delhi NCT', 'ut', 'https://govtprocurement.delhi.gov.in/nicgep/app'),
    Portal('JK', 'Jammu and Kashmir', 'ut', 'https://jktenders.gov.in/nicgep/app'),
    Portal('LA', 'Ladakh', 'ut', 'https://tenders.ladakh.gov.in/nicgep/app'),
    Portal('LD', 'Lakshadweep', 'ut', 'https://tendersutl.gov.in/nicgep/app'),
    Portal('PY', 'Puducherry', 'ut', 'https://pudutenders.gov.in/nicgep/app'),
)

PSU_PORTALS = (
    Portal('PSU_BEL', 'Bharat Electronics', 'psu', 'https://eprocurebel.co.in/'),
    Portal('PSU_BHEL', 'Bharat Heavy Electricals', 'psu', 'https://eprocurebhel.co.in/'),
    Portal('PSU_CIL', 'Coal India', 'psu', 'https://coalindiatenders.nic.in/nicgep/app'),
    Portal('PSU_CPCL', 'Chennai Petroleum', 'psu', 'https://cpcletenders.nic.in/nicgep/app'),
    Portal('PSU_GRSE', 'Garden Reach Shipbuilders', 'psu', 'https://eprocuregrse.co.in/'),
    Portal('PSU_GSL', 'Goa Shipyard', 'psu', 'https://eprocuregsl.nic.in/nicgep/app'),
    Portal('PSU_HSL', 'Hindustan Shipyard', 'psu', 'https://eprocurehsl.nic.in/nicgep/app'),
    Portal('PSU_IOCL', 'Indian Oil Corporation', 'psu', 'https://iocletenders.nic.in/nicgep/app'),
    Portal('PSU_MDL', 'Mazagon Dock Shipbuilders', 'psu', 'https://eprocuremdl.nic.in/nicgep/app'),
    Portal('PSU_MIDHANI', 'Mishra Dhatu Nigam', 'psu', 'https://eprocuremidhani.nic.in/nicgep/app'),
    Portal('PSU_NTPC', 'NTPC', 'psu', 'https://eprocurentpc.nic.in/nicgep/app'),
    Portal('PSU_BEML', 'BEML', 'psu', 'https://eprocurebeml.nic.in/nicgep/app'),
)

ALL_PORTALS = CENTRAL_PORTALS + STATE_PORTALS + UT_PORTALS + PSU_PORTALS
BID_ID_RE = re.compile(r'\bGEM\s*/\s*20\d{2}\s*/\s*B\s*/\s*\d{5,9}\b', re.I)
DETAIL_CUES = ('tender', 'bid', 'rfq', 'rfi', 'rfp', 'eoi', 'procurement', 'quotation', 'supply',
               'purchase', 'licence', 'license', 'works', 'contract', 'invitation', 'renewal')
BAD_PATHS = ('/login', '/signin', '/register', '/faq', '/contact', '/about', '/policy',
             '/privacy', '/terms', '/help', '/downloads', '/tenders-by-', '/search')


def _rotating_slice(portals, count, when):
    """Deterministic daily portal rotation, never a random sample."""
    if not count or not portals:
        return []
    start = (when.toordinal() * count) % len(portals)
    return [portals[(start + i) % len(portals)] for i in range(min(count, len(portals)))]


def plan_portals(when: Optional[date] = None, max_queries: int = 12):
    """Fair rotation across national/PSU/state portals, capped to API budget.

    The national group is consulted each run; all states/UTs and PSUs rotate
    over successive days. This is *scheduled coverage*, not successful scraping.
    """
    when = when or date.today()
    budget = max(0, int(max_queries))
    if not budget:
        return []
    central_n = min(3, budget)
    remaining = budget - central_n
    state_n = max(0, round(remaining * 0.7))
    psu_n = remaining - state_n
    if remaining and not psu_n and budget >= 5:
        state_n -= 1
        psu_n += 1
    state_pool = STATE_PORTALS + UT_PORTALS
    return (
        _rotating_slice(CENTRAL_PORTALS, central_n, when) +
        _rotating_slice(state_pool, state_n, when) +
        _rotating_slice(PSU_PORTALS, psu_n, when)
    )


def _target_expression(targets, when):
    clean = []
    for t in targets or []:
        t = re.sub(r'\s+', ' ', str(t).replace('"', '').strip())
        if 2 <= len(t) <= 90 and t.casefold() not in [x.casefold() for x in clean]:
            clean.append(t)
    if not clean:
        return ''
    # Rotate product terms for large portfolios so no product is always omitted.
    offset = ((when.toordinal() - 1) * 4) % len(clean)
    group = [clean[(offset + i) % len(clean)] for i in range(min(4, len(clean)))]
    return ' OR '.join('"' + t + '"' for t in group)


def _host_match(url, portal):
    parsed = urlparse(str(url))
    host = (parsed.hostname or '').lower().removeprefix('www.')
    # Never include off-domain/third-party results just because search gave them.
    return parsed.scheme == 'https' and bool(host) and (
        host == portal.host or host.endswith('.' + portal.host)
    )


def _is_specific_tender(url, title, snippet):
    """Conservative: reject home/login/categories; allow indexed tender details.

    A candidate still has unverified status and goes to Review at integration.
    """
    p = urlparse(url)
    path = p.path.casefold().rstrip('/')
    query = p.query.casefold()
    text = f'{title or ""} {snippet or ""}'.casefold()
    if any(path.endswith(b) for b in BAD_PATHS):
        return False
    if any(q in query for q in ('frontendtenderdetails', 'showtender', 'tenderid=', 'tender_id=')):
        return any(cue in text for cue in DETAIL_CUES)
    if not path or path in ('/nicgep/app', '/eprocure/app', '/epublish/app', '/contract',
                           '/index.html', '/home', '/tenders', '/tender', '/live-tenders'):
        return False
    if len(path) < 12:
        return False
    # Avoid broad category pages even if the query contains product names.
    if any(seg in path for seg in ('/tender-search', '/tender-list', '/latest-tenders',
                                  '/list-of-tenders', '/tenders-by-organisation',
                                  '/frontendlatest')):
        return False
    return any(cue in text for cue in DETAIL_CUES)


def fetch_india_government_tenders(keywords, max_results=60, max_queries=12,
                                    searcher=None, when: Optional[date] = None):
    """Find provisional official-DOMAIN indexed government tender references.

    Returns records compatible with adapters/tenders.py `_dict_to_lead` shape:
    [{url,title,snippet,metadata},...]. Does not assert buyer/date/active status.
    Caller must route unverified results for manual review and preserve provenance.
    """
    when = when or date.today()
    expr = _target_expression(keywords, when)
    if not expr or max_results < 1 or max_queries < 1:
        return []
    searcher = searcher or SearchEngine()
    selected = plan_portals(when=when, max_queries=max_queries)
    target_terms = [str(t).replace('\"', '').strip().casefold() for t in keywords if str(t).strip()]
    out, seen_exact_urls = [], set()
    summary = {"raw": 0, "off_domain": 0, "not_detail": 0, "keyword_miss": 0, "duplicate_url": 0, "accepted": 0}
    for portal in selected:
        if len(out) >= max_results:
            break
        query = f'site:{portal.host} ({expr}) (tender OR RFQ OR RFP OR procurement)'
        try:
            organic = searcher.search(query, country='india', pages=1, recent=False,
                                      preserve_operators=True)
        except Exception as exc:
            log.warning('[gov-index] %s search failed: %s', portal.code, str(exc)[:120])
            continue
        accepted = 0
        reasons = {"raw": 0, "off_domain": 0, "not_detail": 0, "keyword_miss": 0, "duplicate_url": 0}
        for item in organic or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get('link') or item.get('url') or '').strip()
            title = str(item.get('title') or '').strip()
            snippet = str(item.get('snippet') or '').strip()
            reasons["raw"] += 1
            if not _host_match(url, portal):
                reasons["off_domain"] += 1
                continue
            if not _is_specific_tender(url, title, snippet):
                reasons["not_detail"] += 1
                continue
            if not any(term in (title + ' ' + snippet).casefold() for term in target_terms):
                reasons["keyword_miss"] += 1
                continue
            if url in seen_exact_urls:
                reasons["duplicate_url"] += 1
                continue  # identical URL only; uncertain cross-site dupes are retained.
            # Retain the original search index and don't assert a live opportunity.
            seen_exact_urls.add(url)
            bid_refs = sorted(set(m.group().upper().replace(' ', '') for m in BID_ID_RE.finditer(title + ' ' + snippet)))
            out.append({
                'url': url,
                'title': title[:300],
                'snippet': ('UNVERIFIED GOVERNMENT PORTAL SEARCH RESULT. Buyer, current '
                            'deadline, tender status and corrigenda NOT verified. '
                            'Manual verification required. ' + snippet)[:1000],
                'metadata': {
                    'internal_source': 'government_portal_index',
                    'government_portal': portal.code,
                    'government_portal_name': portal.name,
                    'government_portal_kind': portal.kind,
                    'government_portal_url': portal.url,
                    'source_index_domain': portal.host,
                    'verification_status': 'unverified_official_domain_index',
                    'source_fetched': False,
                    'buyer_verified': False,
                    'current_deadline_verified': False,
                    'active_tender_verified': False,
                    'official_source_verified': False,
                    'provisional_tender': True,
                    'review_required': True,
                    'relevance_keyword_match': True,
                    'snippet_only': True,
                    'fetch_method': 'search_index',
                    'possible_gem_bid_refs': bid_refs,  # not used as proof of same bid.
                },
            })
            accepted += 1
            if len(out) >= max_results:
                break
        for key, value in reasons.items():
            summary[key] += value
        summary["accepted"] += accepted
        log.info('[gov-index] %s raw=%d off_domain=%d not_detail=%d keyword_miss=%d duplicate_url=%d provisional=%d',
                 portal.code, reasons["raw"], reasons["off_domain"], reasons["not_detail"],
                 reasons["keyword_miss"], reasons["duplicate_url"], accepted)
    log.info('[gov-index-diagnostics] %s', summary)
    log.info('[gov-index] %d provisional government tender references from %d portal searches; '
             'not verified active tenders', len(out), len(selected))
    return out
