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


def _is_vendor_domain(url):
    """True if URL's hostname is a known vendor of a target product."""
    try:
        host = urlparse(url or "").netloc.lower().split(":")[0]
        if not host:
            return False
        for kw in VENDOR_DOMAIN_KEYWORDS:
            if host == kw or host.endswith("." + kw):
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

    def _add(self, name):
        norm = normalize_company_name(name)
        if norm and len(norm) >= 6:
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
        """Return matching original name if any known company appears in title+snippet."""
        if not self.known:
            return ""
        text = (title or "") + " " + (snippet or "")
        if len(text) < 8:
            return ""

        text_norm = normalize_company_name(text)
        if not text_norm:
            return ""

        # Fast path: substring match
        for norm in self.known:
            if norm in text_norm:
                return self.originals.get(norm, norm)

        # Word-overlap match (2+ shared words, half present)
        text_words = set(w.lower() for w in re.findall(r"\b[a-zA-Z]{3,}\b", text))
        if text_words:
            for norm, orig in self.originals.items():
                orig_words = [w.lower() for w in re.findall(r"\b[a-zA-Z]+\b", orig)]
                meaningful = [w for w in orig_words if w not in self.SUFFIX_WORDS and len(w) >= 3]
                if len(meaningful) < 2:
                    continue
                overlap = set(meaningful) & text_words
                if len(overlap) >= 2 and len(overlap) / len(meaningful) >= 0.5:
                    return orig

        # Acronym match (CPWD ↔ Central Public Works Department)
        text_upper = text.upper()
        for norm, orig in self.originals.items():
            caps_words = re.findall(r"\b[A-Z][A-Za-z]{2,}\b", orig)
            meaningful = [w for w in caps_words if w.lower() not in self.SUFFIX_WORDS]
            if len(meaningful) < 3:
                continue
            full_acronym = "".join(w[0] for w in meaningful).upper()
            if len(full_acronym) < 3:
                continue
            # Try progressively longer prefix acronyms
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
