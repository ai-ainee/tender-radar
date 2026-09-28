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
        valid_models = [m.name.lower() for m in client.models.list() if re.match(r'^models/gemini-\d+\.\d+-flash$', m.name.lower())]
        if valid_models:
            valid_models.sort(key=lambda x: float(re.search(r'\d+\.\d+', x).group()), reverse=True)
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return BEST_MODEL_STACK

def is_duplicate(link):
    if not WEBHOOK or not SECRET: return False
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "check_duplicate", "link": link}, timeout=30).json()
        return res.get("duplicate", False)
    except Exception: return False

def ddgs_search(query):
    ddgs = DDGS()
    # RESTORED: Strict 30-Day Time Filter
    return list(ddgs.text(query, timelimit="m", max_results=10, backend="lite"))

def get_search_results(query):
    results = []
    if SERPER_KEY:
        try:
            url = "https://google.serper.dev/search"
            # RESTORED: Strict 30-Day Time Filter (qdr:m)
            payload = json.dumps({"q": query, "gl": "in", "tbs": "qdr:m", "num": 10})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            response = requests.post(url, headers=headers, data=payload, timeout=30)
            if response.status_code == 200:
                for r in response.json().get("organic", []):
                    # NEW: Extracting Google's hidden Index Date
                    results.append({
                        "title": r.get("title", ""), 
                        "link": r.get("link", ""), 
                        "summary": r.get("snippet", ""),
                        "publish_date": r.get("date", "Recent") 
                    })
                if results: return results
        except Exception: pass

    if DDGS:
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(ddgs_search, query)
                res = future.result(timeout=15)
            for r in res:
                results.append({
                    "title": r.get("title", ""), 
                    "link": r.get("href", ""), 
                    "summary": r.get("body", ""),
                    "publish_date": "Recent"
                })
        except Exception: pass
    return results

def fetch_deep_text(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=(5, 10))
        if r.status_code == 200:
            content_type = r.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type and 'application/pdf' not in content_type: return ""
            soup = BeautifulSoup(r.text[:30000], "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]): tag.decompose()
            return soup.get_text(separator=" ", strip=True)
    except Exception: pass
    return ""

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_analyze_batch(batch):
    client = get_next_gemini_client()
    if not client: return []
    model_stack = get_flash_model_stack(client)
    
    items_block = ""
    for i, x in enumerate(batch):
        # NEW: Injecting the Publish Date directly into the AI's brain
        items_block += f"\n--- ITEM {i} ---\nTitle: {x['title']}\nLink: {x['link']}\nGoogle Publish Date: {x.get('publish_date', 'Recent')}\nData: {(x.get('deep_text') or x.get('summary') or '')[:3000]}\n"
        
    current_date = datetime.now().strftime("%B %d, %Y")
    current_year = datetime.now().year
        
    prompt = f"""
You are an elite B2B Market Analyst ensuring NO EXPIRED LEADS pass through.
Current Date: {current_date}. Year: {current_year}.

Evaluate EVERY ITEM and classify whether it is a:
1. 'BUYER' (Active procurement, RFQ, live tender, Capex project, looking for vendors)
2. 'SELLER' (Manufacturer, authorized distributor, OEM, supplier, stockist offering products)
3. 'IRRELEVANT' (Market research, stock tickers, consumer retail, jobs, completely foreign)

STRICT TIME FILTERS FOR BUYERS:
- If a deadline is explicitly shown in the text and it has ALREADY PASSED relative to {current_date}, REJECT IT (is_valid=False).
- If the 'Google Publish Date' explicitly says it was published in 2024, 2023, or older, REJECT IT.
- If NO deadline is shown, but the 'Google Publish Date' says "Recent", "X days ago", or a date within the last 2 months, ACCEPT IT. Give recent posts the benefit of the doubt.

RULES FOR SELLERS:
- Must be a confirmed business entity supplying/manufacturing the target product.

CONFIDENCE SCORE (1-100):
- Rate your certainty. Scores of 60+ are acceptable if the company name and recent intent are clear.

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
                "confidence_score": {"type": "INTEGER"},
                "org": {"type": "STRING"},
                "industry": {"type": "STRING"},
                "intent_summary": {"type": "STRING"},
                "deadline": {"type": "STRING", "nullable": True},
                "website": {"type": "STRING", "nullable": True},
                "dm_name": {"type": "STRING", "nullable": True},
                "dm_title": {"type": "STRING", "nullable": True}
            },
            "required": ["item_index", "is_valid", "entity_role", "confidence_score", "org", "industry", "intent_summary"]
        }
    }

    for model_name in model_stack:
        try:
            res = client.models.generate_content(
                model=model_name, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0)
            )
            raw_text = res.text.strip()
            if raw_text.startswith("```"): raw_text = re.sub(r'^```(?:json)?|```$', '', raw_text, flags=re.IGNORECASE | re.MULTILINE).strip()
            return json.loads(raw_text)
        except Exception as e:
            if "503" in str(e) or "500" in str(e) or "limit: 0" in str(e):
                print(f"    ⚠️ {model_name} overloaded. Cascading...", flush=True)
                continue
            raise e
    raise Exception("All Gemini models unavailable.")

def build_vector_matrix(target):
    current_year = datetime.now().year
    return [
        f'"{target}" tender OR RFQ {current_year} site:gov.in',
        f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com',
        f'"{target}" "looking for vendors" site:[linkedin.com/posts](https://linkedin.com/posts)',
        f'"{target}" "vendor empanelment" OR "request for quotation" India'
    ]

def run():
    print(">>> 📡 RADAR DUAL-SCOUT ACTIVE (Strict Time-Window Version)", flush=True)
    if not WEBHOOK or not SECRET: return
    try:
        cloud_targets = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_targets"}, timeout=30).json().get("targets", [])
    except Exception as e:
        print(f"❌ Failed to fetch targets: {e}", flush=True)
        return
        
    if not cloud_targets:
        print("    -> No targets found in Google Sheet '🎯 Targets'.", flush=True)
        return

    search_matrix = []
    for t in cloud_targets:
        search_matrix.extend(build_vector_matrix(t))

    for query in search_matrix:
        print(f"\n[*] Scanning: {query[:95]}...", flush=True)
        results = get_search_results(query)
        fresh_leads = []
        for r in results:
            if not is_duplicate(r['link']):
                r['deep_text'] = fetch_deep_text(r['link'])
                fresh_leads.append(r)
                
        if not fresh_leads: 
            print("    -> 0 new leads found (all duplicates or filtered out).", flush=True)
            continue
            
        print(f"    -> Analyzing {len(fresh_leads)} candidates with AI...", flush=True)
        try: ai_data = ai_analyze_batch(fresh_leads)
        except Exception as e: 
            print(f"    -> AI Error: {e}", flush=True)
            continue
            
        for entity in ai_data:
            idx = entity.get("item_index")
            if idx is None or idx >= len(fresh_leads) or idx < 0: continue
            
            print(f"       [AI Vote] Valid: {entity.get('is_valid')} | Role: {entity.get('entity_role')} | Score: {entity.get('confidence_score')} | Org: {entity.get('org', 'Unknown')}", flush=True)
            
            if (entity.get("is_valid") and 
                entity.get("confidence_score", 0) >= 60 and 
                entity.get("org") and 
                entity.get("org").lower() not in ["unknown firm", "linkedin", "naukri", "gem", "indiamart"]):
                
                role = entity.get("entity_role", "BUYER")
                if role == "IRRELEVANT": continue
                
                is_supplier = (role == "SELLER")
                deadline_note = f" [Deadline: {entity.get('deadline')}]" if entity.get("deadline") else ""
                intent_label = f"Supplier ({entity.get('intent_summary')})" if is_supplier else f"{entity.get('intent_summary')}{deadline_note}"

                payload = {
                    "secret": SECRET,
                    "action": "add_lead",
                    "is_supplier": is_supplier,
                    "target_sheet": "Suppliers" if is_supplier else "Inbox",
                    "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "Omni-Radar",
                    "org": entity.get("org", "Unknown"),
                    "industry": entity.get("industry", "General"),
                    "intent": intent_label,
                    "dm_name": entity.get("dm_name") or "N/A",
                    "dm_title": entity.get("dm_title") or "N/A",
                    "link": fresh_leads[idx]['link'],
                    "email": "N/A",
                    "phone": "N/A",
                    "website": entity.get("website") or "N/A"
                }
                try:
                    requests.post(WEBHOOK, json=payload, timeout=30)
                    dest = "Suppliers" if is_supplier else "Inbox"
                    print(f"    ✅ PUSHED [{role}] -> {dest}: {entity['org']}", flush=True)
                except Exception: pass
        
        time.sleep(5)

if __name__ == "__main__":
    run()
