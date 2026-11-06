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

    Without a distinctive value (or named project), unrelated events from
    the same actor and month must NOT collapse into one opportunity.
    """
    if not isinstance(fp, dict):
        return ""

    event_type = str(fp.get("event_type") or "").lower().strip()
    actor      = str(fp.get("primary_actor") or "").lower().replace(" ", "")
    value      = str(fp.get("key_value") or "").strip().replace(",", "")
    # Normalize numbers: "5000" and "5000.0" → "5000"
    try:
        if value:
            value = f"{float(value):g}"
    except ValueError:
        pass
    unit       = str(fp.get("key_unit") or "").lower().strip()
    month      = str(fp.get("rough_date") or "")[:7]     # YYYY-MM
    location   = str(fp.get("location_key") or "").lower().replace(" ", "")

    # Minimum viable fingerprint requires event_type + actor
    if not event_type or not actor:
        return ""

    project = str(fp.get("project_key") or "").lower().strip()
    project = "".join(c for c in project if c.isalnum())[:70]
    # A location alone is far too broad (e.g. one company may post several
    # jobs and open several stores in Delhi during the same month).
    if not value and len(project) < 8:
        return ""
    if not month:
        return ""
    return "|".join([event_type, actor, value or project, unit, month, location])


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
