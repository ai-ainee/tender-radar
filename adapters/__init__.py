"""adapters - Source adapter framework for Radar Scout V3.

Every adapter implements the same interface:
  - name:      short slug for logging ("firsttender", "serper")
  - category:  primary lead category ("tender", "filing", ...)
  - priority:  run order (lower = first)
  - is_enabled(settings): True if configured
  - fetch(settings):      returns List[StandardLead], never raises

The orchestrator (radar.py) calls each adapter in priority order,
collects all StandardLeads, then feeds them through the pipeline.

Adding a new source = drop a file in adapters/ and register it.
"""
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from urllib.parse import urlparse

log = logging.getLogger("RadarScout")


# ═══════════════════════════════════════════════════════════════════════
# SHARED CONSTANTS — vendor domain + content page detection
# ═══════════════════════════════════════════════════════════════════════

# Vendor domains — a URL on any of these is a VENDOR of a target product,
# never a buyer. Route to Partners, not Inbox.
VENDOR_DOMAIN_KEYWORDS = (
    # Autodesk family (the vendors of your target portfolio)
    "autodesk.com", "autocad.com", "revit.com",
    "navisworks.autodesk", "bim360.autodesk", "forge.autodesk",
    "inventor.autodesk", "construction.autodesk",
    # Other CAD/BIM software vendors
    "d5render.com",
    "sofistik.com",
    "imaginit.com",
    "grabcad.com",
    "bentley.com",
    "12d.com",
    "vectorworks.net",
    "rhino3d.com",
    "sketchup.com",
    "archicad.com",
  )

# Content-page URL patterns — blog / article / resources etc.
# These CAN contain useful signals but are never procurement notices.
# Demote confidence to LOW (routes to Review, never Inbox).
CONTENT_URL_RE = re.compile(
    r"/(blog|article|insights?|whitepaper|webinar|case-?stud|case-?studies|press-?release|newsroom)(/|$)",
    re.I,
)


# ── Learned vendor domains (synced from Partners tab at startup) ──────
# Populated by sync_vendor_domains_from_crm() — see radar.py main().
# Contains every domain previously classified as SELLER and inserted into
# the Partners & Suppliers tab. Partners tab is the source of truth.
_LEARNED_VENDORS = set()


def sync_vendor_domains_from_crm(sheet):
    """Load vendor domains from the Partners & Suppliers tab.

    Called once at startup from radar.py main(). Populates _LEARNED_VENDORS
    from the CRM's Partners tab so _is_vendor_domain() can catch those URLs
    on subsequent runs without AI.
    """
    global _LEARNED_VENDORS
    try:
        data = sheet.get("get_vendor_domains")
        domains = data.get("domains") or []
        for d in domains:
            if isinstance(d, str) and d.strip():
                _LEARNED_VENDORS.add(d.strip().lower().replace("www.", ""))
        log.info(f"Vendor domains synced from Partners tab: {len(_LEARNED_VENDORS)}")
    except Exception as e:
        log.info(f"Vendor domain sync unavailable ({str(e)[:80]}) — using static list only")


def _is_vendor_domain(url):
    """True if URL's hostname is a vendor (static seed OR Partners tab)."""
    try:
        host = urlparse(url or "").netloc.lower().split(":")[0]
        if not host:
            return False
        host = host.replace("www.", "")
        for kw in VENDOR_DOMAIN_KEYWORDS:
            if host == kw or host.endswith("." + kw):
                return True
        for d in _LEARNED_VENDORS:
            if host == d or host.endswith("." + d):
                return True
    except Exception:
        pass
    return False


def _is_content_page(url):
    """True if URL looks like a blog/article/resources page."""
    return bool(CONTENT_URL_RE.search(url or ""))


def normalize_company_name(s):
    """Mirror of normName_ in Apps Script for cross-checking company names."""
    return re.sub(r"[^\w]", "", str(s or "").lower())


# ═══════════════════════════════════════════════════════════════════════
# CompanyDedup — pre-flight company membership check
# ═══════════════════════════════════════════════════════════════════════

class CompanyDedup:
    """Pre-flight company check: skip URLs whose company is already known.

    Loads a list of known company names from:
      1. Local seen_companies.txt (persists across runs)
      2. Apps Script CRM via get_all_companies action (instant full snapshot)

    Matches via:
      - Normalized substring
      - Word overlap (2+ words shared, half the company name present)
      - Acronym (CPWD ↔ Central Public Works Department)
    """

    SUFFIX_WORDS = {
        "ltd", "limited", "pvt", "private", "inc", "corp",
        "corporation", "llp", "company", "the", "and", "of",
    }

    def __init__(self, sheet=None, cache_file="seen_companies.txt"):
        self.known = set()          # normalized names
        self.originals = {}         # normalized -> original display
        self.cache_file = cache_file
        self._load_local()
        if sheet is not None:
            self._load_from_crm(sheet)

       # ── STOPLIST — names that are NOT real companies and must never be
    # added to the pre-check set. These are aggregator brands, AI-generated
    # aggregate labels, and generic descriptors that polluted the CRM
    # during earlier runs.
    STOPLIST = {
        # Aggregator brands (URL-driven, not buyers)
        "firsttender", "first tender", "tenderdetail", "tender detail",
        "tender247", "tender 247", "bidassist", "bid alert", "bidalert",
        "naukri", "glassdoor", "indeed", "linkedin", "internshala",
        "instahyre", "bebee", "jobhai", "randstad", "foundit", "adzuna",
        "tenderapex", "tender apex", "thetenders", "globaltenders",
        "tenderfiles", "tendernews", "tendernews com", "tenders info",
        "justdial", "indiamart", "sulekha", "tradeindia", "exportersindia",
        "biharinfratales", "tendershark", "bidassist com",
        # AI-generated aggregate labels (from earlier buggy runs)
        "various companies", "multiple companies", "multiple firms",
        "various firms", "all companies", "indian organisations",
        "indian organizations", "various buyers", "multiple buyers",
        "sector", "industry", "market", "tenders portal", "assam tenders portal",
        "state government departments public sector banks",
        "indian firms", "indian firms aggregate",
        "commercial service building project",
        # Training institutes / service providers that should never be "buyers"
        "cadd centre", "cad training", "training institute",
        "cads software india", "edc engineering design center",
        "engineering design center",
        # Placeholder artifacts
        "unknown", "not available", "n a", "na", "unidentified",
    }

    def _add(self, name):
        """Add a company name to the known set. Rejects stoplisted/generic names."""
        if not name:
            return
        norm = normalize_company_name(name)
        if not norm or len(norm) < 8:
            return
        # Reject stoplist matches (case-insensitive, exact-after-normalize)
        if norm in {normalize_company_name(s) for s in self.STOPLIST}:
            return
        # Reject names that are 90%+ generic English words
        words = [w.lower() for w in re.findall(r"\b[a-zA-Z]+\b", name)]
        meaningful = [w for w in words if w not in self.SUFFIX_WORDS and len(w) >= 3]
        if len(meaningful) < 2:
            return
        # Reject if ALL meaningful words are on a small generic list
        GENERIC = {"various", "multiple", "several", "all", "any", "many",
                   "companies", "firms", "organisations", "organizations",
                   "buyers", "sellers", "contractors", "suppliers",
                   "tenders", "bids", "projects", "portal", "report", "survey"}
        if all(w in GENERIC for w in meaningful):
            return
        self.known.add(norm)
        if norm not in self.originals:
            self.originals[norm] = name

    def _load_local(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        orig = line.strip()
                        if orig:
                            self._add(orig)
                log.info(f"CompanyDedup: {len(self.known)} known from local cache")
        except Exception as e:
            log.debug(f"CompanyDedup local cache error: {str(e)[:80]}")

    def _load_from_crm(self, sheet):
        try:
            data = sheet.get("get_all_companies")
            names = data.get("companies") or []
            loaded = 0
            for n in names:
                if isinstance(n, str) and n.strip():
                    self._add(n)
                    loaded += 1
            log.info(f"CompanyDedup: {loaded} companies loaded from CRM")
        except Exception as e:
            log.info(f"CompanyDedup: CRM snapshot unavailable ({str(e)[:80]}) — using local only")

    def mark(self, name):
        """Record a company as known after push (success or duplicate)."""
        if not name:
            return
        self._add(name)
        try:
            with open(self.cache_file, "a", encoding="utf-8") as f:
                f.write(name.strip() + "\n")
        except Exception:
            pass

    def find_in(self, title, snippet=""):
        """Return matching original name if a known company appears in title+snippet.

        Stricter matching than before:
        - Require 12+ char substring match (was 6)
        - Require 3+ word overlap with 60%+ ratio (was 2+ / 50%)
        - Skip if title is very short (<15 chars) — likely a category page
        - Skip if title's first word is a generic descriptor
        """
        if not self.known:
            return ""
        text = (title or "").strip()
        snippet_text = (snippet or "").strip()
        combined = text + " " + snippet_text
        if len(combined) < 15:
            return ""

        text_norm = normalize_company_name(combined)
        if not text_norm:
            return ""

        # Skip pre-check entirely if title starts with a generic descriptor
        GENERIC_STARTERS = {"various", "multiple", "several", "all", "any", "many",
                            "top", "best", "list", "browse", "search"}
        first_word = (text.split()[0].lower() if text.split() else "")
        if first_word in GENERIC_STARTERS:
            return ""

        # Fast path: 12+ char substring match only
        for norm in self.known:
            if len(norm) >= 12 and norm in text_norm:
                return self.originals.get(norm, norm)

        # Word-overlap match — strict: 3+ shared words AND 60%+ of company
        text_words = set(w.lower() for w in re.findall(r"\b[a-zA-Z]{4,}\b", combined))
        if text_words:
            for norm, orig in self.originals.items():
                orig_words = [w.lower() for w in re.findall(r"\b[a-zA-Z]+\b", orig)]
                meaningful = [w for w in orig_words
                              if w not in self.SUFFIX_WORDS and len(w) >= 4]
                if len(meaningful) < 3:
                    continue
                overlap = set(meaningful) & text_words
                if len(overlap) >= 3 and len(overlap) / len(meaningful) >= 0.6:
                    return orig

        # Acronym match (CPWD ↔ Central Public Works Department) — unchanged
        text_upper = combined.upper()
        for norm, orig in self.originals.items():
            caps_words = re.findall(r"\b[A-Z][A-Za-z]{2,}\b", orig)
            meaningful = [w for w in caps_words if w.lower() not in self.SUFFIX_WORDS]
            if len(meaningful) < 3:
                continue
            full_acronym = "".join(w[0] for w in meaningful).upper()
            if len(full_acronym) < 3:
                continue
            for n in range(3, len(full_acronym) + 1):
                acr = full_acronym[:n]
                if re.search(rf"\b{acr}\b", text_upper):
                    return orig

        return ""

    def __len__(self):
        return len(self.known)


# ═══════════════════════════════════════════════════════════════════════
# SourceAdapter base class
# ═══════════════════════════════════════════════════════════════════════

class SourceAdapter(ABC):
    """Base class. All adapters inherit from this."""

    name: str = ""
    category: str = "news"
    priority: int = 100

    def is_enabled(self, settings: Dict[str, Any]) -> bool:
        return bool(self.name)

    @abstractmethod
    def fetch(self, settings: Dict[str, Any]) -> List:
        ...

    def safe_fetch(self, settings: Dict[str, Any]) -> List:
        try:
            if not self.is_enabled(settings):
                log.debug(f"Adapter {self.name}: disabled")
                return []
            leads = self.fetch(settings)
            if not isinstance(leads, list):
                log.warning(f"Adapter {self.name}: fetch() returned {type(leads).__name__}, expected list")
                return []
            from standard_lead import StandardLead
            valid = [l for l in leads if isinstance(l, StandardLead)]
            dropped = len(leads) - len(valid)
            if dropped:
                log.warning(f"Adapter {self.name}: dropped {dropped} non-StandardLead items")
            log.info(f"Adapter {self.name}: {len(valid)} lead(s)")
            return valid
        except Exception as e:
            log.warning(f"Adapter {self.name} failed: {str(e)[:200]}")
            return []

    def __repr__(self) -> str:
        return f"<Adapter {self.name} (p{self.priority}, {self.category})>"


# ═══════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════

_ADAPTERS: List[SourceAdapter] = []


def register(adapter: SourceAdapter) -> SourceAdapter:
    global _ADAPTERS
    _ADAPTERS = [a for a in _ADAPTERS if a.name != adapter.name]
    _ADAPTERS.append(adapter)
    return adapter


def get_all() -> List[SourceAdapter]:
    return sorted(_ADAPTERS, key=lambda a: a.priority)


def get_enabled(settings: Dict[str, Any]) -> List[SourceAdapter]:
    return [a for a in get_all() if a.is_enabled(settings)]


def reset():
    global _ADAPTERS
    _ADAPTERS = []


def _auto_register():
    adapter_modules = [
        ("firsttender", "adapters.firsttender", "FirstTenderAdapter"),
        ("tenderdetail", "adapters.tenderdetail", "TenderDetailAdapter"),
        ("gem", "adapters.gem", "GeMAdapter"),
        ("cppp", "adapters.cppp", "CPPPAdapter"),
        ("bse", "adapters.bse", "BSEAdapter"),
        ("nse", "adapters.nse", "NSEAdapter"),
        ("search", "adapters.search", "SearchAdapter"),
        ("hiring", "adapters.hiring", "HiringAdapter"),
    ]
    for slug, module_path, class_name in adapter_modules:
        try:
            mod = __import__(module_path, fromlist=[class_name])
            cls = getattr(mod, class_name)
            register(cls())
            log.debug(f"Registered adapter: {slug}")
        except ImportError:
            log.debug(f"Adapter {slug} not present — skipped")
        except Exception as e:
            log.warning(f"Adapter {slug} failed to load: {str(e)[:100]}")


_auto_register()
