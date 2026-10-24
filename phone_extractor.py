"""phone_extractor.py - Free phone extraction (stages 1-4).

DORMANT in Deploy 1 — no code imports this. Activated in Deploy 2 by
wiring find_dm_phone() into enrichment_worker.py.

Stages:
  1. Extract from already-fetched content (raw_text)
  2. Deep-read tender PDFs (tender category only)
  3. Targeted web search for DM's phone
  4. Directory page lookup (JustDial)

Stage 5 (providers: Prospeo, Bulkpe, Enrich.so, Apollo) is stubbed —
activated later when API keys are configured.

Line-type classification is pattern-based (no carrier API):
  10 digits starting 6-9       → mobile
  +91 6-9XXXXXXXXX             → mobile
  0XX XXXXXXX (STD + number)   → landline
  1800/1860 XXXXXXX            → tollfree
"""
import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

from common import log

# ── Indian phone patterns ─────────────────────────────────────────────
# Match: +91 9876543210 / 09876543210 / 9876543210 / 011-2345-6789
_PHONE_RE = re.compile(
    r"(?:\+?91[\s\-\.]?)?\(?0?\d{2,5}\)?[\s\-\.]?\d{3,5}[\s\-\.]?\d{4}"
)

_MOBILE_RE = re.compile(r"(?:\+?91|0)?\s*[6-9]\d{9}")
_LANDLINE_RE = re.compile(r"(?:\+?91|0)?\s*(?:11|22|33|44|20|40|80|79|33|44|20)\d{7,8}")
_TOLLFREE_RE = re.compile(r"1(?:800|860)\s*\d{6,7}")

# Label patterns for proximity association
_LABEL_PATTERNS = [
    (re.compile(r"\b(?:mobile|mob|cell|whatsapp|wa)\b", re.I), "mobile"),
    (re.compile(r"\b(?:tel|telephone|phone|ph)\b", re.I), None),  # pattern-based
    (re.compile(r"\b(?:fax)\b", re.I), "fax"),
    (re.compile(r"\b(?:toll\s*free)\b", re.I), "tollfree"),
]


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def normalize_phone(phone: str) -> str:
    """Return E.164 format (+91XXXXXXXXXX) for Indian numbers, or ''."""
    d = digits(phone)
    if not d:
        return ""
    # Strip leading 91 if it makes it 12 digits total
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    # Strip leading 0
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) != 10:
        return ""
    # Must start with 6-9 for mobile, or STD-code + number for landline
    return f"+91{d}"


def classify_phone_type(phone: str, near_label: str = "") -> str:
    """Classify as mobile/landline/tollfree/fax/''.

    Uses label first (if present), then pattern.
    """
    label_lower = (near_label or "").lower()

    # Label overrides
    if "fax" in label_lower:
        return "fax"
    if "toll" in label_lower:
        return "tollfree"
    if any(x in label_lower for x in ("mobile", "mob", "cell", "whatsapp")):
        return "mobile"

    d = digits(phone)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]

    if len(d) != 10:
        return ""

    if d[0] in "6789":
        return "mobile"

    if d[:2] in ("11", "22", "33", "44", "20", "40", "80", "79"):
        return "landline"

    return ""


def extract_phone_candidates(text: str) -> List[Dict]:
    """Extract all phone candidates with offset + nearby label.

    Returns list of:
        {"phone": "+919876543210", "type": "mobile", "label": "Mobile",
         "offset": 1234, "context": "...Mobile: +91 98765 43210..."}
    """
    if not text:
        return []

    seen = set()
    out = []
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0)
        normalized = normalize_phone(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        # Look for a label within 60 chars before the phone
        start = max(0, m.start() - 60)
        context = text[start:m.end() + 20]
        label = ""
        for pat, explicit_type in _LABEL_PATTERNS:
            lm = pat.search(context)
            if lm:
                label = lm.group(0)
                break

        # Classify by pattern + label
        phone_type = classify_phone_type(normalized, label)

        out.append({
            "phone": normalized,
            "type": phone_type,
            "label": label,
            "offset": m.start(),
            "context": context,
        })

    return out


def score_association(candidate: Dict, text: str, dm_name: str, dm_title: str) -> int:
    """Score how likely this phone belongs to the DM. 0-100.

    Factors:
      - Proximity to DM name
      - Explicit label present
      - Proximity to DM title
      - Line type (mobile gets higher)
      - Source quality (PDF vs snippet — passed via metadata)
    """
    score = 0
    name_tokens = [t.lower() for t in re.findall(r"[a-zA-Z]{3,}", dm_name or "") if len(t) >= 3]
    title_tokens = [t.lower() for t in re.findall(r"[a-zA-Z]{3,}", dm_title or "") if len(t) >= 3]

    context = (candidate.get("context") or "").lower()
    offset = candidate.get("offset", 0)

    # Proximity to DM name (0-40)
    if name_tokens:
        for tok in name_tokens:
            # Search within 500 chars around phone
            window = text[max(0, offset - 500):offset + 200].lower()
            if tok in window:
                distance = abs(window.index(tok) - 300) if window.index(tok) < len(window) else 500
                score += max(0, 40 - (distance // 15))
                break

    # Explicit label (0-30)
    if candidate.get("label"):
        score += 30

    # Proximity to title (0-15)
    if title_tokens:
        window = text[max(0, offset - 300):offset + 150].lower()
        if any(t in window for t in title_tokens):
            score += 15

    # Line type (0-15)
    if candidate.get("type") == "mobile":
        score += 15
    elif candidate.get("type") == "landline":
        score += 5

    return min(score, 100)


# ── Stage 1-4 (dormant, called by Deploy 2) ───────────────────────────

def find_dm_phone(lead, search_engine=None) -> Tuple[str, str, str, str, int]:
    """Find the DM's phone via 4-stage waterfall.

    Args:
        lead: StandardLead with dm_name + raw_text populated
        search_engine: optional SearchEngine for stages 3-4

    Returns:
        (phone, phone_type, phone_status, phone_source, score)
        All "" if not found.
    """
    dm_name = getattr(lead, "dm_name", "") or ""
    dm_title = getattr(lead, "dm_title", "") or ""
    category = getattr(lead, "category", "") or ""

    if not dm_name:
        return ("", "", "", "", 0)

    # Stage 1: existing content
    text = getattr(lead, "raw_text", "") or ""
    if text:
        result = _stage_1_existing(text, dm_name, dm_title)
        if result:
            return result

    # Stage 2: tender PDF deep-read
    if category == "tender":
        result = _stage_2_pdf(lead)
        if result:
            return result

    # Stage 3-4: web search + directory
    if search_engine:
        result = _stage_3_search(lead, search_engine)
        if result:
            return result
        result = _stage_4_directory(lead, search_engine)
        if result:
            return result

    return ("", "", "", "", 0)


def _stage_1_existing(text, dm_name, dm_title):
    """Stage 1: extract from already-fetched content."""
    candidates = extract_phone_candidates(text)
    if not candidates:
        return None

    scored = [(score_association(c, text, dm_name, dm_title), c) for c in candidates]
    scored.sort(key=lambda x: -x[0])

    if scored and scored[0][0] >= 60:
        s, c = scored[0]
        return (c["phone"], c["type"], "verified", "content", s)
    elif scored and scored[0][0] >= 40:
        s, c = scored[0]
        return (c["phone"], c["type"], "unverified", "content", s)
    return None


def _stage_2_pdf(lead):
    """Stage 2: deep-read PDF for contact block (tender only)."""
    url = getattr(lead, "url", "") or ""
    if not url.lower().endswith(".pdf") and "pdf" not in url.lower():
        return None

    try:
        from common import fetch_page
        content = fetch_page(url, max_chars=20000, pdf_pages=10)
    except Exception as e:
        log.debug(f"[phone] pdf deep-read failed: {str(e)[:80]}")
        return None

    if not content or len(content) < 500:
        return None

    dm_name = getattr(lead, "dm_name", "") or ""
    dm_title = getattr(lead, "dm_title", "") or ""
    return _stage_1_existing(content, dm_name, dm_title)


def _stage_3_search(lead, search_engine):
    """Stage 3: targeted web search."""
    dm_name = getattr(lead, "dm_name", "") or ""
    buyer_org = getattr(lead, "buyer_org", "") or ""
    if not dm_name or not buyer_org:
        return None

    queries = [
        f'"{dm_name}" "{buyer_org}" contact',
        f'"{dm_name}" "{buyer_org}" mobile',
    ]
    for q in queries:
        try:
            results = search_engine.search(q, country="india", pages=1)
        except Exception:
            continue
        for res in results:
            snippet = (res.get("snippet") or "") + " " + (res.get("title") or "")
            candidates = extract_phone_candidates(snippet)
            if not candidates:
                continue
            for c in candidates:
                score = score_association(c, snippet, dm_name, "")
                if score >= 50 and c["type"] == "mobile":
                    return (c["phone"], c["type"], "unverified", "search", score)
    return None


def _stage_4_directory(lead, search_engine):
    """Stage 4: JustDial / IndiaMART page fetch (company-level only)."""
    buyer_org = getattr(lead, "buyer_org", "") or ""
    if not buyer_org:
        return None

    queries = [
        f'"{buyer_org}" site:justdial.com',
        f'"{buyer_org}" site:indiamart.com',
    ]
    for q in queries:
        try:
            results = search_engine.search(q, country="india", pages=1)
        except Exception:
            continue
        for res in results[:1]:
            url = (res.get("link") or "").strip()
            if not url:
                continue
            try:
                from common import fetch_page
                content = fetch_page(url)
            except Exception:
                continue
            candidates = extract_phone_candidates(content)
            for c in candidates:
                if c["type"] in ("mobile", "landline"):
                    return (c["phone"], c["type"], "unverified", "directory", 30)
    return None


def find_company_phone(lead, search_engine=None):
    """Fallback: find company-level phone when no DM phone is available.

    ALWAYS returns a 5-tuple, never None — even when no phone is found.
    """
    text = getattr(lead, "raw_text", "") or ""
    candidates = extract_phone_candidates(text)
    for c in candidates:
        if c["type"] in ("mobile", "landline", "tollfree"):
            return (c["phone"], c["type"], "unverified", "content", 20)

    if search_engine:
        result = _stage_4_directory(lead, search_engine)
        if result:
            return result
    return ("", "", "", "", 0)
