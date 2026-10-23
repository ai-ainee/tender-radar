"""tender_utils.py — Extract canonical tender IDs for cross-portal dedup.

Indian public procurement portals each have their own ID format, but the ID
is often the same across aggregators (a GeM ID quoted in a TenderTiger
article, etc.). Extracting it lets us skip the same tender appearing on
4 different sites.
"""
import re

TENDER_ID_PATTERNS = [
    # GeM format: GEM/2024/B/123456 or GEM-2024-B-123456
    re.compile(r"\bGEM[/-]\d{4}[/-][A-Z][/-]\d{4,}\b", re.I),
    # CPPP format: 2024_NTPC_78901_1
    re.compile(r"\b\d{4}_[A-Z]{2,}[A-Z0-9_]*_\d{3,}_\d+\b"),
    # Common prefixes: MSTC/..., IREPS/..., NIT/..., EOI/..., RFP/..., TENDER/...
    re.compile(r"\b(?:MSTC|IREPS|NIT|EOI|RFP|TENDER)[/-][A-Z0-9][A-Z0-9/_-]{3,}\b", re.I),
    # STATE/DEPT/YYYY/ID format
    re.compile(r"\b[A-Z]{2,5}/\d{4}/[A-Z0-9/_-]{3,}\b"),
    # Alt: STATE-YYYY-ID format
    re.compile(r"\b[A-Z]{2,5}-\d{4}-[A-Z0-9-]{3,}\b"),
]


def extract_tender_id(text):
    """Returns a normalised tender ID string, or '' if none found.

    Normalises to: uppercase, no whitespace, no surrounding punctuation.
    Two citations of the same ID (e.g. "gem/2024/b/123456" and
    "GEM/2024/B/123456") are treated as the same ID.
    """
    if not text:
        return ""
    for pat in TENDER_ID_PATTERNS:
        m = pat.search(str(text))
        if m:
            return re.sub(r"\s+", "", m.group(0).upper()).strip(".,;:")
    return ""


def tender_id_from_url(url):
    """Extracts a tender ID embedded in the URL path if present.

    Examples that work:
      https://eprocure.gov.in/cppp/tendersfullview/2024_NTPC_78901_1
      https://mahatenders.gov.in/.../NIT-2024-MH-1234
    """
    u = str(url or "")
    m = re.search(
        r"/([A-Z0-9]{2,}_\d{3,}_\d+|[A-Z]{2,}[/-]\d{4}[/-][A-Z0-9/_-]+)(?:[/?#]|$)",
        u, re.I
    )
    if m:
        return re.sub(r"\s+", "", m.group(1).upper())
    return ""
