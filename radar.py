#!/usr/bin/env python3
"""
radar.py - Radar Scout harvester.
Reads settings from your Google Sheet, searches the web, scores pages with a free LLM,
and pushes qualified leads back into the Sheet. Sends a Telegram summary at the end.
"""
import hashlib
import os
import random
import re
import sys
import time
import uuid
import json
from datetime import datetime
from urllib.parse import urlparse

from common import (LLMRouter, SearchEngine, SheetAPI, SheetError, Telegram, clean_url, env_int, extract_phone,
                    fetch_page, log, normalize_company, tg_escape)

CACHE_FILE = "seen_links.txt"
FINGERPRINT_FILE = "seen_fingerprints.txt"
MAX_FINGERPRINTS = 30000
SHEET_INBOX = "📥 Inbox"
SHEET_REVIEW = "⚠️ Needs Review"
SHEET_PARTNERS = "🤝 Partners & Suppliers"
SHEET_TRASH = "🗑 AI_Trash"

TRACK_KEYS = ["TRACK_1_PUBLIC_TENDERS", "TRACK_2_CORPORATE_PROCUREMENT",
              "TRACK_3_BUSINESS_EXPANSION", "TRACK_4_COMMERCIAL_HIRING"]

class StopRun(Exception):
    """Raised to leave the campaign loops early."""
  
def load_cache(sheet):
    seen = set()
    if not os.path.exists(CACHE_FILE): open(CACHE_FILE, "a", encoding="utf-8").close()
    try:
        with open(CACHE_FILE, "r", encoding="utf-8", errors="ignore") as f:
            seen.update(clean_url(line) for line in f if line.strip())
    except Exception as e: log.warning(f"Local cache unreadable: {e}")
    try:
        data = sheet.post("get_all_urls")
        seen.update(clean_url(u) for u in data.get("urls", []) if isinstance(u, str) and u.strip())
        log.info(f"Cache synchronised: {len(seen)} known links")
    except SheetError as e: log.warning(f"Cloud cache unavailable, continuing with local cache only: {e}")
    seen.discard("")
    return seen
  
def ai_is_gated_portal(llm, url):
    """
    Asks the AI if a given URL is a gated portal, e-procurement site, 
    or tender board requiring authentication/browser rendering.
    """
    prompt = f"""Analyze this URL: "{url}"
Is this website likely to be a government tender portal, e-procurement platform, corporate vendor board, or a site that requires login/cookies to view content?
Return ONLY a valid JSON object: {{"is_portal": true}} or {{"is_portal": false}}"""
    
    try:
        res = llm.generate_json(prompt, validator=lambda d: isinstance(d, dict))
        if res and isinstance(res, dict):
            return bool(res.get("is_portal", False))
    except Exception:
        pass
    return False

def fingerprint(text):
    norm = re.sub(r"[^a-z0-9]", "", (text or "").lower())[:3000]
    return hashlib.sha1(norm.encode()).hexdigest()[:16] if len(norm) >= 200 else ""

def load_fingerprints():
    fps = []
    try:
        if os.path.exists(FINGERPRINT_FILE):
            with open(FINGERPRINT_FILE, "r", encoding="utf-8", errors="ignore") as f:
                fps = [x.strip() for x in f if x.strip()]
        else: open(FINGERPRINT_FILE, "a", encoding="utf-8").close()
        if len(fps) > MAX_FINGERPRINTS:
            fps = fps[-MAX_FINGERPRINTS:]
            with open(FINGERPRINT_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(fps) + "\n")
    except Exception as e: log.warning(f"Fingerprint file unreadable: {e}")
    return set(fps)

def mark_fp(fps, fp):
    if not fp or fp in fps: return
    fps.add(fp)
    try:
        with open(FINGERPRINT_FILE, "a", encoding="utf-8") as f: f.write(fp + "\n")
    except Exception as e: log.warning(f"Could not write fingerprint: {e}")

def mark_seen(seen, url):
    c = clean_url(url)
    if not c or c in seen: return
    seen.add(c)
    try:
        with open(CACHE_FILE, "a", encoding="utf-8") as f: f.write(c + "\n")
    except Exception as e: log.warning(f"Could not write cache: {e}")

def _as_list(value):
    if value is None: return []
    if isinstance(value, str): value = [x for x in value.replace("\n", ",").split(",")]
    return [str(x).strip() for x in value if str(x).strip()]

def load_settings(sheet):
    s = sheet.get("get_settings")
    return {
        "special_prompt": str(s.get("special_prompt") or "").strip(),
        "session_cookies": str(s.get("session_cookies") or "").strip(),
        "targets": _as_list(s.get("target_products")),
        "directives": _as_list(s.get("directives")),
        "industries": _as_list(s.get("industry_keywords")),
        "countries": _as_list(s.get("target_countries")),
        "states": _as_list(s.get("target_states")),
        "banned_kw": [k.lower() for k in _as_list(s.get("banned_keywords"))],
        "banned_sites": [k.lower() for k in _as_list(s.get("banned_websites"))],
        "protected": [k.lower() for k in _as_list(s.get("protected_domains"))],
    }

class QueryGenerator:
    def __init__(self, target, industry, country, states, llm, use_ai=True):
        self.target = target.strip()
        self.industry = industry.strip() if industry and industry != "ALL_SECTORS" else ""
        self.country = (country or "").strip()
        self.states = [s.strip() for s in states if s.strip()]
        self.year = datetime.now().year
        self.llm = llm
        self.use_ai = use_ai

    @staticmethod
    def _normalize(data):
        if not isinstance(data, dict): return {}
        for parent in ("tracks", "queries", "data", "result"):
            if isinstance(data.get(parent), dict):
                data = data[parent]
                break
        out = {}
        for k, v in data.items():
            if isinstance(v, str): v = [v]
            if not isinstance(v, list) or not v: continue
            ck = "".join(ch for ch in str(k).upper() if ch.isalnum())
            queries = [str(x).strip() for x in v if str(x).strip()][:2]
            if not queries: continue
            for n, name in enumerate(TRACK_KEYS, start=1):
                label = name.split("_", 2)[2].replace("_", "")
                if f"TRACK{n}" in ck or label in ck:
                    out[name] = queries
                    break
        return out

    def _ai_tracks(self, prompt):
        if not self.use_ai or not self.llm.available(): return {}
        data = self.llm.generate_json(prompt, validator=lambda d: isinstance(d, dict))
        return self._normalize(data)

    def _merge(self, ai, fallback):
        merged = dict(fallback)
        merged.update({k: v for k, v in ai.items() if v})
        return merged

    def build_special_tracks(self, special_prompt):
        log.info(f"Generating search queries for special directive: '{special_prompt}'")
        sp = special_prompt
        fb = {
            "TRACK_1_PUBLIC_TENDERS": [f"{sp} public tenders", f"{sp} GeM bids"],
            "TRACK_2_CORPORATE_PROCUREMENT": [f"{sp} vendor empanelment", f"{sp} corporate RFQ"],
            "TRACK_3_BUSINESS_EXPANSION": [f"{sp} expansion capex", f"{sp} new facility"],
            "TRACK_4_COMMERCIAL_HIRING": [f"{sp} hiring specialists", f"{sp} team expansion"],
        }
        prompt = f"""You are an expert SEO and B2B search strategist.
MANDATORY CAMPAIGN DIRECTIVE: "{special_prompt}"
Generate Google search queries to find buyers, tenders, or expansions matching this directive.

CRITICAL RULES:
1. Keep queries between 3 and 7 words.
2. No quotes, no operators, plain text only.

TRACK_1_PUBLIC_TENDERS: public tenders, GeM bids, or infrastructure bids.
TRACK_2_CORPORATE_PROCUREMENT: private RFQs or vendor empanelment.
TRACK_3_BUSINESS_EXPANSION: new facilities or commercial project launches.
TRACK_4_COMMERCIAL_HIRING: companies hiring specialists.
Return ONLY JSON with exactly those 4 keys, each an array of 2 strings."""
        return self._merge(self._ai_tracks(prompt), fb)

    def build_tracks(self):
        state = random.choice(self.states) if self.states else ""
        t, ind, yr = self.target, self.industry, self.year
        loc = state or self.country
        loc_s = f" {loc}" if loc else ""
        ind_s = f"{ind} " if ind else ""
        
        # Dynamically switch the state rule based on whether Target States is set in Google Sheets
        if state:
            state_rule = f"Restrict the queries to {state}."
        else:
            state_rule = "DO NOT restrict to a single state (search nationwide)."
        
        fb = {
            "TRACK_1_PUBLIC_TENDERS": [f"{t} tender{loc_s}", f"{ind_s}public tenders{loc_s}"],
            "TRACK_2_CORPORATE_PROCUREMENT": [f"{t} RFQ{loc_s}", f"vendor empanelment {ind_s}{loc_s}"],
            "TRACK_3_BUSINESS_EXPANSION": [f"{t} project expansion{loc_s}", f"{ind_s}new facility construction{loc_s}"],
            "TRACK_4_COMMERCIAL_HIRING": [f"hiring {t} specialist{loc_s}", f"{ind_s}team expansion{loc_s}"],
        }
        
        industry_line = f"INDUSTRY FOCUS: {ind}" if ind else "INDUSTRY: all commercial/industrial sectors"
        prompt = f"""You are an expert SEO and B2B search strategist.
Generate Google search queries to find buyers, tenders, or expansions related to '{t}'.
{industry_line}
{f"Geography: {self.country}" if self.country else ""}
{f"Region: {state}" if state else ""}

CRITICAL RULES:
1. Keep queries between 3 and 7 words.
2. Each track has 2 queries:
   - Query 1: Include the product name '{t}'. {state_rule}
   - Query 2: Search for the broad project or industry requirement. {state_rule}
3. No quotes, no operators, plain text only.

TRACK_1_PUBLIC_TENDERS: government tenders, GeM bids, or public infrastructure bids.
TRACK_2_CORPORATE_PROCUREMENT: private RFQs or vendor empanelment.
TRACK_3_BUSINESS_EXPANSION: new facilities or commercial project launches.
TRACK_4_COMMERCIAL_HIRING: companies hiring specialists.
Return ONLY JSON with exactly those 4 keys, each an array of 2 strings."""
        return self._merge(self._ai_tracks(prompt), fb)
        
def _valid_verdicts(d):
    if isinstance(d, list): return all(isinstance(x, dict) for x in d)
    if isinstance(d, dict): return isinstance(d.get("leads"), list) or "item_index" in d
    return False

class Evaluator:
    def __init__(self, llm):
        self.llm = llm

    @staticmethod
    def _slice(text, max_chars=3000):
        text = (text or "").strip()
        if len(text) <= max_chars: return text
        return f"{text[:2200]}\n\n[...truncated...]\n\n{text[-800:]}"

    def evaluate(self, batch, targets_str, directives_str, industries_str, country, states, geo_rule, ban_rule, special=""):
        items = "\n".join(
            f"--- ITEM {i} ---\nTRACK: {x['track']}\nURL: {x['url']}\n<scraped_data>\n{self._slice(x.get('raw_text'))}\n</scraped_data>\n"
            for i, x in enumerate(batch))
        
        special_block = (f"\n🔥 MASTER CAMPAIGN OVERRIDE (SPECIAL DIRECTIVE): {special}\n"
                         "CRITICAL: Because a special directive is provided, it BYPASSES all standard product/industry rules. "
                         "You MUST evaluate the prospect solely on whether it matches this specific directive.\n") if special else ""

        prompt = f"""You are an elite B2B sales intelligence analyst judging genuine commercial buying intent.
TODAY'S DATE: {datetime.now().strftime('%Y-%m-%d')}
TARGET PORTFOLIO (Products/Solutions): {targets_str}
TARGET DIRECTIVES (e.g. LOA/Award): {directives_str if directives_str else "None specified"}
TARGET INDUSTRIES: {industries_str if industries_str else "All commercial/industrial sectors"}
{f"TARGET GEOGRAPHY: {country}" if country else ""}
{f"REGIONAL FOCUS: {', '.join(states)}" if states else ""}
{geo_rule}
{ban_rule}{special_block}

OBJECTIVE: Find real, identifiable companies / public bodies with an ACTIVE requirement, project, tender, or expansion.

EVALUATION RULES (CRITICAL MATRIX):
0. SPECIAL OVERRIDE: If a "MASTER CAMPAIGN OVERRIDE" is active above, ignore Rules 1 & 2. Judge the lead ENTIRELY on whether it matches that exact directive. (If exact match -> INBOX. If indirectly related -> REVIEW).
1. INBOX (is_valid=true, confidence="HIGH"):
   - The text explicitly mentions ANY product in the TARGET PORTFOLIO.
   - AND it aligns with the Directives OR Industries (if provided).
   - *Fallback:* If only a Product was provided, use your intelligence to qualify if they are an active commercial buyer.
2. REVIEW (is_valid=true, confidence="LOW"):
   - The text strongly matches a TARGET DIRECTIVE (e.g., an LOA/Award) and/or TARGET INDUSTRY...
   - BUT it does NOT explicitly name a Target Portfolio product.
   - INSTEAD, it names a Competitor Product, the same OEM, or RELATED SERVICES (e.g., an awarded contract for "BIM Modeling", "Architectural Design", or "Drafting services" which inherently require the software).
3. EXCLUDE / TRASH (is_valid=false): 
   - Matches a Directive (e.g., an LOA) but the context is completely irrelevant (e.g., catering, janitorial, standard road paving without design).
   - Schools/students, freelancers, blogs, generic global articles.
   - IT RESELLERS: Only use entity_role="SELLER" for IT distributors selling software licenses. (Engineering firms providing drafting/BIM services are SERVICE_USER).
   - STALE LEADS (STRICT 1-MONTH LIMIT): Any awarded contract or milestone that occurred MORE THAN 30 DAYS before TODAY'S DATE.

Return ONLY JSON: {{"leads": [ one object per ITEM ]}} where each object has:
"item_index": integer matching the ITEM number,
"is_valid": boolean,
"confidence": "HIGH" | "MEDIUM" | "LOW",
"entity_role": "BUYER" | "PROJECT_BUYER" | "SERVICE_USER" | "SELLER" | "IRRELEVANT",
"organization": official name of the company / procuring body,
"industry": string, "city": string, "state": string,
"target_solution": the specific product, service, or competitor found,
"why_engage_now": 2-3 sentences (signal, urgency, opportunity),
"product_usage": "Confirmed User" | "Prospective Buyer" | "Competitor" | "Unknown" plus short evidence,
"upcoming_events": tender closing date / milestone, or "Unknown",
"dm_name": contact person if stated in the text else "",
"phone": phone if stated in the text else "",
"lead_score": "Rate the lead from 1 to 100 based on urgency, scale, and match to our products.",
"competitor_winner": "If this article announces a contract award, who won it? (else leave empty)",
"winning_value": "The contract value/amount awarded (else leave empty)",
"project_name": "The name of the project/tender (else leave empty)"
Never invent facts that are not in the scraped text.

BATCH:
{items}"""
        data = self.llm.generate_json(prompt, validator=_valid_verdicts)
        if data is None: return None
        if isinstance(data, dict): data = data.get("leads") if isinstance(data.get("leads"), list) else [data]
        return data

def map_verdicts(verdicts, n):
    out = {}
    for pos, v in enumerate(verdicts or []):
        if not isinstance(v, dict): continue
        try: idx = int(v.get("item_index"))
        except (TypeError, ValueError): idx = pos if len(verdicts) == n else None
        if idx is None or not 0 <= idx < n or idx in out: continue
        out[idx] = v
    return out

def safe_cell(v):
    v = "" if v is None else str(v)
    return " " + v if v[:1] in ("=", "+", "-", "@") else v

class Router:
    def __init__(self, sheet):
        self.sheet = sheet

    def push(self, doc, ai, default_target, default_industry):
        company = normalize_company(str(ai.get("organization") or ""))
        is_valid = ai.get("is_valid") in (True, "true", "True", "yes")
        confidence = str(ai.get("confidence") or "LOW").upper()
        role = str(ai.get("entity_role") or "IRRELEVANT").upper()
        if role == "IRRELEVANT": is_valid = False
        buyer_roles = ("BUYER", "PROJECT_BUYER", "SERVICE_USER")
        doubtful = (not is_valid) and role in buyer_roles and confidence == "MEDIUM"
        if doubtful: ai = dict(ai, why_engage_now="(AI doubted validity) " + str(ai.get("why_engage_now") or ""))

        if company.lower() in ("unknown", "unknown firm", ""):
            if (is_valid or doubtful) and role != "SELLER":
                host = re.sub(r"[^a-z0-9]", "", urlparse(doc["url"]).netloc.lower().replace("www.", ""))[:18]
                company = f"Unidentified {hashlib.md5(doc['url'].encode()).hexdigest()[:4]} {host}"
                confidence = "LOW"
            else: return "SKIPPED", company
        if doc.get("snippet_only") and is_valid:
            confidence = "LOW"
            ai = dict(ai, why_engage_now="(page unreadable - judged from search snippet) " + str(ai.get("why_engage_now") or ""))

        capture = datetime.now().strftime("%Y-%m-%d %H:%M")
        score = str(ai.get("lead_score") or "50")
        why = f"🔥 [SCORE: {score}/100] " + str(ai.get("why_engage_now") or "")
        solution = str(ai.get("target_solution") or default_target)
        phone = str(ai.get("phone") or "")
        if len(extract_phone(phone)) < 10: phone = extract_phone(doc.get("raw_text", ""))
        else: phone = extract_phone(phone)
        dm = str(ai.get("dm_name") or "")
        industry = default_industry if default_industry and default_industry != "ALL_SECTORS" else str(ai.get("industry") or "Commercial Enterprise")
        lead_id = f"{str(uuid.uuid4())[:8].upper()}::{hashlib.md5(doc['url'].encode()).hexdigest()[:10]}"

        if doubtful: is_valid, confidence = True, "LOW"
        
        if role == "SELLER":
            sheet_name, outcome = SHEET_PARTNERS, "PARTNER"
            row = [lead_id, capture, company, str(ai.get("city") or ""), str(ai.get("state") or ""), solution, doc["url"], "", "", "", ""]
        elif not is_valid:
            sheet_name, outcome = SHEET_TRASH, "TRASH"
            row = [capture, company, doc["url"], why, ""]
        else:
            sheet_name = SHEET_REVIEW if confidence == "LOW" else SHEET_INBOX
            outcome = "REVIEW" if confidence == "LOW" else "INBOX"
            row = [capture, str(ai.get("upcoming_events") or "Unknown"), role, industry, str(ai.get("state") or "N/A"), str(ai.get("city") or "N/A"), company, solution, why, doc["url"], str(ai.get("product_usage") or "Unknown"), "", lead_id]

        # --- NEW: COMPETITOR INTEL TRIGGER ---
        comp_winner = str(ai.get("competitor_winner") or "").strip()
        if comp_winner and comp_winner.lower() not in ("none", "", "null", "n/a", "unknown"):
            try:
                self.sheet.post("insert_competitor_intel", project_name=safe_cell(ai.get("project_name") or "Unknown Project"),
                                client_name=safe_cell(company), competitor_name=safe_cell(comp_winner),
                                winning_value=str(ai.get("winning_value") or "0"), our_price="N/A", outcome="Lost",
                                remarks=f"AI detected award via {doc['url']}")
            except SheetError as e:
                log.warning(f"competitor intel not logged: {e}")

        log.info(f"[*] {company} [{role}/{confidence}] -> {sheet_name} | {doc['url']} | Reason: {why}")
        try:
            res = self.sheet.post("insert_lead", target_sheet=sheet_name, company_name=safe_cell(company), signal_brief=safe_cell(why), row_data=[safe_cell(x) for x in row], contact_phone=safe_cell(phone), dm_name=safe_cell(dm), source_url=doc["url"])
        except SheetError as e:
            log.error(f"insert_lead failed for {company}: {e}")
            return "FAILED", company
        if res.get("status") == "duplicate" or res.get("duplicate") or "appended note" in str(res.get("message", "")).lower():
            log.info(f"[-] Already in CRM: {company} | {doc['url']} | Reason: {why}")
            return "DUPLICATE", company
        return outcome, company

def main():
    started = time.time()
    t0 = datetime.now()
    tg = Telegram()
    log.info("=== Radar Scout harvester starting ===")

    try:
        sheet = SheetAPI()
        cfg = load_settings(sheet)
    except SheetError as e:
        log.error(f"Cannot read settings: {e}")
        tg.send(f"🚨 <b>Radar Scout halted</b>\nCannot read Google Sheet settings:\n<code>{tg_escape(e)}</code>")
        return 1

    special = cfg["special_prompt"]
    if not cfg["targets"] and not special:
        msg = "No Target Products (and no Special Prompt) found in the Settings sheet."
        log.error(msg)
        tg.send(f"⚠️ <b>Radar Scout</b>: {tg_escape(msg)}")
        return 1

    llm = LLMRouter()
    if not llm.available():
        tg.send("🚨 <b>Radar Scout halted</b>: no LLM API key configured.")
        return 1
    search = SearchEngine()
    evaluator = Evaluator(llm)
    router = Router(sheet)
    seen = load_cache(sheet)
    fps = load_fingerprints()

    country = cfg["countries"][0] if cfg["countries"] else ""
    states = cfg["states"]
    geo_rule = (f"GEOGRAPHY RULE: must have verifiable commercial operations or active requirements in {country}." if country else "")
    ban_rule = f"Ignore these domains: {', '.join(cfg['banned_sites'])}" if cfg["banned_sites"] else ""

    max_runtime = env_int("MAX_RUNTIME_MIN", 40) * 60
    max_links = env_int("MAX_LINKS_PER_RUN", 80)
    max_combos = env_int("MAX_COMBOS_PER_RUN", 12)
    pages = env_int("SEARCH_PAGES", 2)
    use_ai_queries = os.getenv("AI_QUERIES", "1").strip() != "0"

    campaigns = []
    if special:
        log.info(f"SPECIAL DIRECTIVE ACTIVE: {special}")
        gen = QueryGenerator(special, "Special Campaign", country, states, llm, use_ai_queries)
        campaigns.append((special, "Special Campaign", gen))
    else:
        industries = cfg["industries"] or ["ALL_SECTORS"]
        combos = [(t, i) for t in cfg["targets"] for i in industries]
        if len(combos) > max_combos:
            offset = (datetime.now().toordinal() * max_combos) % len(combos)
            combos = (combos[offset:] + combos[:offset])[:max_combos]
            log.info(f"Running {max_combos} of the product/industry combinations this time (rotating daily).")
        for t, i in combos:
            campaigns.append((t, i, QueryGenerator(t, i, country, states, llm, use_ai_queries)))

    stats = {"snippet_only": 0, "scanned": 0, "inbox": 0, "review": 0, "partners": 0, "trash": 0, "dupes": 0, "failed": 0}
    session_signals = set()
    pending = set()
    pending_fp = set()
    ai_failures = 0
    stop_reason = ""

    def check_budget():
        if time.time() - started > max_runtime: raise StopRun(f"time budget of {max_runtime // 60} min reached")
        if stats["scanned"] >= max_links: raise StopRun(f"link budget of {max_links} reached")

    # --- GATED PORTAL SETUP (parsed once) ---
    cookie_str = cfg.get("session_cookies", "").strip()
    cookie_domains = set()
    if cookie_str:
        try:
            for ck in json.loads(cookie_str):
                dom = str(ck.get("domain", "")).lstrip(".").lower()
                if dom: cookie_domains.add(dom)
        except Exception as e:
            log.warning(f"Could not parse cookie domains: {e}")
    portal_hosts = {"eprocure", "gem.gov", "etenders", "railone"} | {p for p in cfg.get("protected", []) if p}
    max_playwright = env_int("MAX_PLAYWRIGHT_PER_RUN", 10)
    playwright_used = 0
    # ----------------------------------------
    # --- PRE-AI BOUNCER ---
    core_kw = set()
    for group in (cfg["targets"], cfg["directives"], cfg["industries"], [special]):
        for item in group:
            for word in str(item).lower().replace("-", " ").split():
                if len(word) >= 3: 
                    core_kw.add(word)
    log.info(f"Pre-AI Bouncer Keywords: {core_kw}")
    # ----------------------

    try:
        for target, industry, gen in campaigns:
            check_budget()
            tracks = gen.build_special_tracks(special) if special else gen.build_tracks()
            for track_name, queries in tracks.items():
                docs = []
                for query in queries:
                    check_budget()
                    recent = any(w in query.lower() for w in ("tender", "rfp", "bid", "procurement", "loa", "award", "contract", "order", "empanelment"))
                    log.info(f"Searching: {query}")
                    results = search.search(query, country, pages=pages, recent=recent)
                    fresh = 0
                    for res in results:
                        raw = (res.get("link") or "").strip()
                        c = clean_url(raw)
                        if not raw or not c or c in seen or c in pending: continue
                        
                        low_url = raw.lower()
                        protected = any(p in low_url for p in cfg["protected"] if p)
                        
                        # --- STAGE 1: DYNAMIC DOMAIN BAN CHECK ---
                        if not protected and any(bad_domain in low_url for bad_domain in cfg["banned_sites"] if bad_domain):
                            log.info(f"   [-] Auto-Drop (Banned Domain in URL): {raw}")
                            mark_seen(seen, raw)
                            stats["trash"] += 1
                            continue

                        pending.add(c)
                        check_budget()
                        stats["scanned"] += 1
                        fresh += 1
                        
                        snippet = (res.get("snippet") or "").lower()
                        if not protected and any(k in snippet for k in cfg["banned_kw"] if k):
                            log.info(f"   [-] Auto-Drop (Banned Keyword in Snippet): {raw}")
                            mark_seen(seen, raw)
                            stats["trash"] += 1
                            continue
                            
                        content = None
                        
                        # Dynamically check via AI if this URL requires Playwright/session cookies
                        needs_playwright = False
                        if cookie_str and playwright_used < max_playwright:
                            needs_playwright = ai_is_gated_portal(llm, raw)

                        if needs_playwright:
                            playwright_used += 1
                            log.info(f"[*] AI identified gated portal ({playwright_used}/{max_playwright}). Launching Playwright on: {raw}")
                            try:
                                from portal_crawler import crawl_all_pages
                                rendered_pages = crawl_all_pages(raw, cookie_str, max_pages=3)
                                if rendered_pages:
                                    content = "\n\n--- NEXT PAGE ---\n\n".join(rendered_pages)
                            except Exception as e:
                                log.warning(f"Playwright crawl failed ({e}); falling back to plain fetch")

                        if not content:
                            content = fetch_page(raw, protected_domains=cfg["protected"])
                        
                        if content and len(content) > 120:
                            low_content = content.lower()
                            
                            # --- STAGE 2: DYNAMIC NEGATIVE WORD CHECK ---
                            bad_match = None
                            for bad in cfg["banned_kw"]:
                                if not bad: continue
                                pattern = r'\b' + re.escape(bad) + r'\b'
                                if re.search(pattern, low_content):
                                    bad_match = bad
                                    break
                            
                            if bad_match and not protected:
                                log.info(f"    [-] Pre-AI Drop (Negative word '{bad_match}'): {raw}")
                                mark_seen(seen, raw)
                                stats["trash"] += 1
                                continue
                                
                            # --- STAGE 3: POSITIVE WORD CHECK ---
                            if not any(kw in low_content for kw in core_kw) and not protected:
                                log.info(f"   [-] Pre-AI Drop (No target keywords found): {raw}")
                                mark_seen(seen, raw)
                                stats["trash"] += 1
                                continue

                            fp = fingerprint(content)
                            if fp and (fp in fps or fp in pending_fp):
                                log.info(f"   same content already processed, skipping: {raw}")
                                mark_seen(seen, raw)
                                stats["dupes"] += 1
                                continue
                            if fp: pending_fp.add(fp)
                            docs.append({"track": track_name, "url": raw, "raw_text": content, "fp": fp})
                        else:
                            snip = f"{res.get('title', '')}\n{res.get('snippet', '')}".strip()
                            if len(snip) >= 60:
                                docs.append({"track": track_name, "url": raw, "fp": "", "snippet_only": True, "raw_text": "[PAGE COULD NOT BE READ - SEARCH SNIPPET ONLY]\n" + snip})
                                stats["snippet_only"] += 1
                            else: mark_seen(seen, raw)
                    log.info(f"   {len(results)} results, {fresh} new")

                for i in range(0, len(docs), 4):
                    batch = docs[i:i + 4]
                    check_budget()
                    
                    all_targets = " OR ".join(cfg["targets"]) if cfg["targets"] else "Any Commercial Software"
                    all_directives = " OR ".join(cfg["directives"]) if cfg["directives"] else ""
                    all_industries = " OR ".join(cfg["industries"]) if cfg["industries"] else ""
                    
                    verdicts = evaluator.evaluate(batch, all_targets, all_directives, all_industries, country, states, geo_rule, ban_rule, special)
                    
                    if verdicts is None:
                        ai_failures += 1
                        log.warning("AI evaluation failed for a batch - links left un-cached so they are retried next run.")
                        if ai_failures >= 2: raise StopRun("all AI providers are exhausted / unreachable")
                        continue
                    ai_failures = 0
                    vmap = map_verdicts(verdicts, len(batch))
                    for idx, doc in enumerate(batch):
                        v = vmap.get(idx)
                        if v is None:
                            mark_seen(seen, doc["url"])
                            mark_fp(fps, doc.get("fp"))
                            continue
                        comp = normalize_company(str(v.get("organization") or ""))
                        sol_key = re.sub(r"\s+", " ", str(v.get("target_solution") or target).strip().lower())
                        sig_key = (comp.lower(), sol_key)
                        if sig_key in session_signals:
                            mark_seen(seen, doc["url"])
                            mark_fp(fps, doc.get("fp"))
                            continue                        
                        outcome, company = router.push(doc, v, target, industry)
                        if outcome == "FAILED":
                            stats["failed"] += 1
                            continue
                        mark_seen(seen, doc["url"])
                        mark_fp(fps, doc.get("fp"))
                        if outcome in ("INBOX", "REVIEW", "PARTNER", "DUPLICATE"):
                            session_signals.add((company.lower(), sol_key))
                        key = {"INBOX": "inbox", "REVIEW": "review", "PARTNER": "partners", "TRASH": "trash", "DUPLICATE": "dupes"}.get(outcome)
                        if key: stats[key] += 1
    except StopRun as e:
        stop_reason = str(e)
        log.warning(f"Stopping early: {e}")
    except Exception as e:
        stop_reason = f"unexpected error: {e}"
        log.exception("Unexpected error")

    duration = str(datetime.now() - t0).split(".")[0]
    pushed = stats["inbox"] + stats["review"]
    log.info(f"Done: {stats} in {duration}")
    lines = ["🏁 <b>Radar Scout complete</b>",
             f"• Links scanned: <code>{stats['scanned']}</code>",
             f"• New leads → CRM: <code>{pushed}</code> (Inbox {stats['inbox']}, Review {stats['review']})",
             f"• Partners/suppliers: <code>{stats['partners']}</code> • Discarded: <code>{stats['trash']}</code>"
             f" • Duplicates: <code>{stats['dupes']}</code>",
             f"• Duration: <code>{duration}</code>"]
    if stats["snippet_only"]: lines.append(f"• 🔎 Judged from snippet only: <code>{stats['snippet_only']}</code>")
    if stats["failed"]: lines.append(f"• ⚠️ Sheet write failures: <code>{stats['failed']}</code>")
    if stop_reason: lines.append(f"• ⛔ Stopped early: {tg_escape(stop_reason)}")
    tg.send("\n".join(lines))
    return 0

if __name__ == "__main__":
    sys.exit(main())
