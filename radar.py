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
        items_block += f"\n--- ITEM {i} ---\nTarget Product: {x.get('target', 'Unknown')}\nQuery Type: {x.get('query_type', 'Direct')}\nTitle: {x['title']}\nLink: {x['link']}\nData: {(x.get('deep_text') or x.get('summary') or '')[:2000]}\n"
        
    exclusion_rule = ""
    if exclusions:
        exclusion_rule = f"""
CRITICAL CONTEXTUAL EXCLUSIONS:
Banned Intents/Keywords: {json.dumps(exclusions)}
- If the primary intent is to procure or offer these EXACT [Banned Keywords], REJECT THEM (is_valid=False).
"""

    prompt = f"""
You are an expert B2B Ecosystem Analyst.
You evaluate news, tenders, and company profiles to find leads related to the 'Target Product'.

CLASSIFICATION:
1. 'BUYER' (Direct Procurement): Actively purchasing or issuing an RFQ/tender for the Target Product.
2. 'PROJECT_BUYER' (Derived Demand): An organization winning a contract or setting up a facility that REQUIRES the Target Product.
3. 'SERVICE_USER' (Ecosystem Prospect): A company offering commercial services USING the Target Product (e.g., AutoCAD drafting, design services). These are high-value prospects because they must purchase the product to do their job.
4. 'SELLER' (Competitor/Distributor): Companies manufacturing or supplying the Target Product OR its direct alternatives/competitors.
5. 'IRRELEVANT': Unrelated products, generic jobs, or consumer retail.

ECOSYSTEM AWARENESS RULE:
- Do NOT reject companies offering services related to the Target Product. They belong in 'SERVICE_USER'.
- Do NOT reject alternative products. They belong in 'SELLER'.

RULES FOR LOCATION ('city' and 'state'):
- Identify the specific Indian City (e.g., 'Pune', 'Chennai') and State/UT (e.g., 'Maharashtra').
- If unspecified or nationwide, output "Unknown".

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
                "product_match_reasoning": {"type": "STRING", "description": "Explain how this entity interacts with the Target Product (Buys it, Uses it for services, or Sells it)."},
                "is_valid": {"type": "BOOLEAN"},
                "entity_role": {"type": "STRING", "enum": ["BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER", "IRRELEVANT"]},
                "org": {"type": "STRING", "description": "Company name or agency"},
                "city": {"type": "STRING", "description": "Indian City"},
                "state": {"type": "STRING", "description": "Indian state or UT"},
                "industry": {"type": "STRING"},
                "intent_summary": {"type": "STRING", "description": "Brief description of what they are doing"},
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
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=schema, 
                    temperature=0.0
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
    current_year = datetime.now().year
    return [
        {"type": "Direct", "query": f'"{target}" tender OR RFQ site:gov.in'},
        {"type": "Direct", "query": f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com'},
        {"type": "Project", "query": f'"{target}" ("Letter of Award" OR "awarded contract" OR "lowest bidder" OR "L1 bidder") India {current_year}'},
        {"type": "Project", "query": f'"{target}" ("MoU signed" OR "groundbreaking ceremony" OR "setting up new plant" OR "greenfield facility") India'},
        {"type": "Project", "query": f'site:bseindia.com/xml-data/corpfiling/ "{target}" ("bagged order" OR "contract worth" OR "LoA")'},
        {"type": "Direct", "query": f'"{target}" "looking for vendors" site:linkedin.com/posts'}
    ]

def run():
    print(">>> 📡 RADAR SCOUT ACTIVE (Ecosystem & Alternative Aware)", flush=True)
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
                print(f"    🚫 Skipped Blocked Domain: {r['link']}", flush=True)
                continue
                
            if not is_duplicate(r['link']):
                r['deep_text'] = fetch_deep_text(r['link'])
                r['target'] = target_product
                r['query_type'] = query_type
                fresh_leads.append(r)
                
        if not fresh_leads: continue
            
        print(f"    -> AI Analyzing {len(fresh_leads)} links against '{target_product}'...", flush=True)
        try: ai_data = ai_analyze_batch(fresh_leads, cloud_exclusions)
        except Exception as e: 
            print(f"    -> AI Error: {e}", flush=True)
            continue
            
        for entity in ai_data:
            idx = entity.get("item_index")
            if idx is None or idx >= len(fresh_leads) or idx < 0: continue
            
            reason = entity.get("product_match_reasoning", "No reasoning provided")
            role = entity.get('entity_role')
            print(f"       [Reasoning] {reason}")
            print(f"       [Vote] Valid: {entity.get('is_valid')} | Role: {role} | Location: {entity.get('city')}, {entity.get('state')} | Org: {entity.get('org')}", flush=True)
            
            if entity.get("is_valid") and role in ["BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER"]:
                is_supplier = (role == "SELLER")
                
                # Intelligent Routing
                if role == "PROJECT_BUYER": source = "Project-Radar"
                elif role == "SERVICE_USER": source = "Ecosystem-Scout"
                else: source = "Radar Scout"
                
                payload = {
                    "secret": SECRET,
                    "action": "add_lead",
                    "is_supplier": is_supplier,
                    "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": source,
                    "org": entity.get("org", "Unknown"),
                    "city": entity.get("city", "Unknown"),
                    "state": entity.get("state", "Unknown"),
                    "industry": target_product,
                    "intent": entity.get("intent_summary"),
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
                        if is_supplier: dest = "Suppliers"
                        elif role == "PROJECT_BUYER": dest = "Projects & MOUs"
                        else: dest = "Inbox"
                        print(f"    ✅ PUSHED [{role}] -> {dest}: {entity['org']}", flush=True)
                        break
                    except Exception:
                        time.sleep(3)
                        
        time.sleep(15)

if __name__ == "__main__":
    run()
