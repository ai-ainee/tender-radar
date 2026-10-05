import os
import re
import ssl
import json
import time
import uuid
import logging
import warnings
import requests
import io
import hashlib
import concurrent.futures
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup
from datetime import datetime
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

warnings.filterwarnings("ignore")
logging.getLogger("google.genai.models").setLevel(logging.ERROR)

# ==========================================
# INJECT TODAY'S DATE FOR TIME-AWARE AI & REGEX
# ==========================================
TODAY = datetime.now()
CURRENT_DATE_STR = TODAY.strftime("%d %B %Y")

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
SECRET = os.environ.get("WEBHOOK_SECRET")
raw_serper_keys = os.environ.get("SERPER_API_KEY", "")
SERPER_KEYS = [k.strip() for k in raw_serper_keys.split(",") if k.strip()]
current_serper_index = 0

raw_keys = os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
current_key_index = 0

EXISTING_URLS_CACHE = set()
EXISTING_FINGERPRINTS_CACHE = {}  # Format: {fingerprint: timestamp}
FINGERPRINT_TTL_SECONDS = 30 * 86400  # 30 days

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
    """Dynamically identifies macro buyer industries to query MCA / ZaubaCorp registries."""
    try:
        prompt = (
            f"What are 3 primary commercial or industrial sectors in India that purchase or deploy '{target}'? "
            f"Respond strictly with 3 space-separated or OR-separated single keywords (e.g. Architecture OR Engineering OR Infrastructure)."
        )
        chat = client.chats.create(model="gemini-1.5-flash")
        res = chat.send_message(prompt)
        
        cleaned = re.sub(r'[^a-zA-Z\s]', '', res.text).strip().split()
        if cleaned:
            return " OR ".join(cleaned[:3])
    except Exception:
        pass
    return f'"{target}"'

# --- URL & FINGERPRINT HELPERS ---
def canonicalize_url(url):
    """Strips tracking query strings (utm, ref, session IDs) and standardizes formatting."""
    if not url: return ""
    try:
        parsed = urlparse(url.strip())
        clean_netloc = parsed.netloc.lower().replace("www.", "")
        clean_path = parsed.path.rstrip('/')
        return urlunparse((parsed.scheme.lower(), clean_netloc, clean_path, '', '', ''))
    except Exception:
        return url.strip().lower()

def normalize_text_key(text):
    """Normalizes names by removing punctuation and corporate suffixes."""
    if not text or text.lower() == "unknown": return ""
    clean = text.lower()
    clean = re.sub(r'\b(pvt|private|ltd|limited|llp|inc|corp|corporation|co|enterprises)\b\.?', '', clean)
    clean = re.sub(r'[^a-z0-9\s]', '', clean)
    return " ".join(clean.split())

def make_lead_fingerprint(entity, target_product):
    """Generates a unique deduplication key for the lead."""
    ref_id = str(entity.get("ref_id", "")).strip().upper()
    org = normalize_text_key(entity.get("org", "Unknown"))
    
    if ref_id and ref_id != "N/A" and len(ref_id) > 4:
        return f"REF::{ref_id}"
    
    if "MCA" in str(entity.get("intent_summary", "")) or "Incorporation" in str(entity.get("intent_summary", "")):
        return f"MCA::{org}"
        
    scope = normalize_text_key(entity.get("project_scope_key", "general-procurement")).replace(" ", "-")
    city = normalize_text_key(entity.get("city", "pan-india")).replace(" ", "-")
    target = normalize_text_key(target_product).replace(" ", "-")
    
    return f"SCOPE::{org}::{city}::{target}::{scope}"

# --- DOUBLE-BACKUP SAVE SYSTEM ---
def add_to_cache(link, fingerprint=None):
    """Adds a URL and Fingerprint to active memory AND saves it to the backup text files."""
    if not link: return
    clean_link = canonicalize_url(link)
    
    if clean_link not in EXISTING_URLS_CACHE:
        EXISTING_URLS_CACHE.add(clean_link)
        try:
            with open("seen_links.txt", "a", encoding="utf-8") as f:
                f.write(clean_link + "\n")
        except Exception: pass
        
    if fingerprint:
        now = time.time()
        EXISTING_FINGERPRINTS_CACHE[fingerprint] = now
        try:
            with open("seen_fingerprints.txt", "a", encoding="utf-8") as f:
                f.write(f"{fingerprint}::{int(now)}\n")
        except Exception: pass

# --- LOAD SYSTEM (TEXT FILE FIRST, GOOGLE SECOND) ---
def load_existing_urls_cache():
    global EXISTING_URLS_CACHE, EXISTING_FINGERPRINTS_CACHE
    
    # 1. Load URLs
    if os.path.exists("seen_links.txt"):
        try:
            with open("seen_links.txt", "r", encoding="utf-8") as f:
                for line in f:
                    val = line.strip()
                    if val: EXISTING_URLS_CACHE.add(canonicalize_url(val))
            print(f"[*] Loaded {len(EXISTING_URLS_CACHE)} URLs from seen_links.txt", flush=True)
        except Exception as e: print(f"⚠️ Error reading seen_links.txt: {e}")

    # 2. Load Fingerprints
    now = time.time()
    if os.path.exists("seen_fingerprints.txt"):
        try:
            with open("seen_fingerprints.txt", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("::")
                    if len(parts) >= 2:
                        fp, ts = "::".join(parts[:-1]), float(parts[-1])
                        if now - ts < FINGERPRINT_TTL_SECONDS:
                            EXISTING_FINGERPRINTS_CACHE[fp] = ts
            print(f"[*] Loaded {len(EXISTING_FINGERPRINTS_CACHE)} active fingerprints.", flush=True)
        except Exception as e: print(f"⚠️ Error reading seen_fingerprints.txt: {e}")

    # 3. Load from Webhook with 60-Second Timeout Fix
    if not WEBHOOK or not SECRET: 
        print("⚠️ Webhook credentials missing. Relying ONLY on text backups.", flush=True)
        return
        
    for attempt in range(1, 4):
        try:
            res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_all_urls"}, timeout=(10, 60))
            if res.status_code == 200:
                data = res.json()
                for u in data.get("urls", []):
                    if u.strip(): EXISTING_URLS_CACHE.add(canonicalize_url(u))
                for fp in data.get("fingerprints", []):
                    if fp.strip(): EXISTING_FINGERPRINTS_CACHE[fp.strip()] = now
                print(f"[*] Synced cache with Sheets. Total URLs: {len(EXISTING_URLS_CACHE)} | FPs: {len(EXISTING_FINGERPRINTS_CACHE)}", flush=True)
                return
            else:
                print(f"⚠️ Sheet Sync HTTP {res.status_code}")
        except Exception as e:
            print(f"⚠️ Cache load failed (Attempt {attempt}/3): {e}", flush=True)
            time.sleep(5)
            
    print("⚠️ WARNING: Failed to load from Google Sheets. Relying on local backups.", flush=True)

def is_duplicate_cached(link):
    if not link: return False
    return canonicalize_url(link) in EXISTING_URLS_CACHE

def get_search_results(query):
    global current_serper_index
    results = []
    
    while current_serper_index < len(SERPER_KEYS):
        api_key = SERPER_KEYS[current_serper_index]
        try:
            payload = json.dumps({"q": query, "gl": "in", "tbs": "qdr:m", "num": 10})
            headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
            response = requests.post("https://google.serper.dev/search", headers=headers, data=payload, timeout=25)
            
            if response.status_code == 200:
                for r in response.json().get("organic", []):
                    results.append({
                        "title": r.get("title", ""), 
                        "link": r.get("link", ""), 
                        "summary": r.get("snippet", ""),
                        "date": r.get("date", "")
                    })
                return results
            elif response.status_code in [403, 429]:
                print(f"    ⚠️ Serper key {current_serper_index + 1} exhausted. Switching to next key...", flush=True)
                current_serper_index += 1
            else:
                break
        except Exception:
            break

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
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://www.google.com/"
        }
        session = requests.Session()
        session.headers.update(headers)
        
        # Disable SSL verification for Indian govt certificates that lack CA chains
        r = session.get(url, timeout=(6, 14), verify=False)
        if r.status_code != 200:
            return ""
            
        content_type = r.headers.get('Content-Type', '').lower()
        clean_url = url.lower().split('?')[0]

        if 'application/pdf' in content_type or clean_url.endswith('.pdf') or 'showbiddocument' in clean_url:
            if PdfReader:
                try:
                    reader = PdfReader(io.BytesIO(r.content))
                    extracted_pages = []
                    for page in reader.pages[:5]:
                        t = page.extract_text()
                        if t: extracted_pages.append(t)
                    return re.sub(r'\s+', ' ', " ".join(extracted_pages)).strip()[:4000]
                except Exception:
                    pass

        if 'text/html' in content_type or not content_type:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)[:4000]
    except Exception:
        pass
    return ""

def is_tender_active(raw_text):
    if not raw_text:
        return False  # If no content could be read from a tender portal, do not risk an expired bid

    text = raw_text.replace("\n", " ")
    
    # 1. Look for explicit End/Closing/Submission Deadlines
    pattern = (
        r"(?:Bid\s+End(?:\s+Date)?(?:/\s*Time)?|"
        r"Submission\s+(?:End\s+Date|Deadline|Closing\s+Date)|"
        r"Closing\s+Date|Due\s+Date|Last\s+Date(?:\s+of\s+Submission)?)"
        r"\s*[:\-]?\s*"
        r"(\d{1,2}[-/\.\s](?:[A-Za-z]{3,9}|\d{1,2})[-/\.\s]\d{4})"
    )
    matches = re.findall(pattern, text, re.IGNORECASE)
    
    found_end_dates = []
    for date_str in matches:
        clean_str = re.sub(r"[/\\.\s]+", "-", date_str.strip())
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%d-%m-%y"):
            try:
                found_end_dates.append(datetime.strptime(clean_str, fmt))
                break
            except ValueError:
                continue

    # If an end date exists, check it directly against today
    if found_end_dates:
        for tender_date in found_end_dates:
            if tender_date.date() >= TODAY.date():
                return True
        print("    🚫 Regex Bouncer: Bid end date has passed. Dropping.", flush=True)
        return False

    # 2. Heuristic for GeM snippets missing end dates:
    # Check "Dated: DD-MM-YYYY" or "Bid Start Date". GeM bids rarely stay active longer than 21 days.
    start_pattern = r"(?:Dated|Bid\s+Start\s+Date)\s*[:\-]?\s*(\d{1,2}[-/\.\s](?:[A-Za-z]{3,9}|\d{1,2})[-/\.\s]\d{4})"
    start_matches = re.findall(start_pattern, text, re.IGNORECASE)
    for s_date_str in start_matches:
        clean_str = re.sub(r"[/\\.\s]+", "-", s_date_str.strip())
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y"):
            try:
                start_dt = datetime.strptime(clean_str, fmt)
                if (TODAY - start_dt).days > 25:
                    print(f"    🚫 Regex Bouncer: Bid was posted >25 days ago ({start_dt.strftime('%d-%b-%Y')}) with no future extension. Dropping.", flush=True)
                    return False
            except ValueError:
                continue

    return True

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
        exclusion_rule = f"""
CRITICAL CONTEXTUAL EXCLUSIONS:
Banned Intents/Keywords: {json.dumps(exclusions)}
- If the primary intent of the organization/lead is to procure or offer these EXACT [Banned Keywords], REJECT THEM (is_valid=False).
"""

prompt = f"""
You are an expert B2B Procurement and Lead Qualification Analyst.
CRITICAL TEMPORAL CONTEXT: Today's date is {CURRENT_DATE_STR}.

ORGANIZATION EXTRACTION RULES (STRICT):
1. 'org': You MUST extract the ACTUAL buying department, ministry, municipal body, PSU, or enterprise (e.g. 'Military Engineer Services', 'NTPC Limited', 'CPWD', 'Tata Projects').
2. NEVER set 'org' to 'Government e-Marketplace', 'GeM', 'eProcure', or 'TenderTiger'. Those are procurement portals/exchanges, NOT the buyer. Look at the buyer/consignee block in the text. If unknown, output 'Unknown Ministry/PSU'.

DEADLINE & ACTIVE BID ENFORCEMENT:
1. If the text mentions an expired Bid End Date, Submission Closing Date, or Due Date before {CURRENT_DATE_STR}, return is_valid=False.
2. If the status is 'Closed', 'Awarded', 'Cancelled', or 'Technical Evaluation Completed', return is_valid=False.

UNIQUE FINGERPRINT EXTRACTION:
1. 'ref_id': Extract any official Bid Number, Tender ID, GeM ID, RERA Project ID, EC File No, or CIN (e.g., 'GEM/2026/B/89123', 'NIT-45/2026', 'U72900KA2026PTC123456'). If not found, return 'N/A'.
2. 'project_scope_key': If 'ref_id' is 'N/A', generate a lowercase 3-to-5 word hyphen-separated slug of the specific project, site, and work scope (e.g., 'mumbai-metro-line-4-signaling'). DO NOT use generic phrases like 'buying-product'.

CLASSIFICATION ROLES:
1. 'BUYER': Active direct procurement, live tender, or live RFP.
2. 'PROJECT_BUYER': Capex, Environmental Clearances, Factory Setups, Land Allotments, RERA projects.
3. 'SERVICE_USER': Company offering commercial services using the Target Product.
4. 'SELLER': Company manufacturing/supplying the Target Product.
5. 'IRRELEVANT': Expired bids, unrelated products, or generic news.

BUYER SEGMENT CLASSIFICATION:
- 'GOVT': Central/State Government, GeM, PSUs, Defense.
- 'CORPORATE': Private/Public Limited enterprises.
- 'LOCAL_MSME': Small businesses, contractors.

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
                "product_match_reasoning": {"type": "STRING"},
                "is_valid": {"type": "BOOLEAN"},
                "confidence_score": {"type": "STRING"},
                "entity_role": {"type": "STRING"},
                "buyer_segment": {"type": "STRING"},
                "org": {"type": "STRING"},
                "city": {"type": "STRING"},
                "state": {"type": "STRING"},
                "industry": {"type": "STRING"},
                "intent_summary": {"type": "STRING"},
                "posted_date": {"type": "STRING"},
                "ref_id": {"type": "STRING", "description": "Unique Tender ID, Bid Number, or CIN. 'N/A' if not present."},
                "project_scope_key": {"type": "STRING", "description": "Hyphenated slug of specific project scope."},
                "dm_name": {"type": "STRING", "nullable": True},
                "dm_title": {"type": "STRING", "nullable": True}
            },
            "required": ["item_index", "product_match_reasoning", "is_valid", "confidence_score", "entity_role", "buyer_segment", "org", "city", "state", "industry", "intent_summary", "posted_date", "ref_id", "project_scope_key"]
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

    tier1_direct_tenders = [
        {"type": "Direct", "query": f'"{target}" site:bidplus.gem.gov.in{exclusions}'},
        {"type": "Direct", "query": f'"{target}" "Tender Documents" (site:eprocure.gov.in OR site:etenders.gov.in){exclusions}'},
        {"type": "Direct", "query": f'"{target}" tender (site:mahatenders.gov.in OR site:wbtenders.gov.in OR site:etenders.kerala.gov.in OR site:eproc.rajasthan.gov.in OR site:tender.up.gov.in){exclusions}'},
        {"type": "Direct", "query": f'"{target}" tender OR RFQ site:gov.in{exclusions}'},
        {"type": "Direct", "query": f'"{target}" site:zauba.com/import-'}
    ]

    tier2_gem_defense = [
        {"type": "Direct", "query": f'"{target}" "Bid Details" "Total Quantity" site:bidplus.gem.gov.in{exclusions}'},
        {"type": "Direct", "query": f'"{target}" "Custom Bid for Services" site:gem.gov.in{exclusions}'},
        {"type": "Direct", "query": f'"{target}" ("Tender Notice" OR "Notice Inviting Tender") (site:isro.gov.in OR site:drdo.gov.in OR site:cpwd.gov.in){exclusions}'},
        {"type": "Project", "query": f'"{target}" ("winner" OR "grant approved" OR "contract signed") site:idex.gov.in'},
        {"type": "Direct", "query": f'"{target}" ("vendor registration" OR "expression of interest") (site:hal-india.co.in OR site:bel-india.in OR site:bdl-india.in)'}
    ]

    tier3_statutory = [
        {"type": "Project", "query": f'"{target}" (site:parivesh.nic.in OR site:environmentclearance.nic.in){exclusions}'},
        {"type": "Project", "query": f'"{target}" "Consent to Establish" (site:mpcb.gov.in OR site:gpcb.gujarat.gov.in OR site:uppcb.com)'},
        {"type": "Project", "query": f'"{target}" "project cost" (site:maharera.mahaonline.gov.in OR site:up-rera.in OR site:rera.karnataka.gov.in)'},
        {"type": "Project", "query": f'"{target}" "IEM acknowledged" site:dpiit.gov.in'},
        {"type": "Project", "query": f'"{target}" "project cost" site:indiainvestmentgrid.gov.in'}
    ]

    tier4_corridors = [
        {"type": "Project", "query": f'"{target}" "allotment" (site:midcindia.org OR site:gidc.gujarat.gov.in OR site:onlineupsida.com)'},
        {"type": "Project", "query": f'"{target}" ("plot allotment" OR "possession letter" OR "building plan approved") (site:yamunaexpresswayauthority.com OR site:dholera.go.gov.in OR site:kiadb.in OR site:sipcot.tn.gov.in)'},
        {"type": "Project", "query": f'"{target}" ("allotment of industrial land" OR "ground breaking") (Tamil Nadu OR Karnataka OR Uttar Pradesh) {current_year}'}
    ]

    tier5_epc = [
        {"type": "Project", "query": f'"{target}" ("Vendor Empanelment" OR "Expression of Interest" OR "Notice Inviting EOI") (site:larsentoubro.com OR site:tataprojects.com OR site:afcons.com OR site:ncc.co.in)'},
        {"type": "Project", "query": f'"{target}" ("sub-contractor required" OR "sub-package" OR "invited for empanelment") India {current_year}'}
    ]

    tier6_mdbs = [
        {"type": "Direct", "query": f'"{target}" "Procurement Notice" India (site:projects.worldbank.org OR site:adb.org OR site:aiib.org)'},
        {"type": "Direct", "query": f'"{target}" "General Procurement Notice" (site:dgmarket.com OR site:devbusiness.com) India'}
    ]

    tier7_capex = [
        {"type": "Project", "query": f'"{target}" ("concall transcript" OR "earnings conference call") "capex" (site:trendlyne.com OR site:researchbytes.com OR site:screener.in) India'},
        {"type": "Project", "query": f'"{target}" "investor presentation" ("capacity addition" OR "new facility" OR "capital outlay") India {current_year}'},
        {"type": "Project", "query": f'"{target}" ("Regulation 30" OR "outcome of board meeting") "capex" (site:bseindia.com OR site:nseindia.com)'},
        {"type": "Project", "query": f'"{target}" "rating rationale" ("enhancement in capacity" OR "capex plan") (site:crisilratings.com OR site:icra.in OR site:careratings.com OR site:infomerics.com)'},
        {"type": "Project", "query": f'"{target}" ("capacity expansion" OR "modernization" OR "brownfield") India {current_year}'}
    ]

    tier8_growth_private = [
        {"type": "Project", "query": f'site:naukri.com/job-listings "{target}" ("urgent opening" OR "walk-in") India'},
        {"type": "Direct", "query": f'"{target}" ("raised" OR "funding" OR "seed" OR "series") (site:yourstory.com OR site:entrackr.com)'},
        {"type": "Direct", "query": f'"{target}" ("buying requirement" OR "urgent order") (site:connect2india.com OR site:exportersindia.com)'},
        {"type": "Direct", "query": f'"{target}" ("need agency" OR "looking for agency" OR "hiring") India (site:upwork.com OR site:freelancer.in)'},
        {"type": "Project", "query": f'"{target}" ("exhibitor list" OR "participating in" OR "stall booked") India {current_year}'},
        {"type": "Direct", "query": f'"{target}" buyer requirement site:indiamart.com OR site:tradeindia.com'},
        {"type": "Direct", "query": f'"{target}" ("authorized dealer" OR "stockist") "contact number" site:justdial.com'},
        {"type": "Direct", "query": f'"{target}" "looking for vendors" site:linkedin.com/posts'},
        {"type": "Direct", "query": f'site:facebook.com/groups "{target}" ("urgent requirement" OR "need supplier" OR "vendor needed") India'},
        {"type": "MCA", "query": f'site:zaubacorp.com "Date of Incorporation" "{current_year}" ({industry_keywords})'},
        {"type": "MCA", "query": f'site:thecompanycheck.com "Incorporation Date" "{current_year}" ({industry_keywords})'},
        {"type": "Project", "query": f'"{target}" ("Letter of Award" OR "awarded contract" OR "lowest bidder") India {current_year}'},
        {"type": "Project", "query": f'"{target}" ("MoU signed" OR "groundbreaking ceremony" OR "new plant") India'}
    ]

    return tier1_direct_tenders + tier2_gem_defense + tier3_statutory + tier4_corridors + tier5_epc + tier6_mdbs + tier7_capex + tier8_growth_private

def run():
    print(">>> 📡 RADAR SCOUT ACTIVE (V15 Master Engine with Smart Deduplication)", flush=True)
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
        
        print(f"\n[*] Scanning: {query} (Target: {target_product})", flush=True)
        results = get_search_results(query)
        fresh_leads = []
        
        for r in results:
            link_lower = r['link'].lower()
            if any(b_dom in link_lower for b_dom in cloud_domains):
                continue
            if not is_duplicate_cached(r['link']):
                raw_deep_text = fetch_deep_text(r['link'])
                
                is_tender_source = any(k in link_lower for k in ["gem.gov.in", "eprocure.gov.in", "tender", "bidplus", "etenders"]) or query_type == "Direct"
                is_registry = any(k in link_lower for k in ["zaubacorp.com", "thecompanycheck.com", "linkedin.com", "indiamart.com", "tradeindia.com"]) or query_type == "MCA"

                if is_tender_source and not is_registry:
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
            
            # Generate the unique composite fingerprint
            lead_fp = make_lead_fingerprint(entity, target_product)
            now = time.time()
            
            # Check for existing fingerprint within 30-day window
            if lead_fp in EXISTING_FINGERPRINTS_CACHE:
                last_seen = EXISTING_FINGERPRINTS_CACHE[lead_fp]
                if now - last_seen < FINGERPRINT_TTL_SECONDS:
                    print(f"    🔁 Duplicate Lead Detected ({lead_fp}). Skipping.", flush=True)
                    add_to_cache(fresh_leads[idx]['link'])  # Don't inspect this URL again
                    continue
            
            is_valid = entity.get("is_valid")
            role = entity.get('entity_role')
            segment = entity.get('buyer_segment', 'CORPORATE')
            confidence = entity.get('confidence_score', 'HIGH')
            reason = entity.get("product_match_reasoning", "No reasoning provided")
            link_url = fresh_leads[idx]['link'].lower()
            
            is_supplier = (role == "SELLER")
            is_mca_registry = "zaubacorp.com" in link_url or "thecompanycheck.com" in link_url or query_type == "MCA"
            is_fresh_incorporation = is_mca_registry and str(TODAY.year) in str(entity.get("posted_date", ""))

            print(f"       [Vote] Valid: {is_valid} | Role: {role} | Conf: {confidence} | Org: {entity.get('org')}", flush=True)
            
            needs_review = (role in ["BUYER", "PROJECT_BUYER"] and confidence in ["LOW", "MEDIUM"])

            if is_valid and role in ["BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER"]:
                if needs_review:
                    target_sheet, source_tag = "Needs Review", f"Review-{segment}"
                elif is_fresh_incorporation:
                    target_sheet, source_tag = "MCA", "MCA-Registry"
                elif role == "PROJECT_BUYER" or (is_mca_registry and not is_fresh_incorporation): 
                    target_sheet, source_tag = "Projects & MOUs", "Enterprise-Capex"
                elif role == "BUYER": 
                    target_sheet, source_tag = "Inbox", f"{segment}-Buyer"
                elif role == "SERVICE_USER": 
                    target_sheet, source_tag = "Services", "Service-Radar"
                elif is_supplier: 
                    target_sheet, source_tag = "Suppliers", "Supplier-Radar"
                else: 
                    target_sheet, source_tag = "Inbox", "Radar Scout"
                
                intent_base = entity.get("intent_summary") or "Identified Requirement"
                intent_label = f"[{segment}] Supplier ({intent_base})" if is_supplier else f"[{segment}] {intent_base}"
                
                if needs_review:
                    intent_label = f"[LOW CONFIDENCE] {intent_label}"
                
                payload = {
                    "secret": SECRET,
                    "action": "add_lead",
                    "target_sheet": target_sheet,
                    "is_supplier": is_supplier,
                    "lead_id": str(uuid.uuid4())[:8],
                    "fingerprint": lead_fp,  # Sending the fingerprint to Apps Script
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "posted_date": entity.get("posted_date", "N/A"), 
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
                        add_to_cache(fresh_leads[idx]['link'], lead_fp)
                        print(f"    ✅ [{role}] -> {target_sheet}: {entity['org']} (FP: {lead_fp})", flush=True)
                        break
                    except Exception: time.sleep(2)
                    
            elif not is_valid and needs_review:
                target_sheet, source_tag = "Needs Review", f"LowConfidence-{segment}"
                intent_base = entity.get("intent_summary") or "Marginal Intent Detected"
                intent_label = f"[AI REJECTED - REVIEW] [{segment}] {intent_base}"
                
                payload = {
                    "secret": SECRET,
                    "action": "add_lead",
                    "target_sheet": target_sheet,
                    "is_supplier": False,
                    "lead_id": str(uuid.uuid4())[:8],
                    "fingerprint": lead_fp,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "posted_date": entity.get("posted_date", "N/A"), 
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
                        add_to_cache(fresh_leads[idx]['link'], lead_fp)
                        print(f"    ⚠️️ [SAVED FROM TRASH] -> {target_sheet}: {entity['org']}", flush=True)
                        break
                    except Exception: time.sleep(2)
            else:
                ai_trash_log.append({"url": fresh_leads[idx]['link'], "reason": f"[{role}] {reason}"})
                add_to_cache(fresh_leads[idx]['link'])
        
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
                    print(f"    🗑 Swept {len(ai_trash_log)} rejected links into AI_Trash.", flush=True)
                    break
                except Exception: time.sleep(2)
        time.sleep(4)

if __name__ == "__main__":
    run()
