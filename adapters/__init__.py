"""adapters - Source adapter framework for Radar Scout V3.

Every adapter implements the same interface:
  - name:      short slug for logging ("firsttender", "serper")
  - category:  primary lead category ("tender", "filing", ...)
  - priority:  run order (lower = first; deterministic gating uses this)
  - is_enabled(settings): True if configured (API key present, source healthy)
  - fetch(settings):      returns List[StandardLead], never raises

The orchestrator (radar.py) calls each adapter in priority order,
collects all StandardLeads, then feeds them through the pipeline.

Adding a new source = drop a file in adapters/ and register it.
Removing = delete or set is_enabled() → False.
"""
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any

log = logging.getLogger("RadarScout")


class SourceAdapter(ABC):
    """Base class. All adapters inherit from this."""

    name: str = ""
    category: str = "news"
    priority: int = 100       # lower = runs earlier

    def is_enabled(self, settings: Dict[str, Any]) -> bool:
        """Override to gate on settings (country match, API key presence, etc).

        Default: enabled if name is set.
        """
        return bool(self.name)

    @abstractmethod
    def fetch(self, settings: Dict[str, Any]) -> List:
        """Fetch leads from this source.

        Args:
            settings: dict from load_settings() — includes targets, countries,
                      states, cities, banned lists, api_keys, etc.

        Returns:
            List[StandardLead]. Empty list if source has no results.
            Never raises — log warnings and return [] on failure.
        """
        ...

    def safe_fetch(self, settings: Dict[str, Any]) -> List:
        """Wrapper that catches exceptions and enforces the contract."""
        try:
            if not self.is_enabled(settings):
                log.debug(f"Adapter {self.name}: disabled")
                return []
            leads = self.fetch(settings)
            if not isinstance(leads, list):
                log.warning(f"Adapter {self.name}: fetch() returned {type(leads).__name__}, expected list")
                return []
            # Filter out anything that isn't a StandardLead
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


# ── Registry ──────────────────────────────────────────────────────────
_ADAPTERS: List[SourceAdapter] = []


def register(adapter: SourceAdapter) -> SourceAdapter:
    """Register an adapter instance. Idempotent per name."""
    global _ADAPTERS
    _ADAPTERS = [a for a in _ADAPTERS if a.name != adapter.name]
    _ADAPTERS.append(adapter)
    return adapter


def get_all() -> List[SourceAdapter]:
    """Return all registered adapters, sorted by priority."""
    return sorted(_ADAPTERS, key=lambda a: a.priority)


def get_enabled(settings: Dict[str, Any]) -> List[SourceAdapter]:
    """Return only adapters that are enabled for these settings."""
    return [a for a in get_all() if a.is_enabled(settings)]


def reset():
    """Clear the registry (used in tests)."""
    global _ADAPTERS
    _ADAPTERS = []


# ── Auto-registration on import ───────────────────────────────────────
def _auto_register():
    """Import and register all built-in adapters.

    Runs on first import of this package. Missing adapter files
    are skipped (won't crash the import).
    """
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
