"""V5.1 conservative tender evidence and same-run project-event deduplication.

No network access: an accepted tender remains UNVERIFIED until independently checked.
"""
import re
from urllib.parse import urlparse

from qualification_v5 import qualify as qualify_v5

_SPECIFIC_ID = re.compile(r'\b(?:GEM/20\d{2}/B/\d+|(?:NIT|RFQ|RFP|TENDER|BID)[\s:#-]*(?:NO\.?\s*)?[A-Z0-9][A-Z0-9/.-]{5,})\b', re.I)
_PROCUREMENT_DOCUMENT = re.compile(r'\b(?:request for quotation|request for proposal|invitation (?:for|to) bid|notice inviting tender|tender notice)\b', re.I)
_DIRECTORY = re.compile(r'\b(?:tender results|latest contract awarded|tenders? in india|government of india tenders|govt eprocurement|view latest|tender detail|tender listing|tenders? online|search tenders)\b', re.I)
_MONEY = re.compile(r'(?:₹|rs\.?\s*)\s*([\d,]+(?:\.\d+)?)\s*(crore|cr|lakh|million|billion)?', re.I)
_PROJECT = re.compile(r'\b(?:coal gasification|semiconductor|aviation|aerospace|rocket testing|metro|airport|data cent(?:er|re)|solar|steel|refinery|manufacturing|industrial park|power plant|highway|railway|housing|satellite launch)\b', re.I)
_LOCATION = re.compile(r'\b(?:andhra pradesh|telangana|maharashtra|uttar pradesh|gujarat|karnataka|tamil nadu|odisha|delhi|dhaka|mumbai|hyderabad|bengaluru)\b', re.I)

def qualify(lead):
    verdict, reason, priority, org = qualify_v5(lead)
    if str(getattr(lead, 'category', '')).lower() != 'tender':
        return verdict, reason, priority, org
    title = str(getattr(lead, 'title', '') or '')
    snippet = str(getattr(lead, 'snippet', '') or '')
    url = str(getattr(lead, 'url', '') or '')
    text = title + ' ' + snippet
    path = urlparse(url).path.lower()
    tender_id = str(getattr(lead, 'tender_id', '') or '')
    # Require a numeric component; words like 'Tender Results' are not IDs.
    matches = [m.group() for m in _SPECIFIC_ID.finditer(text + ' ' + tender_id)]
    has_id = any(re.search(r'\d', m) for m in matches)
    # A document containing a concrete invitation is a review candidate, never a verified tender.
    has_document = path.endswith('.pdf') and bool(_PROCUREMENT_DOCUMENT.search(text))
    if _DIRECTORY.search(title) and not has_document and not (has_id and tender_id):
        return 'REJECTED', 'generic-tender-directory', 0, org
    if not (has_id or has_document):
        return 'REJECTED', 'tender-lacks-specific-reference-or-document', 0, org
    return 'PROVISIONAL', 'specific-tender-awaits-official-verification', 90 if has_id else 70, org

def event_key(lead, org=None):
    """Conservative same-run key; only merges named, quantified, matching project events.

    Missing project or amount intentionally means no deduplication, rather than
    accidentally merging distinct projects from one company.
    """
    if str(getattr(lead, 'category', '')).lower() not in ('news', 'expansion', 'filing'):
        return ''
    org = (org or getattr(lead, 'buyer_org', '') or '').lower().strip()
    if len(org) < 4:
        return ''
    title = str(getattr(lead, 'title', '') or '')
    money = _MONEY.search(title)
    project = _PROJECT.search(title)
    location = _LOCATION.search(title)
    if not (money and project and location):
        return ''
    amount = money.group(1).replace(',', '')
    try:
        amount = str(float(amount)).rstrip('0').rstrip('.') if '.' in amount else str(int(amount))
    except ValueError:
        return ''
    unit = (money.group(2) or '').lower().replace('crore','cr')
    org = re.sub(r'\W+', '', org)
    return '|'.join((org, project.group().lower(), location.group().lower(), amount, unit))
