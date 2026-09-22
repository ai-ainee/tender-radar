import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from google import genai
from google.genai.errors import APIError
import requests

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Regex to detect commercial & procurement terminology locally
PROCUREMENT_PATTERNS = re.compile(
    r"(tender|rfp|bid|bidding|gem|eprocure|procurement|supply|quotation|eoi|license|licence|subscription|renewal|order|contract)",
    re.IGNORECASE,
)


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
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
        print(f"  -> Sheet updated! Status: {res.status_code}")
    except Exception as e:
        print(f"  -> Sheet push error: {e}")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
        print(f"  -> Telegram sent! Status: {res.status_code}")
    except Exception as e:
        print(f"  -> Telegram error: {e}")


def fetch_opportunities(product):
    """
    Searches both targeted Indian procurement queries and Google News feeds
    to collect actual tender notices, GeM bids, and enterprise licensing RFPs.
    """
    queries = [
        f'{product} (tender OR "bid" OR "eprocure" OR "gem.gov.in" OR "procurement")',
        f'"{product}" (licenses OR "subscription renewal" OR "RFP" OR "NIT")',
    ]

    all_items = []
    seen_urls = set()

    for q in queries:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    link = item.findtext("link", "")
                    title = item.findtext("title", "")
                    desc = item.findtext("description", "")
                    if link and title and link not in seen_urls:
                        seen_urls.add(link)
                        all_items.append({"title": title, "link": link, "summary": desc})
        except Exception as e:
            print(f"Fetch error on query '{q}': {e}")

    return all_items[:15]


def batch_analyze_with_ai(client, product, batch):
    """Evaluates candidate items using gemini-3.6-flash with clear parsing rules."""
    items_text = ""
    for idx, item in enumerate(batch):
        items_text += f"\n--- ITEM {idx} ---\nTitle: {item['title']}\nSnippet: {item['summary']}\nLink: {item['link']}\n"

    prompt = f"""
    You are an Indian enterprise software sales and tender procurement intelligence agent.
    Evaluate the following items for commercial opportunities regarding the product: "{product}".

    {items_text}

    Mark "is_lead": true if the item represents ANY of the following in India:
    - Government tender, GeM bid, or e-procurement notice (CPPP, Railways, Defense, State Portals, Universities)
    - Corporate licensing requirement, software subscription renewal, or bulk RFP
    - Vendor empanelment or contract awarded for CAD/engineering software services
    - Tech adoption or infrastructure project that mandates CAD/BIM software deployment

    Extract contact person, email, or phone if present in title or snippet. If absent, set to "Not Listed".

    Reply ONLY with a raw JSON list matching this format:
    [
      {{
        "item_index": 0,
        "is_lead": true or false,
        "lead_type": "Tender / GeM Bid / License Procurement / Corporate RFP",
        "org": "Organization, PSU, or Authority Name",
        "contact_person": "Officer Name or Not Listed",
        "email": "Email or Not Listed",
        "phone": "Phone or Not Listed",
        "summary": "1 concise sentence summarizing the software requirements or procurement context",
        "rejection_reason": "Brief reason if is_lead is false, otherwise empty"
      }}
    ]
    """

    delays = [5, 15, 30]
    for attempt, wait_time in enumerate(delays):
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
            if e.code in (429, 503):
                print(f"  [AI Throttled ({e.code})] Waiting {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f"  [AI API Error]: {e}")
                return []
        except Exception as e:
            print(f"  [AI Parse Error]: {e}")
            return []

    return []


def main():
    if not GEMINI_API_KEY:
        print("Missing GEMINI_API_KEY secret.")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    print(f"Monitoring Products: {products}")
    total_leads = 0

    for prod in products:
        print(f"\n==========================================")
        print(f"Scanning for: {prod}")
        entries = fetch_opportunities(prod)
        print(f"Found {len(entries)} candidate items on web.")

        to_evaluate = []
        for entry in entries:
            link = entry["link"]
            text_blob = f"{entry['title']} {entry['summary']}"

            if link in seen:
                continue

            seen.add(link)
            save_seen(link)

            # Local check
            if PROCUREMENT_PATTERNS.search(text_blob):
                to_evaluate.append(entry)
            else:
                print(f"  [Skipped Local Filter - No Tender Terms]: {entry['title'][:55]}...")

        if not to_evaluate:
            print(f"No new tender candidates to check for {prod}.")
            continue

        print(f"Evaluating {len(to_evaluate)} pre-qualified items with Gemini...")

        chunk_size = 5
        for i in range(0, len(to_evaluate), chunk_size):
            chunk = to_evaluate[i : i + chunk_size]
            results = batch_analyze_with_ai(client, prod, chunk)

            for res in results:
                idx = res.get("item_index", 0)
                if idx < len(chunk):
                    item = chunk[idx]
                    if res.get("is_lead") is True:
                        total_leads += 1
                        org = res.get("org", "Govt / Corporate Buyer")
                        ltype = res.get("lead_type", "Software Procurement")
                        contact = res.get("contact_person", "Not Listed")
                        email = res.get("email", "Not Listed")
                        phone = res.get("phone", "Not Listed")
                        lead_summary = res.get("summary", "Procurement identified.")

                        print(f"\n>>> [LEAD APPROVED]: {item['title'][:70]}")
                        print(f"    Buyer: {org} | Type: {ltype}")

                        push_to_google_sheet(
                            prod, ltype, org, contact, email, phone, lead_summary, item["link"]
                        )

                        msg = (
                            f"🚨 *New Indian Commercial Lead!*\n\n"
                            f"📦 *Product:* {prod}\n"
                            f"🏛 *Buyer / Org:* {org}\n"
                            f"📋 *Type:* {ltype}\n"
                            f"👤 *Contact Person:* {contact}\n"
                            f"📧 *Email:* {email}\n"
                            f"📞 *Phone:* {phone}\n"
                            f"📝 *Summary:* {lead_summary}\n\n"
                            f"🔗 [Open Procurement Link]({item['link']})"
                        )
                        send_telegram(msg)
                    else:
                        reason = res.get("rejection_reason", "Not a real procurement lead")
                        print(f"  [AI Rejected]: {item['title'][:50]}... (Reason: {reason})")

    print(f"\n==========================================")
    print(f"Total qualified leads logged: {total_leads}")


if __name__ == "__main__":
    main()
