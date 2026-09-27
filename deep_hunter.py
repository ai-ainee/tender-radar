import os
import csv
import time
import re
import json
import requests
import urllib.parse
from bs4 import BeautifulSoup

# Search providers
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

try:
    from googlesearch import search as google_organic_search
except ImportError:
    google_organic_search = None

# Google GenAI SDK
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None

CSV_URL = os.environ.get("QUALIFIED_CSV_URL")
WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

def get_best_gemini_model(client):
    try:
        models = [m.name for m in client.models.list() if 'flash' in m.name and 'generateContent' in m.supported_generation_methods]
        if models:
            models.sort(reverse=True)
            return models[0]
    except Exception:
        pass
    return "models/gemini-2.0-flash"

def get_rich_search_results(query, max_res=5):
    """Fetches links, titles, AND text snippets so the AI can read the context."""
    results = []
    
    # Attempt 1: DuckDuckGo (Provides snippets natively)
    if DDGS:
        try:
            ddgs = DDGS()
            res = list(ddgs.text(query, max_results=max_res, backend="lite"))
            for r in res:
                results.append({
                    "title": r.get("title", ""),
                    "link": r.get("href", ""),
                    "snippet": r.get("body", "")
                })
            if results:
                return results
        except Exception:
            pass

    # Attempt 2: Google Organic (Slower, less snippet data, but reliable fallback)
    if google_organic_search:
        try:
            time.sleep(2)
            # Advanced googlesearch can sometimes yield descriptions, otherwise we just pass titles
            urls = list(google_organic_search(query, num=max_res, stop=max_res, pause=2, advanced=True))
            for u in urls:
                results.append({
                    "title": getattr(u, 'title', u.url),
                    "link": u.url,
                    "snippet": getattr(u, 'description', '')
                })
            return results
        except Exception:
            pass
            
    return results

def ai_entity_resolution(org_name, web_results, linkedin_results):
    """
    The Core Engine. Feeds all search data to Gemini to logically deduce the 
    real website and real human decision-maker, eliminating false positives.
    """
    if not GEMINI_KEY or not genai:
        return None

    try:
        client = genai.Client(api_key=GEMINI_KEY)
        model_name = get_best_gemini_model(client)
        
        prompt = f"""
You are an elite B2B Data Verification AI. 
Target Organization: "{org_name}"

Task 1: Identify the OFFICIAL corporate website from 'Web Results'.
- MUST NOT be a directory (IndiaMart, JustDial), social media, forum (GameFAQs, Reddit), or news site.
- If no result genuinely looks like the official B2B/Corporate site for this exact org, return "Not Listed".

Task 2: Identify the DECISION MAKER from 'LinkedIn Results'.
- MUST be a human profile (linkedin.com/in/), NOT a company page (linkedin.com/company/).
- Ensure the snippet proves they currently work at "{org_name}".
- Extract their Name and exact Job Title. If uncertain, return "Not Listed".

Web Results (Evaluate carefully):
{json.dumps(web_results, indent=2)}

LinkedIn Results (Evaluate carefully):
{json.dumps(linkedin_results, indent=2)}

Return ONLY valid JSON:
{{
  "verified_website": "URL or 'Not Listed'",
  "dm_name": "Extracted Name or 'Not Listed'",
  "dm_title": "Extracted Title or 'Not Listed'",
  "linkedin_url": "URL or 'Not Listed'"
}}
"""
        res = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
        )
        return json.loads(res.text)
    except Exception as e:
        print(f"    ⚠️ [AI Resolution Failed]: {e}")
        return None

def extract_contacts_from_web(url):
    """Crawls website and uses regex to find potential contacts."""
    emails, phones = set(), set()
    try:
        # Check Homepage and Contact page
        for path in ["", "/contact", "/contact-us"]:
            target_url = urllib.parse.urljoin(url, path)
            r = SESSION.get(target_url, timeout=8)
            if r.status_code == 200:
                text = r.text
                found_e = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
                found_p = re.findall(r"(?:\+91[- ]?|0)?[6-9]\d{9}\b", text)
                
                for e in found_e:
                    if not e.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".webp", ".svg")):
                        emails.add(e)
                for p in found_p:
                    if len(p.replace("+91", "").strip()) >= 10:
                        phones.add(p)
            time.sleep(1)
    except Exception:
        pass
    return list(emails), list(phones)

def hunt():
    if not CSV_URL or not WEBHOOK:
        print("❌ Missing QUALIFIED_CSV_URL or GOOGLE_SHEET_WEBHOOK.")
        return

    print(">>> 🕵️‍♂️ INITIATING AI-DRIVEN DEEP HUNTER SEQUENCE...")
    try:
        r = SESSION.get(CSV_URL, timeout=15)
        r.raise_for_status()
        reader = csv.DictReader(r.text.splitlines())
    except Exception as e:
        print(f"❌ Error fetching Live CSV: {e}")
        return

    for row in reader:
        lead_id = row.get("Lead ID", "").strip()
        if not lead_id: continue
        
        org = row.get("Organization", "").strip()
        email = row.get("Email", "").strip()
        phone = row.get("Phone", "").strip()
        dm_name = row.get("Decision Maker", "").strip()
        website = row.get("Website", "").strip()

        needs_enrich = (
            email in ["N/A", "Not Listed", ""] or "⚠️" in email or
            phone in ["N/A", "Not Listed", ""] or
            dm_name in ["N/A", "Not Listed", ""] or
            website in ["N/A", "Not Listed", ""]
        )

        if not needs_enrich:
            continue
            
        print(f"\n[*] Target Locked: {org}")
        
        new_web, new_dm, new_title, new_li, new_email, new_phone = website, dm_name, row.get("DM Title", "Not Listed"), row.get("DM LinkedIn", "N/A"), email, phone

        # 1. GATHER CONTEXT (Rich Search)
        web_results = []
        if new_web in ["N/A", "Not Listed", ""]:
            web_results = get_rich_search_results(f'"{org}" official website india', 5)
            
        li_results = []
        if new_dm in ["N/A", "Not Listed", ""]:
            li_results = get_rich_search_results(f'"{org}" (Director OR Founder OR CEO OR "Managing Director" OR "Procurement") site:linkedin.com/in/', 5)

        # 2. AI ENTITY RESOLUTION
        if web_results or li_results:
            print("    -> Routing data through Gemini Entity Verification...")
            ai_data = ai_entity_resolution(org, web_results, li_results)
            
            if ai_data:
                if ai_data.get("verified_website") != "Not Listed":
                    new_web = ai_data["verified_website"]
                    print(f"    ✅ AI Verified Domain: {new_web}")
                
                if ai_data.get("dm_name") != "Not Listed":
                    new_dm = ai_data["dm_name"]
                    new_title = ai_data.get("dm_title", "Decision Maker")
                    new_li = ai_data.get("linkedin_url", "Not Listed")
                    print(f"    ✅ AI Verified DM: {new_dm} ({new_title})")

        # 3. DIRECT WEBSITE CRAWL FOR CONTACTS
        if new_web and new_web != "Not Listed" and (new_email in ["N/A", "Not Listed", ""] or new_phone in ["N/A", "Not Listed", ""]):
            print("    -> Crawling verified domain for contacts...")
            emails, phones = extract_contacts_from_web(new_web)
            
            # Simple heuristic: filter out obvious dummy emails, pick the first good one
            valid_emails = [e for e in emails if not any(j in e.lower() for j in ["example.com", "yourdomain", "sentry.io", "domain.com"])]
            
            if valid_emails and new_email in ["N/A", "Not Listed", ""]:
                new_email = valid_emails[0]
                print(f"    -> Extracted Email: {new_email}")
                
            if phones and new_phone in ["N/A", "Not Listed", ""]:
                new_phone = phones[0]
                print(f"    -> Extracted Phone: {new_phone}")

        # 4. PUSH TO CRM
        if new_dm == dm_name and new_email == email and new_phone == phone and new_web == website:
            print(f"    -> No fresh verified data found. Moving on.")
            continue
            
        payload = {
            "action": "deep_enrich",
            "lead_id": lead_id,
            "new_dm_name": new_dm,
            "new_dm_title": new_title,
            "new_linkedin": new_li,
            "new_email": new_email,
            "new_phone": new_phone,
            "new_website": new_web
        }
        
        try:
            res = SESSION.post(WEBHOOK, json=payload, timeout=10)
            if res.status_code == 200:
                print(f"    ✅ CRM Updated Successfully!")
            else:
                print(f"    ❌ CRM Update Failed. HTTP {res.status_code}")
        except Exception as e:
            print(f"    ❌ Webhook Error: {e}")
            
        time.sleep(3) # Respect API limits

if __name__ == "__main__":
    hunt()
