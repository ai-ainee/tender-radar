import os
import json
import hashlib
import requests
import urllib3
import re
import uuid
import time
import random
import logging
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse
from google import genai
from openai import OpenAI, OpenAIError
from tenacity import retry, wait_exponential, stop_after_attempt
from ddgs import DDGS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RadarScout")

logging.getLogger("google.genai.models").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("duckduckgo_search").setLevel(logging.ERROR)
logging.getLogger("ddgs").setLevel(logging.ERROR)
logging.getLogger("pypdf").setLevel(logging.ERROR)

class APIKeyManager:
    def __init__(self, env_string):
        clean_str = env_string.replace('"', '').replace("'", "").replace("\n", "").replace("\r", "").replace(" ", "")
        self.keys = [k for k in clean_str.split(',') if k]
        self.index = 0
        if not self.keys: raise ValueError("No API keys found. Check GitHub Secrets.")
    def get_current(self): return self.keys[self.index]
    def rotate(self, service_name):
        self.index = (self.index + 1) % len(self.keys)
        logger.warning(f"{service_name} Failover. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

class SystemAlertNotifier:
    def __init__(self):
        self.token, self.chat_id = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    def send(self, message):
        if not self.token or not self.chat_id: return
        try: requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
        except Exception: pass

serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", ""))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", ""))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_secret")
system_monitor = SystemAlertNotifier()

CACHE_FILE = "seen_links.txt"
if not os.path.exists(CACHE_FILE): open(CACHE_FILE, 'w').close()

def clean_url(url):
    try:
        p = urlparse(url.strip().lower())
        return f"{p.netloc.replace('www.', '')}{p.path.rstrip('/')}"
    except Exception: return url.strip().lower()

@retry(wait=wait_exponential(multiplier=2, min=4, max=10), stop=stop_after_attempt(3), reraise=True)
def load_hybrid_cache():
    seen = set()
    try:
        with open(CACHE_FILE, 'r') as f: seen.update(clean_url(line) for line in f if line.strip())
    except Exception as e: logger.warning(f"Local cache unreadable: {e}")
        
    logger.info("🔄 Fetching cloud cache (seen links) from Google Sheets...")
    res = requests.post(WEBHOOK_URL, json={"secret": WEBHOOK_SECRET, "action": "get_all_urls"}, timeout=20)
    res.raise_for_status() 
    data = res.json()
    if data.get("status") != "success": raise ValueError(f"Webhook error: {data.get('message', 'Unknown error')}")
    seen.update(clean_url(u) for u in data.get("urls", []) if u.strip())
    logger.info(f"✅ Cloud cache synchronized. Total memory: {len(seen)} links.")
    return seen

def save_to_cache(link):
    with open(CACHE_FILE, 'a') as f: f.write(link + '\n')

class QueryGenerator:
    def __init__(self, target, ind, country, loc, ai_client, ai_models):
        self.target = target.strip()
        self.ind = ind.strip() if ind and ind != "Unknown" else ""
        self.loc = loc.strip() if loc else ""
        self.country = country.strip() if country else "India"
        self.year = datetime.now().year
        self.client = ai_client
        self.models = ai_models

    def build_tracks(self):
        logger.info(f"🧠 Asking AI to invent custom search algorithms for: {self.target}...")
        prompt = f"""
        You are an elite OSINT and B2B Data Analyst. Generate exactly 2 Google Search queries per track to find B2B buyers for '{self.target}' in {self.country}:
        TRACK 1 (TENDERS): Government portals, RFPs. Use plain words (e.g., eprocure {self.target} tender).
        TRACK 2 (CAPEX_AND_PARTNERS): Target factory expansions. Use plain words (e.g., {self.target} case study OR {self.target} implementation).
        TRACK 3 (MCA): Corporate registrations. Use plain words (e.g., zaubacorp {self.ind} incorporation).
        TRACK 4 (COMMERCIAL): 
           - Query 1: Job aggregators hiring '{self.target}' skills. Use plain words (e.g., naukri {self.target} hiring).
           - Query 2: Direct corporate websites. Use plain words (e.g., {self.target} careers apply now).
        CRITICAL RULES: Max 15 words. Include "{self.country}" exactly in every query.
        NEVER use advanced operators like site:, intitle:, or minus signs (-). DO NOT use quotes ("") or parentheses (). Use plain text ONLY.
        Respond STRICTLY with a JSON object containing the 4 keys: TRACK_1_TENDERS, TRACK_2_CAPEX_AND_PARTNERS, TRACK_3_MCA, TRACK_4_COMMERCIAL.
        """
        for model_name in self.models:
            try:
                response = self.client.models.generate_content(model=model_name, contents=prompt, config={'response_mime_type': 'application/json'})
                if response.text: return json.loads(response.text.strip())
            except Exception: continue
        return self._fallback_tracks()

    def _fallback_tracks(self):
        yr = self.year
        return {
            "TRACK_1_TENDERS": [f'eprocure {self.target} tender {yr} {self.country}', f'gem {self.target} RFP {self.country}'],
            "TRACK_2_CAPEX_AND_PARTNERS": [f'{self.target} case study {self.country}', f'{self.target} capacity expansion {self.country}'],
            "TRACK_3_MCA": [f'zaubacorp {self.ind} incorporation {yr} {self.loc}'],
            "TRACK_4_COMMERCIAL": [f'naukri hiring {self.target} {yr} {self.country}', f'careers {self.target} {self.country} apply now']
        }

class DataEngine:
    def __init__(self, key_manager):
        self.serper_keys = key_manager
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
        ]

    def search(self, query, country_name="India"):
        clean_q = re.sub(r'(site:|intitle:|inurl:)\S+', '', str(query), flags=re.IGNORECASE)
        clean_q = re.sub(r'[-"()]', ' ', clean_q)
        clean_q = re.sub(r'\bOR\b', ' ', clean_q)
        clean_q = " ".join(clean_q.split())
        
        logger.info(f"🔍 Executing Query: {clean_q}")
        results = []
        gl_code = {"india": "in", "united states": "us", "uk": "gb", "uae": "ae"}.get(str(country_name).strip().lower(), "us")
        payload_dict = {"q": clean_q, "gl": gl_code, "num": 15}
        if "tender" in clean_q.lower() or "rfp" in clean_q.lower(): payload_dict["tbs"] = "qdr:m"

        for attempt in range(len(self.serper_keys.keys) or 1):
            current_key = self.serper_keys.get_current()
            if not current_key: break
            try:
                response = requests.post("https://google.serper.dev/search", headers={'X-API-KEY': current_key, 'Content-Type': 'application/json'}, json=payload_dict, timeout=15)
                if response.status_code != 200:
                    logger.error(f"❌ Serper API Rejected Key (Status {response.status_code}). Serper says: {response.text}")
                    self.serper_keys.rotate("Serper")
                    continue
                data = response.json()
                if "organic" in data:
                    for item in data["organic"]:
                        if item.get("link"): results.append({"link": item.get("link"), "snippet": item.get("snippet", "")})
                    if results: return results
                return results 
            except Exception as e:
                logger.error(f"❌ Serper Network Error: {e}")
                self.serper_keys.rotate("Serper")
                
        logger.error("❌ All Serper keys failed. Falling back to DuckDuckGo...")
        try:
            with DDGS() as ddgs:
                for item in ddgs.text(f"{clean_q} {country_name}", region='wt-wt', max_results=15): 
                    results.append({"link": item.get("href"), "snippet": item.get("body", "")})
            if results: logger.info("✅ Recovered using free DDGS Search.")
        except Exception as ddg_err: logger.error(f"🚨 DDGS Fallback failed: {ddg_err}")
        return results

    def fetch(self, url):
        time.sleep(random.uniform(1.5, 3.0))
        is_gov_or_protected = any(k in url.lower() for k in [".gov.in", ".nic.in", "zaubacorp", "gem.gov.in"])
        target_url = f"https://r.jina.ai/{url}" if is_gov_or_protected else url

        try:
            headers = {"User-Agent": random.choice(self.user_agents)}
            verify_ssl = False if '.gov.in' in url else True
            res = requests.get(target_url, headers=headers, timeout=18, verify=verify_ssl)
            if res.status_code != 200 and not is_gov_or_protected: res = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=18)
            res.raise_for_status()

            if 'application/pdf' in res.headers.get('Content-Type', '') or url.lower().endswith('.pdf'):
                return "".join(page.extract_text() + "\n" for page in PdfReader(BytesIO(res.content)).pages[:10]).strip()
                
            soup = BeautifulSoup(res.text, 'html.parser')
            for el in soup(["script", "style", "nav", "footer", "header", "aside"]): el.decompose()
            return "\n".join([line.strip() for line in soup.get_text(separator="\n", strip=True).splitlines() if line.strip()])[:8000]
        except Exception: return None

class BatchedSplitBrain:
    def __init__(self, key_manager):
        self.keys = key_manager
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        self.gemini_models = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
        self._init_client()

    def _init_client(self):
        self.client = genai.Client(api_key=self.keys.get_current())

    def rotate_key(self):
        new_key = self.keys.rotate("Gemini")
        self.client = genai.Client(api_key=new_key)
        return new_key

    def evaluate_batch(self, batch, target, ind, country, states, geo_rule, ban_rule):
        if not batch: return []
        items_block = "\n".join([f"--- ITEM {i} ---\nTRACK: {x['track']}\n<scraped_data>\n{x['raw_text'][:6000]}\n</scraped_data>\n" for i, x in enumerate(batch)])

        prompt = f"""
        Your Role: You are a ruthless, senior B2B sales strategist and research analyst.
        TARGET SOLUTION / PRODUCT: {target}
        INDUSTRY SEGMENT: {ind}
        {geo_rule}
        {ban_rule}

        STEP 1 - THE QUALIFICATION LENS:
        Before classifying, look for the problem the segment is solving, urgency drivers (Why now?), and target outcomes.

        STEP 2 - RESEARCH & EXTRACTION:
        Analyze the raw web scrapes. Identify real, verifiable companies matching our segment and extract their details. 
        PRIORITY SCORING CRITERIA:
        - HIGH: No confirmed usage of our specific {target} + active project pipeline/urgency + matches segment profile closely.
        - MEDIUM: Unknown product usage or partial match.
        - LOW: Confirmed existing user (upsell only) or highly incomplete data.

        STEP 3 - QUALITY CHECKLIST:
        - EVERY company must be real and verifiable. 
        - "Why Engage Now" MUST reference specific, concrete signals from the text — NO generic claims.
        - DIRECTORY HANDLING: If the text is a directory listing multiple companies, extract the single most prominent buyer actively seeking '{target}'.
        - TENDER EXPIRY RULE: If the scraped text is a government tender, RFP, or bid, check the deadline. If the submission closing date has passed relative to today, set `is_valid` to false.

        STEP 4 - OUTPUT FORMAT (STRICT JSON SCHEMA):
        Respond STRICTLY with a JSON object containing a single key "leads" which maps to an array of objects with these exact keys:
        "item_index": (integer) matches the input item,
        "is_valid": (boolean) true if a real company lead is found,
        "confidence": (string) "HIGH", "MEDIUM", or "LOW",
        "entity_role": (string) "BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER", or "IRRELEVANT",
        "organization": (string) Official trading name of the company,
        "city": (string), "state": (string),
        "why_engage_now": (string) 2-3 sentences: why this company is a compelling target right now,
        "product_usage": (string) Format: [Confirmed User / Unknown / Competitor] - [Cite Evidence],
        "solutions_to_push": (string) Specific use-cases of {target} that fit this profile,
        "upcoming_events": (string) Any trade shows, conferences, or deadlines (or 'Unknown')
        
        DATA BATCH:
        {items_block}
        """

        for model_name in self.gemini_models:
            for key_attempt in range(len(self.keys.keys) or 1):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config={'response_mime_type': 'application/json'}
                    )
                    if response.text:
                        data = json.loads(response.text.strip().replace("```json", "").replace("```", "").strip())
                        return data.get("leads", data) if isinstance(data, dict) else data
                except Exception as e:
                    error_str = str(e).lower()
                    if "429" in error_str or "quota" in error_str:
                        logger.warning(f"⏳ Gemini Rate Limit on ({model_name}). Rotating to next Gemini key...")
                        self.rotate_key()
                        time.sleep(1)
                        continue
                    elif "503" in error_str or "unavailable" in error_str:
                        logger.warning(f"⚠️ Gemini 503 Overloaded ({model_name}). Skipping model...")
                        break
                    elif "404" in error_str or "not_found" in error_str:
                        logger.warning(f"⚠️ Gemini 404 ({model_name}). Skipping model...")
                        break
                    else:
                        logger.warning(f"⚠️ Gemini Error ({model_name}): {e}")
                        time.sleep(2)

        if self.openai_key:
            try:
                response = OpenAI(api_key=self.openai_key).chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}
                )
                time.sleep(2) 
                data = json.loads(response.choices[0].message.content.strip())
                return data.get("leads", data) if isinstance(data, dict) else data
            except OpenAIError as e:
                logger.error(f"OpenAI Error: {e}")
        
        return []

class WebhookRouter:
    def __init__(self, url, secret):
        self.url, self.secret = url, secret

    def normalize_company(self, name):
        if not name or not isinstance(name, str): name = "Unknown"
        return re.sub(r'[^a-zA-Z0-9\s]', '', re.sub(r'(?i)\b(ltd|pvt|limited|private|inc|corp|llc)\b\.?', '', name)).strip().title()

    @retry(wait=wait_exponential(multiplier=2, min=4, max=10), stop=stop_after_attempt(3))
    def route_and_push(self, doc, ai_result, target_product):
        company_name = self.normalize_company(ai_result.get('organization', 'Unknown'))
        try:
            check = requests.post(self.url, json={"secret": self.secret, "action": "pre_flight_check", "company_name": company_name}, timeout=15).json()
            if check.get("exists") is True:
                logger.info(f"[-] Dropped Duplicate: {company_name}")
                return "DUPLICATE"
        except Exception: pass

        capture_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        is_valid = ai_result.get('is_valid', False)
        confidence = ai_result.get('confidence', 'LOW')
        role = ai_result.get('entity_role', 'IRRELEVANT')
        product_usage = str(ai_result.get('product_usage', 'Unknown')).strip()

        target_sheet = "🗑 AI_Trash" if not is_valid else ("🤝 Partners & Suppliers" if role == "SELLER" else ("⚠️ Needs Review" if confidence == "LOW" else "📥 Inbox"))
        
        lead_id = f"{str(uuid.uuid4())[:8].upper()}::{hashlib.md5(doc['url'].encode()).hexdigest()[:10]}"

        if target_sheet == "🗑 AI_Trash": 
            row_data = [capture_date, company_name, ai_result.get('why_engage_now', ''), doc['url'], doc['track'], ""]
        elif target_sheet == "🤝 Partners & Suppliers": 
            row_data = [capture_date, "Dealer", ai_result.get('state', ''), ai_result.get('city', ''), company_name, "", "", target_product]
        else: 
            # EXACT 13-COLUMN MAPPING
            row_data = [
                capture_date,                                   # Col 1: Capture Date
                ai_result.get('upcoming_events', 'Unknown'),    # Col 2: Deadline / Post Date
                role,                                           # Col 3: Signal Category
                ai_result.get('industry', 'AEC / Engineering'), # Col 4: Sector / Industry
                ai_result.get('state', 'N/A'),                  # Col 5: State
                ai_result.get('city', 'N/A'),                   # Col 6: City
                company_name,                                   # Col 7: Organization
                target_product,                                 # Col 8: Target Product
                ai_result.get('why_engage_now', ''),            # Col 9: AI Intent Brief (Pure pitch)
                doc['url'],                                     # Col 10: Source Link
                product_usage,                                  # Col 11: Product Usage / Status (Dedicated column)
                "",                                             # Col 12: Action / Move To (BLANK - DROPDOWN PRESERVED)
                lead_id                                         # Col 13: Lead ID & Fingerprint
            ]

        logger.info(f"[*] Routing {company_name} [{role}] -> {target_sheet}")
        requests.post(self.url, json={"secret": self.secret, "action": "insert_lead", "target_sheet": target_sheet, "company_name": company_name, "signal_brief": ai_result.get('why_engage_now', ''), "row_data": row_data}, timeout=15)
        return target_sheet

if __name__ == "__main__":
    logger.info("=== Waking Up: Radar Scout ===")
    start_time, leads_pushed, scanned_links = datetime.now(), 0, 0
    
    try: seen_links = load_hybrid_cache()
    except Exception as e:
        system_monitor.send(f"🚨 *Radar Scout Halted*: Critical Failure. Could not load seen_links cache.\nError: `{e}`")
        logger.error("System exit triggered to prevent duplicate scraping and quota drain."); exit(1)
        
    try:
        settings_req = requests.get(f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}&action=get_settings&cb={int(time.time())}", timeout=30).json()
        TARGETS = settings_req.get("target_products", [])
        if not TARGETS: raise ValueError("No target products defined in Column A.")
        
        INDUSTRIES, COUNTRIES, STATES = settings_req.get("industry_keywords", []), settings_req.get("target_countries", []), settings_req.get("target_states", [])
        BANNED_KW, BANNED_SITES, PROTECTED_DOMAINS = [k.lower() for k in settings_req.get("banned_keywords", [])], [s.lower() for s in settings_req.get("banned_websites", [])], [d.lower() for d in settings_req.get("protected_domains", [])]
        
        IND = INDUSTRIES[0] if INDUSTRIES else "Unknown"
        COUNTRY = COUNTRIES[0] if COUNTRIES else "India"
        LOC = STATES[0] if STATES else ""
        
        country_name = COUNTRIES[0] if COUNTRIES else "India"
        geo_rule = f"CRITICAL GEOGRAPHY CHECK: Target country is {country_name}. Multinational companies are 100% VALID if the text proves they have a physical office, active project, or are hiring INSIDE {country_name}. If they are ONLY located outside {country_name} with NO local operations, you MUST reject it by setting is_valid to false."
        ban_rule = f"Do not qualify domains: {', '.join(BANNED_SITES)}" if BANNED_SITES else ""
    except Exception as e: logger.error(f"Settings Error: {e}"); exit()

    engine = DataEngine(serper_keys)
    evaluator = BatchedSplitBrain(gemini_keys)
    router = WebhookRouter(WEBHOOK_URL, WEBHOOK_SECRET)
    
    for TARGET in TARGETS:
        session_companies = set()
        tracks = QueryGenerator(TARGET, IND, COUNTRY, LOC, evaluator.client, evaluator.gemini_models).build_tracks()

        for track_name, queries in tracks.items():
            docs_to_evaluate = []
            for query in queries:
                for res in engine.search(query, country_name=country_name):
                    link, snippet = res.get("link", "").lower(), res.get("snippet", "").lower()
                    c_link = clean_url(link)
                    
                    if not link or c_link in seen_links: continue
                    scanned_links += 1
                    
                    if not any(pd in link for pd in PROTECTED_DOMAINS if pd):
                        if any(bd in link for bd in BANNED_SITES if bd) or any(bx in snippet for bx in BANNED_KW if bx):
                            save_to_cache(c_link); seen_links.add(c_link); continue
                            
                    if "tender" in query and "TRACK_1" in track_name:
                        years = [int(y) for y in re.findall(r'\b(?:202[0-9])\b', f"{link} {snippet}")]
                        if years and max(years) < datetime.now().year - 1:
                            save_to_cache(c_link); seen_links.add(c_link); continue

                    save_to_cache(c_link); seen_links.add(c_link)
                    content = engine.fetch(link)
                    if content: docs_to_evaluate.append({"track": track_name, "url": link, "raw_text": content})
                        
            if docs_to_evaluate:
                for i in range(0, len(docs_to_evaluate), 2):
                    batch = docs_to_evaluate[i:i+2]
                    raw_verdicts = evaluator.evaluate_batch(batch, TARGET, IND, COUNTRY, LOC, geo_rule, ban_rule)
                    time.sleep(4) 
                    
                    if not raw_verdicts:
                        logger.warning(f"⚠️ Skipping batch due to total AI ecosystem failure.")
                        continue
                    
                    ai_verdicts = raw_verdicts if isinstance(raw_verdicts, list) else [raw_verdicts]
                    
                    for verdict in ai_verdicts:
                        idx = verdict.get("item_index")
                        if idx is not None and idx < len(batch):
                            comp_name = router.normalize_company(verdict.get('organization', ''))
                            if comp_name in session_companies:
                                logger.info(f"[-] Dropped In-Flight Duplicate: {comp_name}")
                                continue
                            
                            session_companies.add(comp_name)
                            if router.route_and_push(batch[idx], verdict, TARGET) in ["📥 Inbox", "⚠️ Needs Review"]: 
                                leads_pushed += 1

    system_monitor.send(f"🏁 *Radar Scout Complete*\n• Links Scanned: `{scanned_links}`\n• Sent to CRM: `{leads_pushed}`\n• Duration: `{str(datetime.now() - start_time).split('.')[0]}`")
