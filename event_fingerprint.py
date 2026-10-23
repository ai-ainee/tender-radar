"""event_fingerprint.py — Structural fingerprint for corporate signal dedup.

Corporate signals (capex, partnerships, order wins) have no canonical ID
like tenders do. The same ₹5,000 cr investment gets reported by BSE, ET,
Mint, Business Standard, and the company blog — all describing one event
with different wording.

The fingerprint is a structured hash of the event's core attributes:
  event_type + actor + numeric value + unit + month + location

Two sources describing the same event produce the same fingerprint.
"""
import os


def fingerprint_key(fp):
    """Returns a stable string key from an AI-extracted fingerprint dict.

    Returns '' if the fingerprint is not usable for dedupe (missing
    event_type or primary_actor).

    Fallback behaviour: if no key_value (numeric magnitude) is present,
    the location is dropped from the key. This prevents over-deduping
    of small events that share the same actor + type + month but are
    actually different (e.g. "RIL opens 2 offices" vs "RIL opens 3 offices"
    on the same day — without a number, we can't tell if they're the same).
    """
    if not isinstance(fp, dict):
        return ""

    event_type = str(fp.get("event_type") or "").lower().strip()
    actor      = str(fp.get("primary_actor") or "").lower().replace(" ", "")
    value      = str(fp.get("key_value") or "").strip()
    unit       = str(fp.get("key_unit") or "").lower().strip()
    month      = str(fp.get("rough_date") or "")[:7]     # YYYY-MM
    location   = str(fp.get("location_key") or "").lower().replace(" ", "")

    # Minimum viable fingerprint requires event_type + actor
    if not event_type or not actor:
        return ""

    # If we have no numeric magnitude, drop location to avoid over-deduping
    if not value:
        location = ""

    return "|".join([event_type, actor, value, unit, month, location])


def load_cache(path):
    """Loads the set of previously-seen fingerprints from a text file.

    The file is created if missing. Unreadable file = empty cache
    (fails safe: dedupe gets weaker, nothing crashes).
    """
    fps = set()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                fps.update(x.strip() for x in f if x.strip())
        else:
            open(path, "a", encoding="utf-8").close()
    except Exception:
        pass
    return fps


def mark_cache(path, key):
    """Appends a fingerprint key to the cache file.

    Silent on failure — a write failure means dedupe gets weaker next run,
    not that the run crashes.
    """
    if not key:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(key + "\n")
    except Exception:
        pass
