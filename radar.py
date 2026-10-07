import os
import json
import hashlib
import requests
import urllib3
import re
import uuid
import time
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO
from datetime import datetime
from google import genai
from google.genai import types

# Suppress SSL warnings for Indian Govt portals
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# MULTI-KEY AUTO-ROTATION MANAGER
# ==========================================
class APIKeyManager:
    def __init__(self, env_string):
        self.keys = [k.strip() for k in env_string.split(',') if k.strip()]
        self.index = 0
        if not self.keys:
            raise ValueError("No API keys found. Check your GitHub Secrets.")

    def get_current(self):
        return self.keys[self.index]

    def rotate(self, service_name):
        self.index = (self.index + 1) % len(self.keys)
        print(f"[!] {service_name} limit hit. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", ""))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", ""))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

CACHE_FILE = "seen_links.txt"
if not os.path.exists(CACHE_FILE):
    open(CACHE_FILE, 'w').close()

def get_cached_links():
    with open(CACHE_FILE, 'r') as f:
        return set(line.strip() for line in f)

def cache_link(link):
    with open(CACHE_FILE, 'a') as f:
        f.write(link + '\n')

# ==========================================
# 1. QUERY GENERATOR
# ==========================================
class QueryGenerator:
    def __init__(self, target_product, industry_keywords="", country="India", states=""):
        self.target = target_product
        self.industry = industry_keywords
        self.country = country
        self.states = states
        self.year = datetime.now().year

    def build_tracks(self):
        ind = self.industry if self.industry != "Unknown" else ""
        current_year = self.year
        location_kw = self.states if self.states else self.country
        
        return {
            "TRACK_1_TENDERS": [
                f'"{self.target}" ("{current_year}" OR "{current_year - 1}") tender OR RFP site:eprocure.gov.in',
                f'"{self.target}" "bid document" "{current_year}" site:gem.gov.in',
                f'"{self.target}" {ind} tender "{current_year}" site:mahatenders.gov.in'
            ],
            "TRACK_2_CAPEX": [
                f'"{self.target}" "environmental clearance" "{current_year}" site:environmentclearance.nic.in',
                f'"{self.target}" ("land allotment" OR "industrial area") "{current_year}" (MIDC OR GIDC OR SIPCOT)',
                f'"{self.target}" ("capacity expansion" OR "greenfield project") "{current_year}" "{location_kw}" filetype:pdf'
            ],
            "TRACK_3_MCA": [
                f'"{ind}" "Incorporation Date" "{current_year}" "{location_kw}" site:zaubacorp.com'
            ],
            "TRACK_4_COMMERCIAL": [
                f'hiring "CAD Draftsman" OR "{self.target} engineer" "{current_year}" site:naukri.com OR site:linkedin.com',
                f'"{self.target}" service provider OR consultant "{current_year}" "{location_kw}"'
            ]
        }

# ==========================================
# 2. SERPER HARVESTER
# ==========================================
class SerperHarvester:
    def __init__(self, key_manager):
        self.keys = key_manager

    def _execute_search(self, endpoint, payload):
        for _ in range(len(self.keys.keys)):
            headers = {
                'X-API-KEY': self.keys.get_current(),
                'Content-Type': 'application/json'
            }
            try:
                response = requests.post(endpoint, headers=headers, data=payload, timeout=10)
                if response.status_code in [403, 429]:
                    self.keys.rotate("Serper")
                    continue
                if response.status_code == 200:
                    return response.json()
            except Exception as e:
                print(f"[!] Serper connection issue: {e}")
            return {}
        print("[!] All Serper keys exhausted.")
        return {}

    def search_web(self, query):
        print(f"[*] Executing Search: {query}")
        payload = json.dumps({
            "q": query, 
            "num": 10, 
            "tbs": "qdr:m"
        })
        res = self._execute_search("https://google.serper.dev/search", payload)
        return res.get("organic", [])

# ==========================================
# 3. DEEP CONTENT SCRAPER
# ==========================================
class ContentScraper:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

    def fetch_content(self, url):
        try:
            response = requests.get(url, headers=self.headers, timeout=10, verify=False)
            response.raise_for_status()

            if 'application/pdf' in response.headers.get('Content-Type', '') or url.lower().endswith('.pdf'):
                return self._parse_pdf(response.content)
            return self._parse_html(response.text)
        except Exception as e:
            print(f"[!] Scraping failed for {url}: {e}")
            return None

    def _parse_pdf(self, pdf_bytes):
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            text = "".join(page.extract_text() + "\n" for page in reader.pages[:10])
            return text.strip()
        except Exception:
            return ""

    def _parse_html(self, html_text):
        soup = BeautifulSoup(html_text, 'html.parser')
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return " ".join(text.split())[:15000]

# ==========================================
# 4. SPLIT-BRAIN EVALUATOR
# ==========================================
class SplitBrainEvaluator:
    def __init__(self, key_manager):
        self.keys = key_manager

    def evaluate(self, doc, target_product, industry, country, states, banned_keywords):
        track = doc['track']
        raw_text = doc['raw_text']
        
        # Anti-blocker rescue for all vectors
        if len(raw_text.strip()) < 200:
            print(f"[!] Website blocked scraper or PDF failed. Rescuing to Needs Review.")
            return {
                "is_valid": True, "confidence": "LOW", "entity_role": "PROJECT_BUYER",
                "organization": "Unknown (Blocked by Website)", "city": "N/A", "state": "N/A", 
                "intent_brief": "Website blocked automated reading. Please click the link to review manually.",
                "deadline": "N/A", "reason": "SCRAPER_BLOCKED"
            }

        if track == "TRACK_3_MCA":
            return self._evaluate_mca(raw_text)

        prompt = self._get_prompt_for_track(track, target_product, industry, country, states, banned_keywords)
        full_prompt = f"{prompt}\n\nDOCUMENT TEXT:\n{raw_text[:15000]}"
        
        max_retries = 3
        for attempt in range(max_retries):
            for _ in range(len(self.keys.keys)):
                try:
                    client = genai.Client(api_key=self.keys.get_current())
                    print(f"[*] Sending to Gemini ({track}) [Attempt {attempt + 1}/{max_retries}]...")
                    
                    response = client.models.generate_content(
                        model='gemini-3.5-flash',
                        contents=full_prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                        ),
                    )
                    return json.loads(response.text)
                    
                except Exception as e:
                    error_str = str(e).lower()
                    if "429" in error_str or "quota" in error_str or "exhausted" in error_str:
                        self.keys.rotate("Gemini")
                    elif "503" in error_str or "unavailable" in error_str or "500" in error_str:
                        wait_time = 3 * (attempt + 1)
                        print(f"[!] Gemini Server Busy (503). Waiting {wait_time}s and retrying...")
                        time.sleep(wait_time)
                        break # Break the key loop to trigger the outer retry loop
                    else:
                        print(f"[!] Gemini Error: {e}")
                        return {"is_valid": False, "confidence": "LOW", "reason": "AI_PARSE_ERROR"}
                        
        print("[!] All retries and keys exhausted for this document.")
        return {"is_valid": False, "confidence": "LOW", "reason": "ALL_GEMINI_KEYS_EXHAUSTED"}

    def _evaluate_mca(self, text):
        cin_match = re.search(r'[L|U]\d{5}[A-Z]{2}(\d{4})[A-Z]{3}\d{6}', text)
        if cin_match and cin_match.group(1) == str(datetime.now().year):
            return {
                "is_valid": True, "confidence": "HIGH", "entity_role": "NEW_INCORPORATION",
                "organization": "Unknown (Review Link)", "city": "Unknown", "state": "Unknown",
                "intent_brief": f"New Company incorporated in {datetime.now().year}. Verified via CIN.", "deadline": "N/A"
            }
        
        return {
            "is_valid": True, "confidence": "LOW", "entity_role": "PROJECT_BUYER",
            "organization": "Unknown (Review MCA Link)", "city": "Unknown", "state": "Unknown",
            "intent_brief": "Possible new incorporation, but CIN could not be auto-verified. Manual review required.",
            "deadline": "N/A", "reason": "CIN_NOT_FOUND_OR_OLD"
        }

    def _get_prompt_for_track(self, track, target_product, industry, country, states, banned_keywords):
        geo_rule = f"Target country is {country}."
        if states:
            geo_rule = f"""
            PREFERRED STATES: [{states}].
            STATE MISMATCH RULE:
            1. If the opportunity is a valid match but located in a DIFFERENT state (or state cannot be verified), DO NOT DISCARD IT.
            2. Set "is_valid": true, "confidence": "LOW", and "reason": "OUTSIDE_TARGET_STATE".
            3. NEVER set "is_valid": false solely due to a non-matching state.
            """

        ban_rule = ""
        if banned_keywords:
            ban_rule = f"EXCLUSION: Mark is_valid as false ONLY if the primary subject of the text is about these banned keywords: ({banned_keywords}). If a banned keyword is merely mentioned in passing, ignore it."

        context_rule = f"""
        SEMANTIC MATCHING RULE (CRITICAL): Do not be overly literal. The document DOES NOT need to explicitly contain the exact target word "{target_product}". 
        If the text heavily involves the broader "{industry}" sector, related workflows, associated services, or standard industry synonyms, you MUST treat it as a valid match. 
        DO NOT mark is_valid as false just because the specific product name is missing if the context implies its usage.
        """

        base_schema = f"""
        Respond STRICTLY in this JSON format:
        {{
            "is_valid": true/false, "confidence": "HIGH" or "LOW",
            "entity_role": "(BUYER, PROJECT_BUYER, SERVICE_USER, SELLER, IRRELEVANT)",
            "organization": "Name of Company", "city": "City Name or N/A",
            "state": "State Name or N/A", "intent_brief": "2 sentence summary",
            "deadline": "YYYY-MM-DD or N/A", "reason": "If false or low confidence, why?"
        }}
        
        {geo_rule}
        {ban_rule}
        {context_rule}
        
        CRITICAL RESCUE RULE: Only use "is_valid": false for guaranteed irrelevance, sellers/spam, or stale archives. If an actual buyer or commercial demand exists, always mark "is_valid": true.
        """
        
        if track == "TRACK_1_TENDERS":
            return f"You are a procurement analyst. Find bids related to {target_product} or the {industry} sector." + base_schema
        elif track == "TRACK_2_CAPEX":
            return f"You are an industrial analyst looking for {target_product} demand or {industry} expansion in land/factory reports. Role = PROJECT_BUYER." + base_schema
        elif track == "TRACK_4_COMMERCIAL":
            return f"You are a commercial analyst finding {target_product} usage or {industry} operations (hiring = SERVICE_USER, distributors = SELLER)." + base_schema
        else:
            return f"You are a general B2B analyst looking for {target_product} or {industry} opportunities." + base_schema

# ==========================================
# 5. FAILSAFE ROUTER & CRM PUSH
# ==========================================
class WebhookRouter:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def route_and_push(self, doc, ai_result, target_product):
        company_name = ai_result.get('organization', 'Unknown')
        
        try:
            check_req = requests.post(
                self.webhook_url, 
                json={"action": "pre_flight_check", "company_name": company_name}, 
                timeout=30
            )
            check_data = check_req.json()
            if check_data.get("status") in ["exists", "duplicate"]:
                print(f"[-] Dropping Duplicate: {company_name} already registered in CRM.")
                return
        except Exception:
            pass

        lead_id = str(uuid.uuid4())[:8].upper()
        capture_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        fingerprint = hashlib.md5(f"{doc['url']}".encode()).hexdigest()[:10]

        is_valid = ai_result.get('is_valid', False)
        confidence = ai_result.get('confidence', 'LOW')
        role = ai_result.get('entity_role', 'IRRELEVANT')
        reason = ai_result.get('reason', '')
        
        # ROUTING LOGIC
        if not is_valid: 
            target_sheet = "🗑️ AI_Trash"
        elif role == "SELLER": 
            target_sheet = "🤝 Partners & Suppliers"
        elif confidence == "LOW" or reason in ["OUTSIDE_TARGET_STATE", "SCRAPER_BLOCKED", "CIN_NOT_FOUND_OR_OLD"]: 
            target_sheet = "⚠️ Needs Review"
        else:
            target_sheet = "📥 Inbox"
            
        if target_sheet == "🗑️ AI_Trash":
            row_data = [capture_date, company_name, reason, doc['url'], doc['track'], ""]
        elif target_sheet == "🤝 Partners & Suppliers":
            row_data = [capture_date, "Dealer", ai_result.get('state', ''), ai_result.get('city', ''), company_name, "", "", target_product]
        else:
            row_data = [
                capture_date, ai_result.get('deadline', 'N/A'), role, "Unknown", 
                ai_result.get('state', 'N/A'), ai_result.get('city', 'N/A'), company_name, 
                target_product, ai_result.get('intent_brief', ''), doc['url'], "", f"{lead_id}::{fingerprint}"
            ]

        try:
            print(f"[*] Routing {company_name} to -> {target_sheet}")
            requests.post(self.webhook_url, json={
                "action": "insert_lead", "target_sheet": target_sheet,
                "company_name": company_name,
                "signal_brief": ai_result.get('intent_brief', ''), "row_data": row_data
            }, timeout=10)
        except Exception as e:
            print(f"[!] Failed to push to CRM: {e}")

# ==========================================
# 6. MASTER EXECUTION
# ==========================================
def fetch_dynamic_settings():
    print("[*] Fetching search parameters from Google Sheets...")
    try:
        response = requests.get(f"{WEBHOOK_URL}?action=get_settings", timeout=30).json()
        if response.get("status") == "success":
            return (
                response.get("target_product"), 
                response.get("industry_keywords"),
                response.get("target_country", "India"),
                response.get("target_states", ""),
                response.get("banned_keywords", ""),
                response.get("banned_websites", "")
            )
        else:
            print("[!] Settings tab is empty. Please fill Row 2 in Google Sheets.")
            return None, None, None, None, None, None
    except Exception as e:
        print(f"[!] Failed to connect to Google Sheets for settings: {e}")
        return None, None, None, None, None, None

if __name__ == "__main__":
    print("=== Waking Up: Radar Scout Harvester ===")
    
    TARGET_PRODUCT, INDUSTRY, COUNTRY, STATES, BANNED_KEYWORDS, BANNED_WEBSITES = fetch_dynamic_settings()
    
    if not TARGET_PRODUCT or TARGET_PRODUCT == "Unknown":
        print("[!] Halting execution. No Target Product defined in Google Sheets.")
        exit()

    print(f"[*] Active Target: {TARGET_PRODUCT}")
    print(f"[*] Preferred Territory: {COUNTRY} | {STATES if STATES else 'All States'}")
    
    banned_kw_list = [k.strip().lower() for k in str(BANNED_KEYWORDS).split(',')] if BANNED_KEYWORDS else []
    banned_site_list = [s.strip().lower() for s in str(BANNED_WEBSITES).split(',')] if BANNED_WEBSITES else []
    
    generator = QueryGenerator(TARGET_PRODUCT, INDUSTRY, COUNTRY, STATES)
    harvester = SerperHarvester(serper_keys)
    scraper = ContentScraper()
    evaluator = SplitBrainEvaluator(gemini_keys)
    router = WebhookRouter(WEBHOOK_URL)
    seen_links = get_cached_links()
    
    tracks = generator.build_tracks()

    for track_name, queries in tracks.items():
        print(f"\n=== Initiating {track_name} ===")
        for query in queries:
            results = harvester.search_web(query)
            for res in results:
                link = res.get("link")
                snippet = res.get("snippet", "")
                
                if not link or link in seen_links: 
                    continue
                
                link_lower = link.lower()
                snippet_lower = snippet.lower()

                # --- PRE-SCRAPE BANNED WEBSITE GUARD ---
                if any(b_site in link_lower for b_site in banned_site_list if b_site):
                    print(f"[-] Dropping banned website: {link}")
                    cache_link(link)
                    seen_links.add(link)
                    continue
                    
                # --- PRE-SCRAPE BANNED KEYWORD GUARD ---
                if any(b_kw in link_lower or b_kw in snippet_lower for b_kw in banned_kw_list if b_kw):
                    print(f"[-] Dropping link due to banned keyword in snippet: {link}")
                    cache_link(link)
                    seen_links.add(link)
                    continue

                # --- PRE-SCRAPE DATE GUARD ---
                current_yr = datetime.now().year
                combined_text = f"{link} {snippet}"
                found_years = [int(y) for y in re.findall(r'\b(?:19|20)\d{2}\b', combined_text)]
                
                if found_years:
                    max_year = max(found_years)
                    if max_year < current_yr - 1:
                        print(f"[-] Dropping stale link (Most recent year is {max_year}): {link}")
                        cache_link(link)
                        seen_links.add(link)
                        continue

                cache_link(link)
                seen_links.add(link)

                print(f"[+] Scraping: {link}")
                content = scraper.fetch_content(link)
                if content:
                    doc = {"track": track_name, "url": link, "raw_text": content}
                    # Passing INDUSTRY explicitly so Gemini learns the context synonyms
                    ai_verdict = evaluator.evaluate(doc, TARGET_PRODUCT, INDUSTRY, COUNTRY, STATES, BANNED_KEYWORDS)
                    router.route_and_push(doc, ai_verdict, TARGET_PRODUCT)

    print("\n[✓] Radar Scout Cycle Complete.")
