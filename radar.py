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

try:
    from ddgs import DDGS
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
            for preferred in ["models/gemini-3.5-flash-lite", "models/gemini-1.5-flash"]:
                if preferred in valid_models:
                    valid_models.insert(0, valid_models.pop(valid_models.index(preferred)))
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-3.5-flash-lite", "gemini-1.5-flash"]
    return BEST_MODEL_STACK

def load_existing_urls_cache():
    global EXISTING_URLS_CACHE
    if not WEBHOOK or not SECRET: return
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_all_urls"}, timeout=30).json()
        raw_urls = res.get("urls", [])
        EXISTING_URLS_CACHE = {u.strip().lower() for u in raw_urls if u.strip()}
        print(f"[*] Loaded {len(EXISTING_URLS_CACHE)} existing records into cache.", flush=True)
    except Exception as e:
        print(f"⚠️ Cache load failed: {e}. Proceeding with clean cache.", flush=True)

def is_duplicate_cached(link):
    if not link: return False
    clean = link.strip().lower()
    if clean in EXISTING_URLS_CACHE: return True
    parsed = urlparse(clean)
    netloc = parsed.netloc.replace("www.", "")
    directory_domains = ["indiamart.com", "tradeindia.com", "linkedin.com", "gem.gov.in", "eprocure.gov.in", "bseindia.com"]
    if not any(d in netloc for d in directory_domains):
        if any(netloc in cached for cached in EXISTING_URLS_CACHE if cached):
            return True
    return False

def get_search_results(query):
    results = []
    if SERPER_KEY:
        try:
            payload = json.dumps({"q": query, "gl": "in", "tbs": "qdr:y", "num": 10})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            response = requests.post("https://google.serper.dev/search", headers=headers, data=payload, timeout=25)
            if response.status_code == 200:
                for r in response.json().get("organic", []):
                    results.append({"title": r.get("title", ""), "link": r.get("link", ""), "summary": r.get("snippet", "")})
        except Exception: pass

    if DDGS and not results:
        try:
            def ddgs_search(): return list(DDGS().text(query, timelimit="y", max_results=10, backend="lite"))
            with concurrent.futures.ThreadPoolExecutor() as executor:
                res = executor.submit(ddgs_search).result(timeout=15)
            for r in res:
                results.append({"title": r.get("title", ""), "link": r.get("href", ""), "summary": r.get("body", "")})
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

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_analyze_batch(batch, exclusions):
    client = get_next_gemini_client()
    if not client: return []
    model_stack = get_flash_model_stack(client)
    
    items_block = ""
    for i, x in enumerate(batch):
        items_block += f"\n--- ITEM {i} ---\nTarget Product: {x.get('target', 'Unknown')}\nQuery Type: {x.get('query_type', 'Direct')}\nTitle: {x['title']}\nLink: {x['link']}\nData: {(x.get('deep_text') or x.get('summary') or '')[:2000]}\n"
        
    exclusion_rule = ""
    if exclusions:
        exclusion_rule = f"""
CRITICAL CONTEXTUAL EXCLUSIONS:
Banned Intents/Keywords: {json.dumps(exclusions)}
- If the primary intent of the organization/lead is to procure or offer these EXACT [Banned Keywords], REJECT THEM (is_valid=False).
- If the banned keyword is incidental context (e.g. 'Repair Dept buying AutoCAD'), DO NOT REJECT.
"""

    prompt = f"""
You are an expert B2B Ecosystem Analyst.
You evaluate news, tenders, contracts, and company profiles related to the 'Target Product'.

CLASSIFICATION ROLES:
1. 'BUYER': Organization directly procuring or issuing an RFQ/tender for the Target Product.
2. 'PROJECT_BUYER': Organization winning a project/MOU that REQUIRES the Target Product.
3. 'SERVICE_USER': Company offering commercial services using the Target Product (e.g., AutoCAD drafting services).
4. 'SELLER': Company manufacturing/supplying the Target Product or an alternative.
5. 'IRRELEVANT': Unrelated products, job listings, generic articles.

GEOGRAPHIC NORMALIZATION:
- 'city': Specific Indian city (e.g., 'Bengaluru', 'Pune'). 
- 'state': Standard Indian State/UT (e.g., 'Karnataka', 'Maharashtra').

{exclusion_rule}

DATA BATCH:
{items_block}
"""
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "item_index": {"type": "INTEGER"},
                "product_match_reasoning": {"type": "STRING", "description": "Explain interaction with Target Product."},
                "is_valid": {"type": "BOOLEAN"},
                "entity_role": {"type": "STRING", "description": "Must be exactly one of: BUYER, PROJECT_BUYER, SERVICE_USER, SELLER, IRRELEVANT"},
                "org": {"type": "STRING", "description": "Entity name"},
                "city": {"type": "STRING", "description": "Normalized Indian City"},
                "state": {"type": "STRING", "description": "Normalized Indian State/UT"},
                "industry": {"type": "STRING"},
                "intent_summary": {"type": "STRING", "description": "Summary of opportunity"},
                "dm_name": {"type": "STRING", "nullable": True},
                "dm_title": {"type": "STRING", "nullable": True}
            },
            "required": ["item_index", "product_match_reasoning", "is_valid", "entity_role", "org", "city", "state", "industry", "intent_summary"]
        }
    }

    for model_name in model_stack:
        try:
            chat = client.chats.create(model=model_name)
            res = chat.send_message(
                prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0)
            )
            raw_text = res.text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            return json.loads(raw_text)
        except Exception as e:
            err_str = str(e)
            if any(err in err_str for err in ["NOT_FOUND", "404", "503", "500", "limit: 0", "limit: 20"]):
                print(f"    ⚠️ Model {model_name} unavailable. Cascading...", flush=True)
                continue
            raise e
    raise Exception("All Gemini models unavailable.")

def build_vector_matrix(target):
    current_year = datetime.now().year
    return [
        {"type": "Direct", "query": f'"{target}" tender OR RFQ site:gov.in'},
        {"type": "Direct", "query": f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com'},
        {"type": "Project", "query": f'"{target}" ("Letter of Award" OR "awarded contract" OR "lowest bidder") India {current_year}'},
        {"type": "Project", "query": f'"{target}" ("MoU signed" OR "groundbreaking ceremony" OR "new plant") India'},
        {"type": "Project", "query": f'site:bseindia.com/xml-data/corpfiling/ "{target}" ("bagged order" OR "contract worth" OR "LoA")'},
        {"type": "Direct", "query": f'"{target}" "looking for vendors" site:linkedin.com/posts'}
    ]

def run():
    print(">>> 📡 RADAR SCOUT ACTIVE (V7 Master Engine)", flush=True)
    if not WEBHOOK or not SECRET: return
    
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
    for t in cloud_targets:
        for v in build_vector_matrix(t):
            search_matrix.append({"target": t, "query": v["query"], "query_type": v["type"]})

    for item in search_matrix:
        target_product = item["target"]
        query = item["query"]
        query_type = item["query_type"]
        
        print(f"\n[*] Scanning: {query} (Target: {target_product})", flush=True)
        results = get_search_results(query)
        fresh_leads = []
        
        for r in results:
            link_lower = r['link'].lower()
            if any(b_dom in link_lower for b_dom in cloud_domains):
                continue
            if not is_duplicate_cached(r['link']):
                r['deep_text'] = fetch_deep_text(r['link'])
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
            reason = entity.get("product_match_reasoning", "No reasoning provided")
            print(f"       [Vote] Valid: {entity.get('is_valid')} | Role: {role} | Org: {entity.get('org')}", flush=True)
            
            if entity.get("is_valid") and role in ["BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER"]:
                is_supplier = (role == "SELLER")
                
                if role == "PROJECT_BUYER": target_sheet, source_tag = "Projects & MOUs", "Project-Radar"
                elif role == "SERVICE_USER": target_sheet, source_tag = "Inbox", "Ecosystem-Scout"
                elif is_supplier: target_sheet, source_tag = "Suppliers", "Supplier-Radar"
                else: target_sheet, source_tag = "Inbox", "Radar Scout"
                
                intent_label = f"Supplier ({entity.get('intent_summary')})" if is_supplier else entity.get("intent_summary")
                
                payload = {
                    "secret": SECRET,
                    "action": "add_lead",
                    "target_sheet": target_sheet,
                    "is_supplier": is_supplier,
                    "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": source_tag,
                    "org": entity.get("org", "Unknown"),
                    "city": entity.get("city", "Unknown"),
                    "state": entity.get("state", "Pan-India"),
                    "industry": target_product,
                    "intent": intent_label,
                    "dm_name": entity.get("dm_name") or "N/A",
                    "dm_title": entity.get("dm_title") or "N/A",
                    "link": fresh_leads[idx]['link'],
                    "email": "N/A",
                    "phone": "N/A",
                    "website": "N/A"
                }
                for attempt in range(3):
                    try:
                        requests.post(WEBHOOK, json=payload, timeout=30)
                        EXISTING_URLS_CACHE.add(fresh_leads[idx]['link'].strip().lower())
                        print(f"    ✅ [{role}] -> {target_sheet}: {entity['org']}", flush=True)
                        break
                    except Exception: time.sleep(2)
            else:
                ai_trash_log.append({"url": fresh_leads[idx]['link'], "reason": f"[{role}] {reason}"})
                EXISTING_URLS_CACHE.add(fresh_leads[idx]['link'].strip().lower())
        
        if ai_trash_log:
            payload = {
                "secret": SECRET,
                "action": "log_trash_batch",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "trash_data": ai_trash_log
            }
            for attempt in range(3):
                try:
                    requests.post(WEBHOOK, json=payload, timeout=30)
                    print(f"    🗑️ Swept {len(ai_trash_log)} rejected links into AI_Trash.", flush=True)
                    break
                except Exception: time.sleep(2)
        time.sleep(4)

if __name__ == "__main__":
    run()
