import os
import re
import ssl
import json
import time
import uuid
import logging
import warnings
import requests
import concurrent.futures
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from datetime import datetime
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

warnings.filterwarnings("ignore")
logging.getLogger("google.genai.models").setLevel(logging.ERROR)

TODAY = datetime.now()
CURRENT_DATE_STR = TODAY.strftime("%d %B %Y")

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
SECRET = os.environ.get("WEBHOOK_SECRET")
SERPER_KEY = os.environ.get("SERPER_API_KEY")

raw_keys = os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
current_key_index = 0

EXISTING_URLS_CACHE = set()

def get_next_gemini_client():
    global current_key_index
    if not GEMINI_KEYS: return None
    key = GEMINI_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(GEMINI_KEYS)
    return genai.Client(api_key=key)

BEST_MODEL_STACK = []
def get_flash_model_stack(client):
    global BEST_MODEL_STACK
    if BEST_MODEL_STACK: return BEST_MODEL_STACK
    try:
        valid_models = []
        for m in client.models.list():
            name = m.name.lower()
            banned_keywords = ["audio", "tts", "image", "omni", "vision", "native", "preview", "thinking", "2.5"]
            if "flash" in name and not any(bad in name for bad in banned_keywords):
                valid_models.append(name)
        if valid_models:
            valid_models.sort(reverse=True)
            for preferred in ["models/gemini-3.8-flash", "models/gemini-3.5-flash", "models/gemini-1.5-flash"]:
                if preferred in valid_models:
                    valid_models.insert(0, valid_models.pop(valid_models.index(preferred)))
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
    return BEST_MODEL_STACK

def get_buyer_industries(target, client):
    try:
        prompt = (
            f"What are 3 primary commercial or industrial sectors in India that purchase or deploy '{target}'? "
            f"Respond strictly with 3 space-separated or OR-separated single keywords."
        )
        chat = client.chats.create(model="gemini-1.5-flash")
        res = chat.send_message(prompt)
        cleaned = re.sub(r'[^a-zA-Z\s]', '', res.text).strip().split()
        if cleaned: return " OR ".join(cleaned[:3])
    except Exception: pass
    return f'"{target}"'

def add_to_cache(link):
    if not link: return
    clean_link = link.strip().lower()
    if clean_link not in EXISTING_URLS_CACHE:
        EXISTING_URLS_CACHE.add(clean_link)
        try:
            with open("seen_links.txt", "a", encoding="utf-8") as f:
                f.write(clean_link + "\n")
        except Exception: pass

def load_existing_urls_cache():
    global EXISTING_URLS_CACHE
    if os.path.exists("seen_links.txt"):
        try:
            with open("seen_links.txt", "r", encoding="utf-8") as f:
                for line in f:
                    val = line.strip().lower()
                    if val: EXISTING_URLS_CACHE.add(val)
            print(f"[*] Loaded {len(EXISTING_URLS_CACHE)} records from seen_links.txt backup.", flush=True)
        except Exception as e: print(f"⚠️ Could not read seen_links.txt: {e}")

    if not WEBHOOK or not SECRET: return
        
    max_retries = 5
    webhook_success = False
    for attempt in range(max_retries):
        try:
            res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_all_urls"}, timeout=30)
            res.raise_for_status() 
            data = res.json()
            raw_urls = data.get("urls", [])
            for u in raw_urls:
                if u.strip(): EXISTING_URLS_CACHE.add(u.strip().lower())
            print(f"[*] Synced cache with Google Sheets. Total cache size: {len(EXISTING_URLS_CACHE)}", flush=True)
            webhook_success = True
            break
        except Exception as e:
            if attempt < max_retries - 1: time.sleep(10)

def is_duplicate_cached(link):
    if not link: return False
    return link.strip().lower() in EXISTING_URLS_CACHE

def get_search_results(query):
    results = []
    if SERPER_KEY:
        try:
            payload = json.dumps({"q": query, "gl": "in", "tbs": "qdr:m", "num": 10})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            response = requests.post("https://google.serper.dev/search", headers=headers, data=payload, timeout=25)
            if response.status_code == 200:
                for r in response.json().get("organic", []):
                    results.append({"title": r.get("title", ""), "link": r.get("link", ""), "summary": r.get("snippet", ""), "date": r.get("date", "")})
        except Exception: pass

    if DDGS and not results:
        try:
            def ddgs_search(): return list(DDGS().text(query, timelimit="m", max_results=10, backend="lite"))
            with concurrent.futures.ThreadPoolExecutor() as executor:
                res = executor.submit(ddgs_search).result(timeout=15)
            for r in res:
                results.append({"title": r.get("title", ""), "link": r.get("href", ""), "summary": r.get("body", ""), "date": ""})
        except Exception: pass
    return results

def fetch_deep_text(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        session = requests.Session()
        r = session.get(url, headers=headers, timeout=(5, 10), verify=False)
        if r.status_code == 200:
            if 'text/html' not in r.headers.get('Content-Type', '').lower(): return ""
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]): tag.decompose()
            return soup.get_text(separator=" ", strip=True)[:4000]
    except Exception: pass
    return ""

def is_tender_active(raw_text):
    if not raw_text: return True
    
    pattern = (
        r"(?:Bid\s+End(?:\s+Date)?(?:/\s*Time)?|"
        r"Submission\s+(?:End\s+Date|Deadline|Closing\s+Date)|"
        r"Closing\s+Date|Due\s+Date|Last\s+Date(?:\s+of\s+Submission)?)"
        r"\s*[:\-]?\s*"
        r"(\d{1,2}[-/\.\s](?:[A-Za-z]{3,9}|\d{1,2})[-/\.\s]\d{4})"
    )
    
    matches = re.findall(pattern, raw_text, re.IGNORECASE)
    found_dates = []
    
    for date_str in matches:
        clean_str = re.sub(r"[/\\.\s]+", "-", date_str.strip())
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y"):
            try:
                found_dates.append(datetime.strptime(clean_str, fmt))
                break
            except ValueError: continue

    if not found_dates: return True 
    for tender_date in found_dates:
        if tender_date.date() >= TODAY.date(): return True

    return False

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_analyze_batch(batch, exclusions):
    client = get_next_gemini_client()
    if not client: return []
    model_stack = get_flash_model_stack(client)
    
    items_block = ""
    for i, x in enumerate(batch):
        date_str = f"Date Posted: {x['date']}\n" if x.get("date") else ""
        items_block += f"\n--- ITEM {i} ---\nTarget Product: {x.get('target', 'Unknown')}\nQuery Type: {x.get('query_type', 'Direct')}\n{date_str}Title: {x['title']}\nLink: {x['link']}\nData: {(x.get('deep_text') or x.get('summary') or '')[:2000]}\n"
        
    exclusion_rule = ""
    if exclusions:
        exclusion_rule = f"""CRITICAL CONTEXTUAL EXCLUSIONS:
Banned Intents/Keywords: {json.dumps(exclusions)}
- If the primary intent of the organization is to procure these EXACT [Banned Keywords], REJECT THEM."""

    prompt = f"""
You are an expert B2B Ecosystem Analyst. Today's date is {CURRENT_DATE_STR}.

CLASSIFICATION ROLES:
1. 'BUYER': Direct procurement, GeM bids, CPPP eTenders, public tenders, Zauba import data, IndiaMART/TradeIndia buyers.
2. 'PROJECT_BUYER': Capex, Factory Setups, Land Allotments, expansions, newly incorporated companies, OR HIRING EXPANSION SIGNALS (e.g., Company hiring operators/engineers indicating immediate operational demand for the Target Product).
3. 'SERVICE_USER': Company offering commercial services using the Target Product.
4. 'SELLER': Company manufacturing/supplying the Target Product.
5. 'IRRELEVANT': Unrelated products, individual candidates looking for jobs/resumes, generic news.

BUYER SEGMENT CLASSIFICATION:
- 'GOVT': Central/State Government, GeM, CPPP, State eProcurement, PSUs.
- 'CORPORATE': Private/Public Limited enterprises, listed companies, funded startups, Hiring signals on Naukri/LinkedIn.
- 'LOCAL_MSME': Small businesses, contractors, dealers, trade marketplace RFQs.

GEOGRAPHIC NORMALIZATION:
- 'city': Specific Indian city (e.g., 'Bengaluru', 'Pune'). 
- 'state': Standard Indian State/UT (e.g., 'Karnataka', 'Maharashtra').

{exclusion_rule}

DEADLINE ENFORCEMENT RULE (Layer 4 Guardrail):
1. FOR TENDERS / BIDS / RFQs ONLY: Scan for "Bid Submission End Date", "Deadline", or "Valid Upto".
   If that deadline has passed relative to {CURRENT_DATE_STR}, return is_valid=False.
2. FOR PRIVATE CAPEX / NEWS / MCA / HIRING SIGNALS: DO NOT reject based on past publication or job posting dates. Active hiring indicates expanding requirements.

DATA BATCH:
{items_block}
"""
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "item_index": {"type": "INTEGER"},
                "product_match_reasoning": {"type": "STRING"},
                "is_valid": {"type": "BOOLEAN"},
                "entity_role": {"type": "STRING", "description": "Must be exactly one of: BUYER, PROJECT_BUYER, SERVICE_USER, SELLER, IRRELEVANT"},
                "buyer_segment": {"type": "STRING", "description": "Must be exactly one of: GOVT, CORPORATE, LOCAL_MSME"},
                "org": {"type": "STRING"},
                "city": {"type": "STRING"},
                "state": {"type": "STRING"},
                "industry": {"type": "STRING"},
                "intent_summary": {"type": "STRING"},
                "posted_date": {"type": "STRING", "description": "Output 'N/A' if unknown."},
                "dm_name": {"type": "STRING", "nullable": True},
                "dm_title": {"type": "STRING", "nullable": True}
            },
            "required": ["item_index", "product_match_reasoning", "is_valid", "entity_role", "buyer_segment", "org", "city", "state", "industry", "intent_summary", "posted_date"]
        }
    }

    for model_name in model_stack:
        try:
            chat = client.chats.create(model=model_name)
            res = chat.send_message(prompt, config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0))
            raw_text = res.text.strip()
            if raw_text.startswith("```"): raw_text = raw_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            return json.loads(raw_text)
        except Exception as e:
            if any(err in str(e) for err in ["NOT_FOUND", "404", "503", "500", "limit: 0", "limit: 20"]): continue
            raise e
    raise Exception("All Gemini models unavailable.")

def build_vector_matrix(target, industry_keywords):
    current_year = datetime.now().year
    exclusions = ' -"Award of Contract" -"AOC" -"Status: Closed" -"Cancelled" -"Corrigendum"'
    
    base_queries = [
        {"type": "Direct", "query": f'"{target}" tender OR RFQ site:gov.in{exclusions}'},
        {"type": "Direct", "query": f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com'},
        {"type": "MCA", "query": f'site:zaubacorp.com "Date of Incorporation" "{current_year}" ({industry_keywords})'},
        {"type": "MCA", "query": f'site:thecompanycheck.com "Incorporation Date" "{current_year}" ({industry_keywords})'},
        {"type": "Project", "query": f'"{target}" ("Letter of Award" OR "awarded contract" OR "lowest bidder") India {current_year}'},
        {"type": "Project", "query": f'"{target}" ("MoU signed" OR "groundbreaking ceremony" OR "new plant") India'},
        {"type": "Project", "query": f'site:bseindia.com/xml-data/corpfiling/ "{target}" ("bagged order" OR "contract worth" OR "LoA")'},
        {"type": "Direct", "query": f'"{target}" ("looking for vendors" OR "vendor needed" OR "need supplier" OR "urgently required") site:linkedin.com'},
        {"type": "Direct", "query": f'site:facebook.com/groups "{target}" ("urgent requirement" OR "need supplier" OR "vendor needed") India'},
        {"type": "Project", "query": f'"{target}" ("hiring" OR "urgent opening" OR "walk-in") (site:naukri.com/job-listings OR site:linkedin.com/jobs) India'}
    ]

    new_vectors = [
        {"type": "Project", "query": f'"{target}" (site:parivesh.nic.in OR site:environmentclearance.nic.in){exclusions}'},
        {"type": "Project", "query": f'"{target}" "allotment" (site:midcindia.org OR site:gidc.gujarat.gov.in OR site:onlineupsida.com)'},
        {"type": "Project", "query": f'"{target}" "project cost" (site:maharera.mahaonline.gov.in OR site:up-rera.in OR site:rera.karnataka.gov.in)'},
        {"type": "Project", "query": f'"{target}" "project cost" site:indiainvestmentgrid.gov.in'},
        {"type": "Project", "query": f'"{target}" "IEM acknowledged" site:dpiit.gov.in'},
        {"type": "Project", "query": f'"{target}" "Regulation 30" "capex" (site:bseindia.com OR site:nseindia.com)'},
        {"type": "Project", "query": f'"{target}" "rating rationale" "capex" (site:crisilratings.com OR site:icra.in OR site:careratings.com)'},
        {"type": "Project", "query": f'"{target}" "PLI scheme" "approved" (site:gov.in OR site:pib.gov.in)'},
        {"type": "Project", "query": f'"{target}" "resolution plan approved" (site:ibbi.gov.in OR site:nclt.gov.in)'},
        {"type": "Project", "query": f'"{target}" "Consent to Establish" (site:mpcb.gov.in OR site:gpcb.gujarat.gov.in OR site:uppcb.com)'},
        {"type": "Direct", "query": f'"{target}" site:bidplus.gem.gov.in{exclusions}'},
        {"type": "Direct", "query": f'"{target}" "Tender Documents" (site:eprocure.gov.in OR site:etenders.gov.in){exclusions}'},
        {"type": "Direct", "query": f'"{target}" site:zauba.com/import-'}
    ]

    govt_state_vectors = [
        {"type": "Direct", "query": f'"{target}" tender (site:mahatenders.gov.in OR site:wbtenders.gov.in OR site:etenders.kerala.gov.in OR site:eproc.rajasthan.gov.in OR site:tender.up.gov.in){exclusions}'}
    ]

    local_trade_vectors = [
        {"type": "Direct", "query": f'"{target}" ("buy requirement" OR "urgent requirement" OR "dealer required") site:indiamart.com'},
        {"type": "Direct", "query": f'"{target}" ("need supplier" OR "distributor requirement") India site:tradeindia.com'},
        {"type": "Direct", "query": f'"{target}" ("authorized dealer" OR "stockist") "contact number" site:justdial.com'}
    ]

    event_startup_vectors = [
        {"type": "Project", "query": f'"{target}" ("exhibitor list" OR "participating in" OR "stall booked") India {current_year}'},
        {"type": "Direct", "query": f'"{target}" ("raised" OR "funding" OR "seed" OR "series") (site:yourstory.com OR site:entrackr.com)'},
        {"type": "Direct", "query": f'"{target}" ("buying requirement" OR "urgent order") (site:connect2india.com OR site:exportersindia.com)'},
        {"type": "Direct", "query": f'"{target}" ("need agency" OR "looking for agency" OR "hiring") India (site:upwork.com OR site:freelancer.in)'}
    ]

    return base_queries + new_vectors + govt_state_vectors + local_trade_vectors + event_startup_vectors

def run():
    print(">>> 📡 RADAR SCOUT ACTIVE (Tri-Buyer Multi-Engine: Govt, Corporate, Local MSME)", flush=True)
    load_existing_urls_cache()

    cloud_targets, cloud_exclusions, cloud_domains = [], [], []
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_targets"}, timeout=30)
        cloud_targets = res.json().get("targets", [])
    except Exception: pass

    try:
        res_ex = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_exclusions"}, timeout=30)
        data = res_ex.json()
        cloud_exclusions = [e.strip() for e in data.get("exclusions", []) if e.strip()]
        cloud_domains = [d.strip().lower() for d in data.get("blocked_domains", []) if d.strip()]
    except Exception: pass

    if not cloud_targets: return print("    -> No targets found.", flush=True)

    search_matrix = []
    client = get_next_gemini_client()
    
    for t in cloud_targets:
        industry_keywords = get_buyer_industries(t, client) if client else f'"{t}"'
        print(f"[*] Target: '{t}' mapped to macro industries: [{industry_keywords}]", flush=True)
        for v in build_vector_matrix(t, industry_keywords):
            search_matrix.append({"target": t, "query": v["query"], "query_type": v["type"]})

    for item in search_matrix:
        target_product = item["target"]
        query = item["query"]
        query_type = item["query_type"]
        
        print(f"\n[*] Scanning: {query}", flush=True)
        results = get_search_results(query)
        fresh_leads = []
        
        for r in results:
            link_lower = r['link'].lower()
            if any(b_dom in link_lower for b_dom in cloud_domains): continue
            if not is_duplicate_cached(r['link']):
                raw_deep_text = fetch_deep_text(r['link'])
                is_tender_source = any(k in link_lower for k in ["gem.gov.in", "eprocure.gov.in", "tenders", "bidplus", "mahatenders", "wbtenders", "etenders"]) or "tender" in link_lower
                is_non_tender = any(k in link_lower for k in ["zaubacorp.com", "thecompanycheck.com", "linkedin.com", "indiamart.com", "tradeindia.com", "justdial.com", "naukri.com"]) or query_type == "MCA"

                if is_tender_source and not is_non_tender:
                    if not is_tender_active(raw_deep_text or r['summary']):
                        add_to_cache(r['link'])
                        continue
                
                r['deep_text'] = raw_deep_text
                r['target'] = target_product
                r['query_type'] = query_type
                fresh_leads.append(r)
                
        if not fresh_leads: continue
            
        print(f"    -> AI Analyzing {len(fresh_leads)} links...", flush=True)
        try: ai_data = ai_analyze_batch(fresh_leads, cloud_exclusions)
        except Exception as e: 
            print(f"    -> AI Error: {e}", flush=True)
            continue
            
        ai_trash_log = []
            
        for entity in ai_data:
            idx = entity.get("item_index")
            if idx is None or idx >= len(fresh_leads) or idx < 0: continue
            
            role = entity.get('entity_role')
            segment = entity.get('buyer_segment', 'CORPORATE')
            if entity.get("is_valid") and role in ["BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER"]:
                is_supplier = (role == "SELLER")
                link_url = fresh_leads[idx]['link'].lower()
                is_mca_registry = "zaubacorp.com" in link_url or "thecompanycheck.com" in link_url or query_type == "MCA"
                
                if is_mca_registry: target_sheet, source_tag = "MCA", "MCA-Registry"
                elif role == "BUYER": target_sheet, source_tag = "Inbox", f"{segment}-Buyer"
                elif role == "SERVICE_USER": target_sheet, source_tag = "Services", "Service-Radar"
                elif role == "PROJECT_BUYER": target_sheet, source_tag = "Projects & MOUs", "Project-Radar"
                elif is_supplier: target_sheet, source_tag = "Suppliers", "Supplier-Radar"
                else: target_sheet, source_tag = "Inbox", "Radar Scout"
                
                intent_base = entity.get("intent_summary") or "Identified Requirement"
                intent_label = f"[{segment}] Supplier ({intent_base})" if is_supplier else f"[{segment}] {intent_base}"
                
                payload = {
                    "secret": SECRET, "action": "add_lead", "target_sheet": target_sheet,
                    "is_supplier": is_supplier, "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "posted_date": entity.get("posted_date", "N/A"), "source": source_tag,
                    "org": entity.get("org", "Unknown"), "city": entity.get("city", "Unknown"),
                    "state": entity.get("state", "Pan-India"), "industry": target_product,
                    "intent": intent_label, "dm_name": entity.get("dm_name") or "N/A",
                    "dm_title": entity.get("dm_title") or "N/A", "link": fresh_leads[idx]['link'],
                    "email": "N/A", "phone": "N/A", "website": "N/A"
                }
                for attempt in range(3):
                    try:
                        requests.post(WEBHOOK, json=payload, timeout=30)
                        add_to_cache(fresh_leads[idx]['link'])
                        break
                    except Exception: time.sleep(2)
            else:
                ai_trash_log.append({"url": fresh_leads[idx]['link'], "reason": f"[{role}] {entity.get('product_match_reasoning', 'No reason')}"})
                add_to_cache(fresh_leads[idx]['link'])
        
        if ai_trash_log:
            payload = {"secret": SECRET, "action": "log_trash_batch", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "trash_data": ai_trash_log}
            for attempt in range(3):
                try:
                    requests.post(WEBHOOK, json=payload, timeout=30)
                    break
                except Exception: time.sleep(2)
        time.sleep(4)

if __name__ == "__main__":
    run()
