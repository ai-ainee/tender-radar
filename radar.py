import os
import re
import json
import time
import uuid
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

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

BEST_MODEL_CACHE = None
def get_best_gemini_model(client):
    global BEST_MODEL_CACHE
    if BEST_MODEL_CACHE: return BEST_MODEL_CACHE
    try:
        valid_models = []
        for m in client.models.list():
            name = m.name.lower()
            if re.match(r'^models/gemini-\d+\.\d+-flash$', name):
                valid_models.append(name)
        if valid_models:
            valid_models.sort(key=lambda x: float(re.search(r'\d+\.\d+', x).group()), reverse=True)
            BEST_MODEL_CACHE = valid_models[0]
            return BEST_MODEL_CACHE
    except Exception: pass
    BEST_MODEL_CACHE = "gemini-2.5-flash"
    return BEST_MODEL_CACHE

def is_duplicate(link):
    if not WEBHOOK or not SECRET: return False
    try:
        payload = {"secret": SECRET, "action": "check_duplicate", "link": link}
        res = requests.post(WEBHOOK, json=payload, timeout=30).json()
        return res.get("duplicate", False)
    except Exception: return False

def get_search_results(query):
    results = []
    if SERPER_KEY:
        try:
            url = "https://google.serper.dev/search"
            payload = json.dumps({"q": query, "gl": "in", "num": 10})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            response = requests.post(url, headers=headers, data=payload, timeout=30)
            if response.status_code == 200:
                for r in response.json().get("organic", []):
                    results.append({"title": r.get("title", ""), "link": r.get("link", ""), "summary": r.get("snippet", "")})
                if results: return results
        except Exception: pass

    if DDGS:
        try:
            ddgs = DDGS()
            res = list(ddgs.text(query, max_results=10, backend="lite"))
            for r in res:
                results.append({"title": r.get("title", ""), "link": r.get("href", ""), "summary": r.get("body", "")})
        except Exception: pass
    return results

def fetch_deep_text(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        with requests.get(url, headers=headers, timeout=8, stream=True) as r:
            if r.status_code == 200:
                content_type = r.headers.get('Content-Type', '').lower()
                if 'text/html' not in content_type: return ""
                html_content = r.raw.read(50000, decode_content=True)
                soup = BeautifulSoup(html_content, "html.parser")
                for tag in soup(["script", "style", "nav", "footer"]): tag.decompose()
                return soup.get_text(separator=" ", strip=True)
    except Exception: pass
    return ""

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_analyze_batch(batch):
    client = get_next_gemini_client()
    if not client: return []
    
    best_model = get_best_gemini_model(client)
    items_block = ""
    for i, x in enumerate(batch):
        body = x.get("deep_text") or x.get("summary") or ""
        items_block += f"\n--- ITEM {i} ---\nTitle: {x['title']}\nLink: {x['link']}\nData: {body[:3000]}\n"
        
    past_year = datetime.now().year - 1
        
    prompt = f"""
You are an elite B2B Sales AI analyzing universal procurement and supply chain signals in India.
Your goal is to capture organizations actively procuring or sourcing PHYSICAL PRODUCTS, materials, equipment, software, or services.
Evaluate EVERY SINGLE ITEM.

REJECT (is_lead=False) ONLY IF:
1. It is a Market Research Report.
2. It is Stock Market/Financial News.
3. It is explicitly located OUTSIDE of India.
4. It is a B2C/retail post or a freelance gig.
5. Explicit date from {past_year} or older.

ACCEPT (is_lead=True) IF:
The organization is looking to BUY, PROCURE, SOURCE, or INVITE TENDERS. 

CLASSIFICATION MATRIX for 'lead_type':
- Active Tender / RFQ -> 'Active Bulk Buyer (RFQ)'
- Capex/Setup -> 'Capex Buyer'
- General supply needs -> 'Corporate Sourcing'
- Looking for vendors -> 'Vendor Empanelment'
- Selling goods (Not buying) -> 'Supplier'

RULES:
- 'org' MUST be the actual client name. NEVER 'LinkedIn', 'Naukri', or 'GeM'. Use "Unknown Firm" if hidden.
- 'industry' MUST be the specific product/service category they are buying.

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

    try:
        res = client.models.generate_content(
            model=best_model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0)
        )
        raw_text = res.text.strip()
        if raw_text.startswith("```"): raw_text = re.sub(r'^```(?:json)?|```$', '', raw_text, flags=re.IGNORECASE | re.MULTILINE).strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"    ⚠️ Gemini Error. Retrying... ({e})")
        raise e 

def run():
    print(">>> 📡 RADAR SCOUT V2 ACTIVE (Dynamic Targets)")
    if not WEBHOOK or not SECRET: return
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_targets"}, timeout=30)
        cloud_targets = res.json().get("targets", [])
    except Exception as e: return
        
    if not cloud_targets:
        print("    -> No targets found in Google Sheet.")
        return

    keywords = [f'"{t}" AND ("Request for Quotation" OR "tender" OR "vendor empanelment") India' for t in cloud_targets]

    for kw in keywords:
        print(f"\n[*] Scouting keyword: {kw}")
        results = get_search_results(kw)
        fresh_leads = []
        for r in results:
            if not is_duplicate(r['link']):
                r['deep_text'] = fetch_deep_text(r['link'])
                fresh_leads.append(r)
                
        if not fresh_leads: continue
            
        print(f"    -> Analyzing {len(fresh_leads)} items with AI...")
        try: ai_data = ai_analyze_batch(fresh_leads)
        except Exception: continue
            
        for lead in ai_data:
            if lead.get("is_lead") and lead.get("org") and lead.get("org").lower() not in ["linkedin", "naukri", "indeed"]:
                idx = lead.get("item_index")
                if idx is None or idx >= len(fresh_leads) or idx < 0: continue
                
                payload = {
                    "secret": SECRET, "action": "add_lead", "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "Web",
                    "org": lead.get("org", "Unknown"), 
                    "industry": lead.get("industry", "General Products"), 
                    "intent": lead.get("lead_type", "Corporate Sourcing"),
                    "dm_name": lead.get("dm_name") or "N/A", "dm_title": lead.get("dm_title") or "N/A", 
                    "link": fresh_leads[idx]['link'], "email": "N/A", "phone": "N/A", 
                    "website": lead.get("website") or "N/A"
                }
                try: requests.post(WEBHOOK, json=payload, timeout=30)
                except Exception: pass
        time.sleep(15)

if __name__ == "__main__":
    run()
