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
    return list(ddgs.text(query, timelimit="y", max_results=10, backend="lite"))

def get_search_results(query):
    results = []
    if SERPER_KEY:
        try:
            url = "https://google.serper.dev/search"
            payload = json.dumps({"q": query, "gl": "in", "tbs": "qdr:y", "num": 10})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            response = requests.post(url, headers=headers, data=payload, timeout=30)
            if response.status_code == 200:
                for r in response.json().get("organic", []):
                    results.append({"title": r.get("title", ""), "link": r.get("link", ""), "summary": r.get("snippet", "")})
                if results: return results
        except Exception: pass

    if DDGS:
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(ddgs_search, query)
                res = future.result(timeout=15)
            for r in res:
                results.append({"title": r.get("title", ""), "link": r.get("href", ""), "summary": r.get("body", "")})
        except Exception: pass
    return results

def fetch_deep_text(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 10))
        if r.status_code == 200:
            content_type = r.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type: return ""
            soup = BeautifulSoup(r.text[:50000], "html.parser")
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
        items_block += f"\n--- ITEM {i} ---\nTitle: {x['title']}\nLink: {x['link']}\nData: {(x.get('deep_text') or x.get('summary') or '')[:3000]}\n"
        
    current_year = datetime.now().year
    cutoff_year = current_year - 2
        
    prompt = f"""
You are an elite B2B Sales AI. Goal: Capture organizations actively procuring PHYSICAL PRODUCTS, equipment, or services. Evaluate EVERY ITEM.

REJECT (is_lead=False) ONLY IF:
1. Market Research/News/B2C/Outside India.
2. EXPIRED: The current year is {current_year}. If the document explicitly shows a tender deadline or publication date from {cutoff_year} or older (e.g. {cutoff_year}, {cutoff_year-1}), REJECT IT IMMEDIATELY.

ACCEPT (is_lead=True) IF: Actively buying or inviting tenders.

CLASSIFICATION:
- Active Tender / RFQ -> 'Active Bulk Buyer (RFQ)'
- Capex/Setup -> 'Capex Buyer'
- General supply -> 'Corporate Sourcing'
- Looking for vendors -> 'Vendor Empanelment'
- Selling goods -> 'Supplier'

RULES:
- 'org' MUST be actual client name (Use "Unknown Firm" if hidden). Never 'LinkedIn'.
- 'industry' MUST be the specific product category.

DATA BATCH:
{items_block}
"""
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "item_index": {"type": "INTEGER"}, "is_lead": {"type": "BOOLEAN"},
                "org": {"type": "STRING"}, "industry": {"type": "STRING"}, 
                "lead_type": {"type": "STRING"},
                "website": {"type": "STRING", "nullable": True},
                "dm_name": {"type": "STRING", "nullable": True}, "dm_title": {"type": "STRING", "nullable": True}
            }, "required": ["item_index", "is_lead", "org", "industry", "lead_type"]
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

def run():
    print(">>> 📡 RADAR SCOUT ACTIVE (Production)", flush=True)
    if not WEBHOOK or not SECRET: return
    try:
        cloud_targets = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_targets"}, timeout=30).json().get("targets", [])
    except Exception as e: return print(f"❌ Failed to fetch targets: {e}", flush=True)
    if not cloud_targets: return print("    -> No targets found.", flush=True)

    current_year = datetime.now().year
    keywords = [f'"{t}" AND ("Request for Quotation" OR "tender" OR "vendor empanelment") {current_year} India' for t in cloud_targets]

    for kw in keywords:
        print(f"\n[*] Scouting keyword: {kw}", flush=True)
        results = get_search_results(kw)
        fresh_leads = []
        for r in results:
            if not is_duplicate(r['link']):
                r['deep_text'] = fetch_deep_text(r['link'])
                fresh_leads.append(r)
                
        if not fresh_leads: continue
        print(f"    -> Analyzing {len(fresh_leads)} items with AI...", flush=True)
        try: ai_data = ai_analyze_batch(fresh_leads)
        except Exception: continue
            
        for lead in ai_data:
            if lead.get("is_lead") and lead.get("org") and lead.get("org").lower() not in ["linkedin", "naukri", "indeed"]:
                idx = lead.get("item_index")
                if idx is None or idx >= len(fresh_leads) or idx < 0: continue
                payload = {
                    "secret": SECRET, "action": "add_lead", "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "Web",
                    "org": lead.get("org", "Unknown"), "industry": lead.get("industry", "General"), 
                    "intent": lead.get("lead_type", "Corporate Sourcing"),
                    "dm_name": lead.get("dm_name") or "N/A", "dm_title": lead.get("dm_title") or "N/A", 
                    "link": fresh_leads[idx]['link'], "email": "N/A", "phone": "N/A", "website": lead.get("website") or "N/A"
                }
                try:
                    requests.post(WEBHOOK, json=payload, timeout=30)
                    print(f"    ✅ Pushed to Inbox: {lead['org']}", flush=True)
                except Exception: pass
        time.sleep(15)

if __name__ == "__main__":
    run()
