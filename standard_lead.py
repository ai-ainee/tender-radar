"""standard_lead.py - Universal lead schema for Radar Scout V3.

Every source adapter produces StandardLead. The pipeline downstream
(normalizer → gates → AI → router → enrichment → sheet) consumes only
this schema, so adding/removing sources never touches the pipeline.

All fields are strings or simple types. Serialisable via to_dict/from_dict
for caching, logging, and cross-process transport.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any
import hashlib


# Canonical values for categorical fields
CATEGORIES   = ("tender", "filing", "hiring", "news", "expansion")
BUYER_TYPES  = ("PSU", "Government", "Private", "Unknown")
ROLES        = ("BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER", "IRRELEVANT")
CONFIDENCES  = ("HIGH", "MEDIUM", "LOW")
PHONE_TYPES  = ("mobile", "landline", "tollfree", "fax", "voip", "")
PHONE_STATUS = ("verified", "unverified", "")
EMAIL_STATUS = ("found", "inferred", "generic", "unknown", "")
GENUINENESS  = ("GENUINE", "JOB_PORTAL", "HIRING_SIGNAL", "STUDENT", "BLOG",
                "RESELLER", "AGGREGATOR", "OTHER", "")
FRESHNESS    = ("FRESH", "RECENT", "AGING", "STALE", "")


@dataclass
class StandardLead:
    """One lead, source-agnostic."""

    # ── Identity ──────────────────────────────────────────────────────
    source:   str                      # "firsttender" | "serper" | "hiring" | ...
    category: str                      # one of CATEGORIES
    url:      str                      # canonical URL
    id:       str = ""                 # stable hash, computed if empty

    # ── Content ───────────────────────────────────────────────────────
    title:       str = ""
    snippet:     str = ""
    raw_text:    str = ""              # full text if fetched, else ""
    industry:    str = ""              # AI-extracted industry label
    structured:  Dict[str, Any] = field(default_factory=dict)  # source-specific payload

    # ── Buyer (structured sources fill this; search sources leave empty) ──
    buyer_org:   str = ""
    buyer_dept:  str = ""
    buyer_type:  str = "Unknown"       # one of BUYER_TYPES

    # ── Geography ─────────────────────────────────────────────────────
    country: str = ""
    state:   str = ""
    city:    str = ""

    # ── Procurement facts (populated when source provides structured data) ──
    value_inr:      float = 0.0
    deadline:       str = ""           # ISO date "2026-11-15" or ""
    tender_id:      str = ""
    filing_id:      str = ""
    filing_type:    str = ""           # "order_win" | "capex" | "partnership" | ...
    raw_published_at: str = ""         # ISO date

    # ── Derived ───────────────────────────────────────────────────────
    event_fingerprint: str = ""

   # ── Contact (populated by enrichment, not source) ─────────────────
    dm_name:          str = ""
    dm_title:         str = ""
    phone:            str = ""          # raw phone from AI verdict (before validation)
    dm_email:         str = ""
    dm_email_status:  str = ""         # one of EMAIL_STATUS
    dm_phone:         str = ""         # E.164 format "+91XXXXXXXXXX"
    dm_phone_type:    str = ""         # one of PHONE_TYPES
    dm_phone_status:  str = ""         # one of PHONE_STATUS
    dm_phone_source:  str = ""         # "tender_pdf" | "company_site" | ...
    dm_phone_score:   int = 0          # 0-100 association confidence
    dm_linkedin:      str = ""

    # ── Company fallback ──────────────────────────────────────────────
    company_phone:        str = ""
    company_phone_type:   str = ""
    company_phone_source: str = ""

    # ── WhatsApp ──────────────────────────────────────────────────────
    whatsapp_available: bool = False

    # ── AI evaluation (populated by evaluator) ────────────────────────
    is_valid:         bool = False
    confidence:       str = ""         # one of CONFIDENCES
    entity_role:      str = ""         # one of ROLES
    genuineness:      str = ""         # one of GENUINENESS
    expiry_status:    str = ""         # "ACTIVE" | "CLOSED" | "COMPLETED" | "UNKNOWN"
    expiry_evidence:  str = ""
    signal_freshness: str = ""         # one of FRESHNESS
    fit_score:        int = 0          # 0-100
    lead_score:       int = 50         # 0-100 (AI's overall score)
    why_engage_now:   str = ""
    target_solution:  str = ""
    product_usage:    str = ""
    upcoming_events:  str = ""
    project_name:     str = ""
    competitor_winner: str = ""
    winning_value:    str = ""

    # ── Metadata ──────────────────────────────────────────────────────
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── Derived helpers ───────────────────────────────────────────────

    def compute_id(self) -> str:
        """Stable ID. Uses tender_id/filing_id if available, else URL."""
        key = self.tender_id or self.filing_id or self.url or ""
        base = f"{self.source}|{key}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    def __post_init__(self):
        if not self.id:
            self.id = self.compute_id()
        # Normalise category
        if self.category not in CATEGORIES:
            self.category = "news"
        # Normalise buyer_type
        if self.buyer_type not in BUYER_TYPES:
            self.buyer_type = "Unknown"

    # ── Serialisation ─────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StandardLead":
        known = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    # ── Convenience ───────────────────────────────────────────────────

    def is_structured(self) -> bool:
        """True if source provided procurement facts (value, deadline, ID)."""
        return bool(self.tender_id or self.filing_id or self.value_inr > 0 or self.deadline)

    def has_mobile(self) -> bool:
        return self.dm_phone_type == "mobile" and self.dm_phone_status in ("verified", "unverified")

    def has_contact(self) -> bool:
        return bool(self.dm_email or self.dm_phone)

    def __repr__(self) -> str:
        return (f"<StandardLead {self.source}/{self.category} "
                f"buyer={self.buyer_org[:30]!r} "
                f"value={self.value_inr} "
                f"id={self.id[:8]}>")


# ── Quick self-test ───────────────────────────────────────────────────
if __name__ == "__main__":
    lead = StandardLead(
        source="firsttender",
        category="tender",
        url="https://firsttender.com/bids/cctv-tender-noida.html",
        title="CCTV Camera Supply - Noida Authority",
        buyer_org="Noida Authority",
        buyer_type="Government",
        country="India",
        state="Uttar Pradesh",
        city="Noida",
        value_inr=4500000.0,
        deadline="2026-11-15",
        tender_id="GEM/2026/B/123456",
    )
    print(f"Created: {lead}")
    print(f"ID: {lead.id}")
    print(f"Is structured: {lead.is_structured()}")
    print(f"Has contact: {lead.has_contact()}")

    # Roundtrip
    d = lead.to_dict()
    lead2 = StandardLead.from_dict(d)
    print(f"Roundtrip OK: {lead2.id == lead.id}")
