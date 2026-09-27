import os
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

# --- DYNAMIC MODEL SELECTOR (Cached for Speed) ---
BEST_MODEL_CACHE = None

def get_best_gemini_model(client):
    global BEST_MODEL_CACHE
    if BEST_MODEL_CACHE: return BEST_MODEL_CACHE # Return instantly if already known
    try:
        # Fetches all models, filters for 'flash', and sorts to get the highest version automatically
        models = [m.name for m in client.models.list() if 'flash' in m.name.lower()]
        if models:
            models.sort(reverse=True)
            BEST_MODEL_CACHE = models[0]
            print(f"    🧠 Auto-Detected Latest AI Model: {BEST_MODEL_CACHE}")
            return BEST_MODEL_CACHE
    except Exception:
        pass
    BEST_MODEL_CACHE = "gemini-2.0-flash" # Immortal stable fallback
    return BEST_MODEL_CACHE

def is_duplicate(link):
    if not WEBHOOK or not SECRET: return False
    try:
        payload = {"secret": SECRET, "action": "check_duplicate", "link": link}
        res = requests.post(WEBHOOK, json=payload, timeout=10).json()
        return res.get("duplicate", False)
    except Exception:
        return False

def get_search_results(query):
    results = []
    if SERPER_KEY:
        try:
            url = "https://google.serper.dev/search"
            payload = json.dumps({"q": query, "gl": "in", "num": 10})
            headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
            response = requests.post(url, headers=headers, data=payload, timeout=15)
            if response.status_code == 200:
                for r in response.json().get("organic", []):
                    results.append({"title": r.get("title", ""), "link": r.get("link", ""), "summary": r.get("snippet", "")})
                if results: return results
        except Exception:
            pass

    if DDGS:
        try:
            print("    🔄 Using DuckDuckGo Fallback...")
            ddgs = DDGS()
            res = list(ddgs.text(query, max_results=10, backend="lite"))
            for r in res:
                results.append({"title": r.get("title", ""), "link": r.get("href", ""), "summary": r.get("body", "")})
        except Exception:
            pass
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
    except Exception:
        pass
    return ""

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def ai_analyze_batch(batch):
    client = get_next_gemini_client()
    if not client: return []
    
    best_model = get_best_gemini_model(client) # Will use cache instantly
    
    items_block = ""
    for i, x in enumerate(batch):
        body = x.get("deep_text") or x.get("summary") or ""
        items_block += f"\n--- ITEM {i} ---\nTitle: {x['title']}\nLink: {x['link']}\nData: {body[:3000]}\n"
        
    past_year = datetime.now().year - 1
        
    prompt = f"""
You are an elite B2B Sales AI analyzing CAD/BIM/AEC market signals in India.
CRITICAL: Evaluate EVERY SINGLE ITEM in the batch.

REJECT (is_lead=False) IMMEDIATELY IF:
1. Junk, SEO Spam, Adult, Market Research Reports, Stock News.
2. Located OUTSIDE India.
3. Freelance/Student gigs.
4. STRICT DATE CHECK: Explicitly shows a year from {past_year} or older.

ACCEPT (is_lead=True): Genuine CAD/BIM buyers, active RFQs, corporate hiring roles, capex projects, AND resellers/dealers/training partners IN INDIA.

CLASSIFICATION MATRIX for 'lead_type':
- Asking for quotes/RFQ -> 'Active Private Buyer (RFQ)'
- Dealer/Institute -> 'Suppliers'
- Government portal -> 'Govt Tender'
- LinkedIn source -> NEVER Govt Tender
- Recruiting/job opening -> 'Hiring Mandate'
- Factory/EPC project -> 'Private Capex'
- Else -> 'Corporate Lead'

RULES:
- 'org' MUST be the actual client/company name. NEVER 'LinkedIn', 'Naukri', or 'Indeed'.

DATA BATCH:
{items_block}
"""
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "item_index": {"type": "INTEGER"}, "is_lead": {"type": "BOOLEAN"},
                "org": {"type": "STRING"}, "lead_type": {"type": "STRING"},
                "website": {"type": "STRING", "nullable": True},
                "dm_name": {"type": "STRING", "nullable": True}, "dm_title": {"type": "STRING", "nullable": True}
            }, "required": ["item_index", "is_lead", "org", "lead_type"]
        }
    }

    try:
        res = client.models.generate_content(
            model=best_model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0)
        )
        raw_text = res.text.strip()
        if raw_text.startswith("```"): raw_text = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"    ⚠️ Gemini Error. Retrying... ({e})")
        raise e 

def run():
    print(">>> 📡 RADAR SCOUT V2 ACTIVE (Production Ready)")
    keywords = ["Autodesk drafting services requirement India", "BIM implementation tender India", "MEP design consultancy request for proposal", "structural detailing RFQ India"]
    
    for kw in keywords:
        print(f"\n[*] Scouting keyword: {kw}")
        results = get_search_results(kw)
        
        fresh_leads = []
        for r in results:
            if not is_duplicate(r['link']):
                print(f"    -> Deep fetching text for: {r['link'][:50]}...")
                r['deep_text'] = fetch_deep_text(r['link'])
                fresh_leads.append(r)
                
        if not fresh_leads:
            print("    -> No fresh leads found. Skipping.")
            continue
            
        print(f"    -> Analyzing {len(fresh_leads)} items with AI...")
        try:
            ai_data = ai_analyze_batch(fresh_leads)
        except Exception:
            print("    ❌ Failed to analyze batch after retries. Skipping.")
            continue
            
        for lead in ai_data:
            if lead.get("is_lead") and lead.get("org") and lead.get("org").lower() not in ["linkedin", "naukri", "indeed"]:
                idx = lead.get("item_index")
                if idx is None or idx >= len(fresh_leads) or idx < 0: continue
                
                payload = {
                    "secret": SECRET, "action": "add_lead", "lead_id": str(uuid.uuid4())[:8],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "Web",
                    "org": lead.get("org", "Unknown"), "industry": "AEC", 
                    "intent": lead.get("lead_type", "Corporate Lead"),
                    "dm_name": lead.get("dm_name") or "N/A", "dm_title": lead.get("dm_title") or "N/A", 
                    "link": fresh_leads[idx]['link'], "email": "N/A", "phone": "N/A", 
                    "website": lead.get("website") or "N/A"
                }
                try:
                    requests.post(WEBHOOK, json=payload, timeout=10)
                    print(f"    ✅ Verified & Pushed: {lead['org']}")
                except Exception as e:
                    print(f"    ❌ Failed to push to CRM: {e}")
        time.sleep(2)

if __name__ == "__main__":
    run()
