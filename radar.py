import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from google import genai
from google.genai.errors import APIError
import requests

print(">>> TENDER RADAR ENGINE ONLINE")

# ---------------------------------------------------------------------------
# 1. Environment Secrets
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Regex pre-filter to catch real procurement intent without spending API quota
PROCUREMENT_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|license|licenses|subscription|renewal|contract|railway|metro|cpwd|drdo|iit|nit|psu)\b",
    re.IGNORECASE,
)


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        print(f"CRITICAL: {PRODUCTS_FILE} not found!")
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")


def push_to_google_sheet(product, ltype, org, contact, email, phone, summary, link):
    if not GOOGLE_SHEET_WEBHOOK:
        print("  [Google Sheet Webhook not configured]")
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "product": product,
        "type": ltype,
        "org": org,
        "contact_person": contact,
        "email": email,
        "phone": phone,
        "summary": summary,
        "link": link,
    }
    try:
        res = requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=15)
        print(f"  -> Google Sheet updated: Status {res.status_code}")
    except Exception as e:
        print(f"  -> Google Sheet error: {e}")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram credentials not configured]")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        res = requests.post(url, json=payload, timeout=12)
        print(f"  -> Telegram alert sent: Status {res.status_code}")
    except Exception as e:
        print(f"  -> Telegram error: {e}")


def fetch_opportunities(product):
    """Fetches high-intent procurement items directly via targeted search queries."""
    queries = [
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR tender OR RFP OR "NIT") India',
        f'{product} ("procurement of software" OR "license renewal" OR "annual subscription") India',
    ]

    items = []
    seen_in_run = set()

    for q in queries:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    link = item.findtext("link", "").strip()
                    title = item.findtext("title", "").strip()
                    desc = item.findtext("description", "").strip()
                    if link and title and link not in seen_in_run:
                        seen_in_run.add(link)
                        items.append({
                            "title": title,
                            "link": link,
                            "summary": desc,
                            "product": product,
                        })
        except Exception as e:
            print(f"Fetch error for {product}: {e}")

    return items[:10]


def evaluate_batch_single_request(client, candidate_items):
    """
    Evaluates ALL gathered candidates in ONE single Gemini API request.
    This ensures you consume only 1 API credit per scheduled run, completely
    preventing 429 Daily / RPM Quota exhaustion.
    """
    items_block = ""
    for idx, it in enumerate(candidate_items):
        clean_title = it['title'].replace('"', "'")
        clean_desc = re.sub(r"<[^>]+>", " ", it['summary']).replace('"', "'")[:250]
        items_block += f"\n--- ITEM {idx} ---\nProduct: {it['product']}\nTitle: {clean_title}\nDetails: {clean_desc}\n"

    prompt = f"""
    You are an Indian government procurement and B2B tender classification engine.
    Evaluate the following search items:

    {items_block}

    For each item, determine if it represents an authentic Indian commercial opportunity:
    - Central/State Government, GeM, PSU, Metro Rail, Defense, CPWD tenders
    - University/IIT/NIT/Polytechnic CAD lab setups or software licensing
    - Corporate RFPs, software subscription tenders, vendor empanelment notices
    
    Reject:
    - Pure general news, software release hype, tutorials, stock earnings, or piracy

    Respond ONLY with a valid JSON array matching this exact schema:
    [
      {{
        "item_index": 0,
        "is_lead": true,
        "lead_type": "GeM Bid / Govt Tender / University Lab RFP / Corporate RFP",
        "org": "Exact Name of Department, PSU, Metro, or University",
        "contact_person": "Officer Name or Not Listed",
        "email": "Official Email or Not Listed",
        "phone": "Contact Phone or Not Listed",
        "summary": "1 concise sentence stating the scope of software, licenses, or project requirements"
      }}
    ]
    """

    delays = [25, 50]
    for attempt in range(len(delays) + 1):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
            )
            raw = (
                response.text.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            return json.loads(raw)
        except APIError as e:
            if e.code in (429, 503) and attempt < len(delays):
                wait = delays[attempt]
                print(f"  [AI Busy/Throttled {e.code}] Backing off for {wait}s...")
                time.sleep(wait)
            else:
                print(f"  [AI Call Failed]: {e}")
                return []
        except Exception as e:
            print(f"  [AI JSON Parse Error]: {e}")
            return []

    return []


def main():
    if not GEMINI_API_KEY:
        print("FATAL: Missing GEMINI_API_KEY secret.")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    if not products:
        print("No products configured in products.txt.")
        return

    print(f"Scanning products: {products}")

    # Phase 1: Collect & pre-filter candidates across all products locally (0 API cost)
    candidates_to_evaluate = []
    for prod in products:
        raw_items = fetch_opportunities(prod)
        for item in raw_items:
            link = item["link"]
            if link in seen:
                continue

            seen.add(link)
            save_seen(link)

            text_blob = f"{item['title']} {item['summary']}"
            if PROCUREMENT_PATTERNS.search(text_blob):
                candidates_to_evaluate.append(item)
            else:
                print(f"  [Local Pre-Filter Skipped]: {item['title'][:55]}...")

    print(f"\nTotal pre-filtered tender candidates across all products: {len(candidates_to_evaluate)}")

    if not candidates_to_evaluate:
        print("No candidate items to evaluate this cycle.")
        return

    # Phase 2: Send all items in 1 unified API call (Max 12 items to stay within context and quota)
    batch = candidates_to_evaluate[:12]
    print(f"Evaluating {len(batch)} items in a single Gemini request...")
    results = evaluate_batch_single_request(client, batch)

    leads_found = 0
    for res in results:
        idx = res.get("item_index")
        if idx is not None and idx < len(batch) and res.get("is_lead") is True:
            leads_found += 1
            item = batch[idx]
            prod = item["product"]
            org = res.get("org", "Government / Enterprise Buyer")
            ltype = res.get("lead_type", "Tender / Bid")
            contact = res.get("contact_person", "Not Listed")
            email = res.get("email", "Not Listed")
            phone = res.get("phone", "Not Listed")
            summary = res.get("summary", "Software requirement identified.")
            link = item["link"]

            print(f"\n>>> [CONFIRMED COMMERCIAL LEAD]: {item['title'][:70]}")
            print(f"    Buyer: {org} | Type: {ltype}")

            # 1. Update Google Sheet
            push_to_google_sheet(prod, ltype, org, contact, email, phone, summary, link)

            # 2. Dispatch Telegram Notification
            msg = (
                f"🚨 *New Indian Procurement Lead!*\n\n"
                f"📦 *Product:* {prod}\n"
                f"🏛 *Authority / Org:* {org}\n"
                f"📋 *Type:* {ltype}\n"
                f"👤 *Contact Person:* {contact}\n"
                f"📧 *Email:* {email}\n"
                f"📞 *Phone:* {phone}\n"
                f"📝 *Summary:* {summary}\n\n"
                f"🔗 [Open Procurement Notice]({link})"
            )
            send_telegram(msg)

    print(f"\n==========================================")
    print(f"Execution complete. Total leads logged: {leads_found}")


if __name__ == "__main__":
    main()
