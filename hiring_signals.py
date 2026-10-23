"""hiring_signals.py — Predictive hiring signals from Naukri.

Companies hiring CCTV engineers, BIM coordinators, security system
engineers, etc. are typically 60-90 days away from a deployment. These
leads are routed to Review (not Inbox) because hiring alone is noise —
but combined with a BSE/NSE capex filing, they corroborate a real buying
signal.
"""
import re
from urllib.parse import quote_plus

from common import log, fetch_page

# Roles that predict near-future deployments, keyed by target keyword.
# Add entries here matching your Target Products list.
ROLE_PATTERNS = {
    "CCTV":            ["cctv engineer", "cctv technician"],
    "IP Surveillance": ["network video engineer", "vms engineer"],
    "BIM":             ["bim coordinator", "revit engineer"],
    "Security":        ["security system engineer", "access control engineer"],
}


def search_naukri(role_keyword, max_results=10):
    """Search Naukri for a role keyword and return company names found.

    Returns a list of {company, role, source} dicts.
    Returns [] on error (never raises).
    """
    url = f"https://www.naukri.com/{quote_plus(role_keyword)}-jobs"
    try:
        content = fetch_page(url, max_chars=10000)
        if not content or len(content) < 200:
            return []
        # Naukri result pages have company names like "Acme Pvt Ltd" near
        # experience/salary markers. Try a few patterns.
        companies = re.findall(
            r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){1,3})\s+(?:Ltd|Limited|Pvt|Inc|Corp|Technologies|Solutions|Systems)\b",
            content
        )
        if not companies:
            # Fallback — "Company: <name>" blocks
            companies = re.findall(r"Company[:\s]+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){1,3})", content)
        unique = list(dict.fromkeys(c.strip() for c in companies if 4 < len(c.strip()) < 60))[:max_results]
        return [{"company": c, "role": role_keyword, "source": "Naukri"} for c in unique]
    except Exception as e:
        log.warning(f"Naukri search for '{role_keyword}' failed: {e}")
        return []


def collect_hiring_signals(targets, max_per_role=8):
    """Collects hiring signals across all target product keywords.

    Returns a list of {company, role, source} dicts.
    """
    out = []
    if not targets:
        return out
    for target in targets:
        roles = ROLE_PATTERNS.get(target, [str(target)])
        for role in roles[:2]:              # max 2 roles per target
            hits = search_naukri(role, max_per_role)
            out.extend(hits)
            log.info(f"  Hiring signal: {role} -> {len(hits)} companies hiring")
    return out
