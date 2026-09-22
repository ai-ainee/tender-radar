import json
import os
import urllib.parse
from datetime import datetime, timezone
import feedparser
from google import genai
import requests

# 1. Load Environment Secrets
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"


def check_secrets():
    print("--- 1. CHECKING SECRETS ---")
    print(f"GEMINI_API_KEY present: {bool(GEMINI_API_KEY)}")
    print(f"TELEGRAM_BOT_TOKEN present: {bool(TELEGRAM_BOT_TOKEN)}")
    print(f"TELEGRAM_CHAT_ID present: {bool(TELEGRAM_CHAT_ID)}")
    print(f"GOOGLE_SHEET_WEBHOOK present: {bool(GOOGLE_SHEET_WEBHOOK)}")


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
        print("Skipping Sheet: GOOGLE_SHEET_WEBHOOK not set.")
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
        print(f"Sheet Response: Status {res.status_code} | Text: {res.text[:100]}")
    except Exception as e:
        print(f"Error appending to Google Sheet: {e}")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Skipping Telegram: Missing token or chat ID.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        print(f"Telegram Response: Status {res.status_code}")
        if res.status_code != 200:
            print(f"Telegram Error Body: {res.text}")
    except Exception as e:
        print(f"Telegram Exception: {e}")


def analyze_with_ai(client, product, title, summary):
    prompt = f"""
    Analyze this web announcement for: "{product}".
    Title: {title}
    Content: {summary}

    Determine if this represents a commercial procurement opportunity in India:
    - Central Government / PSU tender (GeM, CPPP, Railways, Defense, etc.)
    - State Government tenders
    - Corporate Capex expansion, factory setup, or project orders
    - Bulk buying inquiry / vendor requirement

    Extract contact person, email, and phone if present. If not found, use "Not Listed".

    Reply ONLY with valid raw JSON (no backticks or extra text):
    {{
      "is_lead": true,
      "lead_type": "Tender / Capex / Inquiry",
      "org": "Organization/Department/Company name",
      "contact_person": "Name or Not Listed",
      "email": "Email or Not Listed",
      "phone": "Phone or Not Listed",
      "summary": "1 concise sentence explaining the procurement need"
    }}
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        clean = (
            response.text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return json.loads(clean)
    except Exception as e:
        print(f"AI Parse Error: {e}")
        return {"is_lead": False}


def main():
    check_secrets()
    if not GEMINI_API_KEY:
        print("CRITICAL: GEMINI_API_KEY is missing from GitHub Secrets.")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    print(f"Products loaded: {products}")
    if not products:
        print("ERROR: products.txt is empty!")
        return

    total_leads_found = 0

    for prod in products:
        print(f"\n--- Scanning for: {prod} ---")
        # Broadened, reliable query syntax for Indian news & procurement
        query = f'{prod} (tender OR procurement OR contract OR bid OR capex)'
        encoded = urllib.parse.quote(query)
        rss_url = f"[https://news.google.com/rss/search?q=](https://news.google.com/rss/search?q=){encoded}&hl=en-IN&gl=IN&ceid=IN:en"

        feed = feedparser.parse(rss_url)
        print(f"Articles retrieved from Google News: {len(feed.entries)}")

        for entry in feed.entries[:6]:
            link = getattr(entry, "link", "")
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "")

            if not link or link in seen:
                continue

            analysis = analyze_with_ai(client, prod, title, summary)

            if analysis.get("is_lead") is True:
                total_leads_found += 1
                org = analysis.get("org", "Govt / Enterprise")
                ltype = analysis.get("lead_type", "Commercial Requirement")
                contact = analysis.get("contact_person", "Not Listed")
                email = analysis.get("email", "Not Listed")
                phone = analysis.get("phone", "Not Listed")
                lead_summary = analysis.get("summary", "N/A")

                print(f"[QUALIFIED LEAD]: {title[:50]}...")
                push_to_google_sheet(prod, ltype, org, contact, email, phone, lead_summary, link)

                msg = (
                    f"🚨 *New Commercial Requirement!*\n\n"
                    f"📦 *Product:* {prod}\n"
                    f"🏛 *Organization:* {org}\n"
                    f"📋 *Type:* {ltype}\n"
                    f"👤 *Contact Person:* {contact}\n"
                    f"📧 *Email:* {email}\n"
                    f"📞 *Phone:* {phone}\n"
                    f"📝 *Summary:* {lead_summary}\n\n"
                    f"🔗 [Open Link]({link})"
                )
                send_telegram(msg)

            seen.add(link)
            save_seen(link)

    print(f"\nTotal qualified leads logged this run: {total_leads_found}")


if __name__ == "__main__":
    main()
