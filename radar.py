import os
import re
import json
import time
import uuid
import logging
import warnings
import requests
import concurrent.futures
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

def is_duplicate(link):
    if not WEBHOOK or not SECRET: return False
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "check_duplicate", "link": link}, timeout=60).json()
        return res.get("duplicate", False)
    except Exception: return False

def get_search_results(query):
    results = []
    if SERPER_KEY:
        try:
            payload = json.dumps({"q": query, "gl": "in", "tbs": "qdr:y", "num": 10})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            response = requests.post("https://google.serper.dev/search", headers=headers, data=payload, timeout=30)
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
        r = requests.get(url, headers=headers, timeout=(5, 10))
        if r.status_code == 200:
            content_type = r.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type: return ""
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]): tag.decompose()
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
        items_block += f"\n--- ITEM {i} ---\nTitle: {x['title']}\nLink: {x['link']}\nData: {(x.get('deep_text') or x.get('summary') or '')[:2000]}\n"
        
    exclusion_rule = ""
    if exclusions:
        exclusion_rule = f"""
CRITICAL CONTEXTUAL EXCLUSIONS:
The user has provided a list of Banned Intents/Keywords: {json.dumps(exclusions)}
You must evaluate the core INTENT of the webpage. 
- If the primary intent of the organization is to procure or offer [Banned Keywords], REJECT THEM (is_valid=False, entity_role="IRRELEVANT").
- HOWEVER, if these words merely appear in the organization's name (e.g., 'Department of Repair and Maintenance') or as background context, BUT their actual intent is to buy/sell the target product, you MUST ACCEPT THEM (is_valid=True).
"""

    prompt = f"""
You are a B2B Lead Generator. Capture as many potential leads as possible.
Evaluate EVERY ITEM and classify whether it is a:
1. 'BUYER' (Procurement, RFQ, tender, Capex, looking for vendors)
2. 'SELLER' (Manufacturer, distributor, supplier, offering products)
3. 'IRRELEVANT' (Blog post, job listing, totally unrelated)

RULES:
- If buyer name is hidden (like IndiaMART), set 'org' to "Hidden Buyer (IndiaMART)". Do NOT reject.
- Be forgiving. If unsure, mark is_valid=True.
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
                "is_valid": {"type": "BOOLEAN"},
                "entity_role": {"type": "STRING", "enum": ["BUYER", "SELLER", "IRRELEVANT"]},
                "org": {"type": "STRING"},
                "industry": {"type": "STRING"},
                "intent_summary": {"type": "STRING"},
                "dm_name": {"type": "STRING", "nullable": True},
                "dm_title": {"type": "STRING", "nullable": True}
            },
            "required": ["item_index", "is_valid", "entity_role", "org", "industry", "intent_summary"]
        }
    }

    for model_name in model_stack:
        try:
            chat = client.chats.create(model=model_name)
            res = chat.send_message(
                prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=schema, 
                    temperature=0.2
                )
            )
            raw_text = res.text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            return json.loads(raw_text)
        except Exception as e:
            err_str = str(e)
            if "NOT_FOUND" in err_str or "404" in err_str or "503" in err_str or "500" in err_str or "limit: 0" in err_str or "limit: 20" in err_str:
                print(f"    ⚠️ Model {model_name} unavailable/exhausted. Cascading...", flush=True)
                continue
            raise e
            
    raise Exception("All Gemini models unavailable or failed.")

def build_vector_matrix(target):
    return [
        f'"{target}" tender OR RFQ site:gov.in',
        f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com',
        f'"{target}" "looking for vendors" site:linkedin.com/posts',
        f'"{target}" "vendor empanelment" OR "request for quotation" India'
    ]

def run():
    print(">>> 📡 RADAR SCOUT ACTIVE (Domain Blacklist + Context Filter)", flush=True)
    if not WEBHOOK or not SECRET: return
    
    cloud_targets = []
    cloud_exclusions = []
    cloud_domains = []
    
    for attempt in range(3):
        try:
            res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_targets"}, timeout=60)
            cloud_targets = res.json().get("targets", [])
            break
        except Exception: time.sleep(5)
            
    for attempt in range(3):
        try:
            res_ex = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_exclusions"}, timeout=60)
            data = res_ex.json()
            cloud_exclusions = [e.strip() for e in data.get("exclusions", []) if e.strip()]
            cloud_domains = [d.strip().lower() for d in data.get("blocked_domains", []) if d.strip()]
            break
        except Exception: time.sleep(5)
            
    if not cloud_targets: return print("    -> No targets found.", flush=True)
    if cloud_exclusions: print(f"    -> Context Exclusions: {cloud_exclusions}", flush=True)
    if cloud_domains: print(f"    -> Banned Domains: {cloud_domains}", flush=True)

    search_matrix = []
    for t in cloud_targets: search_matrix.extend(build_vector_matrix(t))

    for query in search_matrix:
        print(f"\n[*] Scanning: {query}", flush=True)
        results = get_search_results(query)
        fresh_leads = []
        for r in results:
            link_lower = r['link'].lower()
            
            # THE PRE-FETCH GUILLOTINE
            if any(b_dom in link_lower for b_dom in cloud_domains):
                print(f"    🚫 Skipped Blocked Domain: {r['link']}", flush=True)
                continue
                
            if not is_duplicate(r['link']):
                r['deep_text'] = fetch_deep_text(r['link'])
                fresh_leads.append(r)
                
        if not fresh_leads: continue
            
        print(f"    -> AI Analyzing {len(fresh_leads)} links...", flush=True)
        try: ai_data = ai_analyze_batch(fresh_leads, cloud_exclusions)
        except Exception as e: 
            print(f"    -> AI Error: {e}", flush=True)
            continue
            
        for entity in ai_data:
            idx = entity.get("item_index")
            if idx is None or idx >= len(fresh_leads) or idx < 0: continue
            
            print(f"       [Vote] Valid: {entity.get('is_valid')} | Role: {entity.get('entity_role')} | Org: {entity.get('org')}", flush=True)
            
            if entity.get("is_valid") and entity.get("entity_role") in ["BUYER", "SELLER"]:
                is_supplier = (entity.get("entity_role") == "SELLER")
                intent_label = f"Supplier ({entity.get('intent_summary')})" if is_supplier else entity.get("intent_summary")

                payload = {
                    "secret": SECRET,
                    "action": "add_lead",
                    "is_supplier": is_supplier,
                    "target_sheet": "Suppliers" if is_supplier else "Inbox",
                    "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "Radar Scout",
                    "org": entity.get("org", "Unknown"),
                    "industry": entity.get("industry", "General"),
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
                        requests.post(WEBHOOK, json=payload, timeout=60)
                        dest = "Suppliers" if is_supplier else "Inbox"
                        print(f"    ✅ PUSHED [{entity.get('entity_role')}] -> {dest}: {entity['org']}", flush=True)
                        break
                    except Exception:
                        time.sleep(3)
                        
        time.sleep(15)

if __name__ == "__main__":
    run()
