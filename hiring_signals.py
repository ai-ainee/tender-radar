"""hiring_signals.py — Hiring signals driven by Target Products from Settings.

NO hardcoded role keywords. For each Target Product, the LLM generates the
job roles that directly consume that product. Cached per-run to avoid
repeat LLM calls.

If the LLM is unavailable, hiring signals are skipped for that run.
"""
import re
from urllib.parse import quote_plus

from common import log, fetch_page

# Per-run cache — avoids repeat LLM calls within a single execution
_ROLE_CACHE = {}


def generate_role_patterns(llm, targets):
    """Uses the LLM to generate relevant job roles for each Target Product.

    Returns: {"<product>": ["role1", "role2", ...]} or {} on failure.
    Cached per-run.
    """
    cache_key = "|".join(sorted(str(t).lower() for t in targets if t))
    if cache_key in _ROLE_CACHE:
        return _ROLE_CACHE[cache_key]

    if not llm or not llm.available():
        return {}

    target_list = "\n".join(f"- {t}" for t in targets if t)
    prompt = f"""You are a B2B sales intelligence analyst.

For each Target Product below, list the JOB ROLES that a company would hire for
when they are about to deploy, expand, or manage that product.

Target Products:
{target_list}

RULES:
- Only roles that DIRECTLY consume, administer, or deploy the product.
- 3-6 roles per product.
- Include junior AND senior variants.
- Exclude: Sales, HR, Admin, Marketing, Management-only roles.
- Include procurement-adjacent roles when relevant (e.g. "Software Asset Manager").
- Use role titles as they appear on Indian job boards (Naukri format).

Return ONLY JSON: {{"roles_by_product": {{"<product>": ["role1", "role2", ...]}}}}"""

    data = llm.generate_json(prompt, validator=lambda d: isinstance(d, dict) and "roles_by_product" in d)
    if not data:
        log.warning("hiring_signals: LLM could not generate role patterns; skipping hiring signals this run")
        return {}

    roles_by_product = data.get("roles_by_product") or {}
    if not isinstance(roles_by_product, dict):
        return {}
    _ROLE_CACHE[cache_key] = roles_by_product
    log.info(f"hiring_signals: LLM generated {sum(len(v) for v in roles_by_product.values() if isinstance(v, list))} role(s) across {len(roles_by_product)} product(s)")
    return roles_by_product


def search_naukri(role_keyword, max_results=10):
    """Searches Naukri for a role keyword and returns company names found."""
    url = f"https://www.naukri.com/{quote_plus(role_keyword)}-jobs"
    try:
        content = fetch_page(url, max_chars=10000)
        if not content or len(content) < 200:
            return []
        companies = re.findall(
            r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){1,3})\s+"
            r"(?:Ltd|Limited|Pvt|Inc|Corp|Technologies|Solutions|Systems|Engineering|Group|Enterprises)\b",
            content
        )
        if not companies:
            companies = re.findall(r"Company[:\s]+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){1,3})", content)
        # Filter training institutes / academies — never buyers
        BAD = ("training", "institute", "academy", "cadd centre", "university",
               "college", "school", "coaching")
        unique = []
        for c in companies:
            name = c.strip()
            if not (4 < len(name) < 60):
                continue
            if any(bad in name.lower() for bad in BAD):
                continue
            unique.append(name)
        return list(dict.fromkeys(unique))[:max_results]
    except Exception as e:
        log.warning(f"Naukri search for '{role_keyword}' failed: {e}")
        return []


def collect_hiring_signals(targets, llm, max_per_role=8):
    """Collects hiring signals using LLM-generated role patterns.

    NO hardcoded keywords — everything derived from Target Products.
    Requires `llm` to be passed in.
    """
    out = []
    if not targets or not llm:
        return out

    roles_by_product = generate_role_patterns(llm, targets)
    if not roles_by_product:
        return out

    for target in targets:
        if not target:
            continue
        roles = roles_by_product.get(str(target), [])
        if not isinstance(roles, list):
            continue
        for role in roles[:3]:                      # cap to 3 roles per product
            hits = search_naukri(str(role), max_per_role)
            for h in hits:
                out.append({
                    "company": h,
                    "role": str(role),
                    "target_product": target,
                    "source": "Naukri",
                })
            if hits:
                log.info(f"  Hiring: {role} -> {len(hits)} companies (target: {target})")
    return out
