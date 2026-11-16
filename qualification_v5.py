"""Evidence-first, general-purpose pre-AI lead gate. No network calls or verification claims."""
import re
from urllib.parse import urlparse

BAD = re.compile(r'\b(?:lottery|jackpot|cricket|ipl|horoscope|celebrity|divorce|lawsuit|court ruling|legal dispute)\b', re.I)
PROMO = re.compile(r'\b(?:why (?:you|every|simulation)|how to|tutorial|guide to|best practices|tips for|webinar|course|training|learn how|what is|benefits of)\b', re.I)
EVENT = re.compile(r'\b(?:expands?|expanded|expansion|commissioned|commissioning|inaugurated|opened|opens|opening of|sets? up|setting up|building|constructing|invests?|investment|announces? (?:a |an )?(?:new |major )?(?:plant|facility|project|investment)|wins? (?:a |the )?(?:contract|order)|awarded? (?:a |the )?(?:contract|project)|secures? (?:a |the )?(?:contract|order)|launches? (?:a |new )?(?:facility|project|plant)|plans?|developing|develops?|completes? (?:a |the )?(?:joint venture|acquisition)|adopts?|deploys?|implements?)\b', re.I)
ASSET = re.compile(r'\b(?:plant|facility|factory|manufacturing|semiconductor|airport|aviation|aerospace|metro|rail|highway|road|bridge|infrastructure|data cent(?:er|re)|construction|housing|industrial|project|contract|order|software|platform|technology|BIM|ERP|capex|investment|rocket testing)\b', re.I)
HIRING = re.compile(r'\b(?:hiring|vacanc(?:y|ies)|recruit(?:ing|ment)|job openings?|positions? available|seeking applicants)\b', re.I)
TENDER = re.compile(r'\b(?:tender|bid|procurement|request for proposal|\bRFP\b|invitation to bid|supply of|annual maintenance contract)\b', re.I)
GEM = re.compile(r'\bGEM/20\d{2}/B/\d+\b', re.I)
ORG_PREFIX = re.compile(r'^([A-Z][\w&.,-]*(?:\s+(?:[A-Z][\w&.,-]*|of|and|&)){0,5}?)\s+(?:[-–:]\s*)?(?=expands?\b|expanded\b|wins?\b|opens?\b|inaugurates?\b|commissions?\b|invests?\b|launches?\b|secures?\b|awarded\b|builds?\b|sets? up\b|deploys?\b|adopts?\b|plans?\b|developing\b|completes?\b|celebrates?\b)',re.I)
ORG_SUFFIX = re.compile(r'\b(?:Ltd\.?|Limited|Inc\.?|Corporation|Corp\.?|Technologies|Projects|Industries|Group|Mining|Airports|Energy|Infrastructure|Systems)\b',re.I)
GENERIC = {'autodesk','india','government','company','factory','why','how','new','the','a','an','software','project','projects'}

def extract_organization(title, snippet='', existing=''):
    if existing and str(existing).strip():
        return str(existing).strip()[:160]
    title = str(title or '').strip()
    # News headlines often describe a company's role before its name.
    title = re.sub(r'^(?:space tech startup|startup|Indian company|engineering firm)\s+', '', title, flags=re.I)
    # Preserve named issuer before a publisher-style dash.
    issuer = re.match(r'^([A-Z][A-Za-z0-9 .,&-]{2,65}?(?:Inc\.|Limited|Ltd\.|Corporation|Technologies|Group))\s*[-–]\s*', title)
    if issuer:
        return issuer.group(1).strip()[:160]
    # Extract only a clearly named subject; never invent a buyer from a news publisher.
    for candidate in (title, re.sub(r'^[^:]{1,30}:\s*', '', title)):
        match = ORG_PREFIX.search(candidate)
        if match:
            name = match.group(1).strip(' -–:,')
            if name.lower() not in GENERIC and not name.lower().startswith(('why ','how ','new ')):
                return name[:160]
    match = re.search(r'\b([A-Z][\w&.-]+(?:\s+[A-Z][\w&.-]+){0,4}\s+(?:Ltd\.?|Limited|Inc\.?|Corporation|Corp\.?|Technologies|Projects|Industries|Group|Mining|Airports|Energy|Infrastructure|Systems))\b',title)
    if match:
        return match.group(1)[:160]
    # Search snippets are lower-confidence; don't treat an agency mention as buyer identity.
    return ''

def qualify(lead):
    title = str(getattr(lead,'title','') or '')
    snippet = str(getattr(lead,'snippet','') or '')
    category = str(getattr(lead,'category','') or '').lower()
    url = str(getattr(lead,'url','') or '')
    host = urlparse(url).netloc.lower()
    text = title + ' ' + snippet
    existing = getattr(lead,'buyer_org','') or ''
    org = extract_organization(title,snippet,existing)
    tender_id = str(getattr(lead,'tender_id','') or '')
    filing_id = str(getattr(lead,'filing_id','') or '')
    # Specific references are candidates, not verified active procurement.
    if tender_id or GEM.search(text):
        return 'PROVISIONAL','specific-tender-unverified',90,org
    if filing_id:
        return 'PROVISIONAL','specific-filing-unverified',85,org
    if host.endswith(('britannica.com','wikipedia.org')) or host in ('help.autodesk.com','learn.microsoft.com','developer.mozilla.org'):
        return 'REJECTED','reference-content-not-opportunity',0,org
    if BAD.search(title) and not (TENDER.search(title) and category=='tender'):
        return 'REJECTED','unrelated-news',0,org
    if PROMO.search(title) and not TENDER.search(title):
        return 'REJECTED','educational-or-promotional-content',0,org
    if category == 'tender':
        if not TENDER.search(text):
            return 'REJECTED','no-specific-procurement-evidence',0,org
        return 'PROVISIONAL','tender-source-and-deadline-unverified',65 if org else 45,org
    if category == 'hiring':
        if not HIRING.search(text):
            return 'REJECTED','no-vacancy-evidence',0,org
        # Employer-specific posting required; generic vendor careers pages not buyer evidence.
        if not org:
            return 'REJECTED','employer-not-identifiable',0,org
        return 'PROVISIONAL','named-employer-hiring-unverified',65,org
    if category in ('news','expansion','filing'):
        if not EVENT.search(title) or not ASSET.search(text):
            return 'REJECTED','no-specific-commercial-event',0,org
        if not org:
            return 'REJECTED','event-organization-not-identifiable',0,org
        return 'PROVISIONAL','named-commercial-event-unverified',75,org
    return 'REJECTED','unsupported-or-unspecific-signal',0,org
