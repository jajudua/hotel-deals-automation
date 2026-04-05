#!/usr/bin/env python3
"""
Hotel Misprice Checker
Monitors Secret Flying, FlyerTalk, and Fly4Free for luxury hotel misprices.
Runs hourly via GitHub Actions, updates dashboard HTML, sends email alerts.
"""

import os
import re
import sys
import logging
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR        = os.getenv("OUTPUT_DIR", ".")
MISPRICE_LOG_FILE = os.path.join(OUTPUT_DIR, "misprice-log.txt")
DASHBOARD_FILE    = os.path.join(OUTPUT_DIR, "luxury-hotel-deals-report.html")
GMAIL_USER        = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS    = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL       = os.getenv("ALERT_EMAIL", "josephajudua@googlemail.com")
DEDUP_HOURS       = 6    # don't re-alert same hotel within this window
MISPRICE_MAX_AGE  = 12   # ignore posts older than this many hours

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Safe destinations (lowercase for matching) ────────────────────────────────
SAFE_DESTINATIONS = {
    "turks and caicos", "barbados", "cayman islands", "portugal", "italy",
    "france", "greece", "spain", "costa rica", "japan", "singapore",
    "maldives", "thailand", "dubai", "uae", "bali", "indonesia", "malaysia",
    "vietnam", "oman", "muscat", "mexico", "bahamas", "mauritius",
    "seychelles", "london", "paris", "rome", "barcelona", "lisbon",
    "amsterdam", "prague", "budapest", "croatia", "montenegro", "cyprus",
    "malta", "australia", "new zealand", "canada", "florida", "hawaii",
    "switzerland", "austria", "marbella", "phuket", "kuala lumpur", "cancun",
    "tulum", "mykonos", "santorini", "amalfi", "positano", "capri",
    "miami", "new york", "los angeles", "las vegas", "chicago", "boston",
    "edinburgh", "dublin", "stockholm", "copenhagen", "oslo", "helsinki",
    "kenya", "tanzania", "south africa", "cape town", "morocco", "marrakech",
    "turkey", "istanbul", "abu dhabi", "bahrain", "jordan", "amman",
    "peru", "colombia", "chile", "argentina", "brazil", "rio",
    "india", "goa", "rajasthan", "sri lanka", "cambodia", "taiwan",
    "hong kong", "macau", "south korea", "seoul", "beijing", "shanghai",
}

# ── Patterns ──────────────────────────────────────────────────────────────────
STAR_RE = re.compile(
    r'(?:5|4|five|four)[\s\-]?(?:\*|★|star)',
    re.IGNORECASE
)

PRICE_RE = re.compile(
    r'(?:for only|just|from|at)?\s*'
    r'(?P<currency>£|€|\$|USD|EUR|GBP|AED|SGD|THB|MYR|AUD|CAD)\s*'
    r'(?P<amount>[\d,]+(?:\.\d{1,2})?)'
    r'(?:\s*(?:per night|/night|a night|pn))?',
    re.IGNORECASE
)

ALT_PRICE_RE = re.compile(
    r'(?P<amount>[\d,]+(?:\.\d{1,2})?)\s*'
    r'(?P<currency>USD|EUR|GBP|AED|SGD|THB|MYR|AUD|CAD)'
    r'(?:\s*(?:per night|/night|a night|pn))?',
    re.IGNORECASE
)

CURRENCY_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def parse_price(text):
    """Return (amount_float, currency_code) or (None, None)."""
    m = PRICE_RE.search(text)
    if not m:
        m = ALT_PRICE_RE.search(text)
    if not m:
        return None, None
    amount_str = m.group("amount").replace(",", "")
    currency   = m.group("currency")
    currency   = CURRENCY_SYMBOLS.get(currency, currency.upper())
    try:
        return float(amount_str), currency
    except ValueError:
        return None, None


def is_safe_destination(text):
    text_lower = text.lower()
    return any(dest in text_lower for dest in SAFE_DESTINATIONS)


def is_luxury(text):
    return bool(STAR_RE.search(text))


def parse_pub_date(date_str):
    """Return timezone-aware datetime or None."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass
    # fallback: try ISO
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def age_minutes(pub_dt):
    if pub_dt is None:
        return 999
    delta = now_utc() - pub_dt
    return int(delta.total_seconds() / 60)


def urgency_label(minutes):
    if minutes < 60:
        return "🔴", f"POSTED {minutes} MIN{'S' if minutes != 1 else ''} AGO", "#ff6464", "ACT NOW"
    if minutes < 360:
        h = minutes // 60
        return "🟠", f"POSTED {h}H AGO", "#f0a830", "STILL ACTIVE"
    if minutes < 720:
        h = minutes // 60
        return "⏳", f"POSTED {h}H AGO", "#8a90a0", "FADING"
    return "❌", "EXPIRED", "#445", ""


# ── Deduplication log ─────────────────────────────────────────────────────────
def load_misprice_log():
    """Return dict keyed by 'HOTEL|LOCATION' with timestamp for last 24h."""
    seen = {}
    if not os.path.exists(MISPRICE_LOG_FILE):
        return seen
    cutoff = now_utc() - timedelta(hours=24)
    try:
        with open(MISPRICE_LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) < 5:
                    continue
                hotel, location, price, ts_str, source = parts[:5]
                ts = parse_pub_date(ts_str)
                if ts and ts > cutoff:
                    key = f"{hotel.lower()}|{location.lower()}"
                    seen[key] = {"timestamp": ts, "price": price}
    except Exception as e:
        log.warning(f"Could not read misprice log: {e}")
    return seen


def append_misprice_log(hotel, location, price, source):
    ts = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(MISPRICE_LOG_FILE, "a") as f:
            f.write(f"{hotel}|{location}|{price}|{ts}|{source}\n")
        log.info(f"Logged: {hotel} | {location} | {price}")
    except Exception as e:
        log.warning(f"Could not write misprice log: {e}")


# ── Source 1: Secret Flying RSS ───────────────────────────────────────────────
def check_secret_flying():
    log.info("Checking Secret Flying RSS...")
    candidates = []
    url = "https://www.secretflying.com/posts/category/hotel-star-rating/feed/"
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
        if feed.bozo and not feed.entries:
            log.warning(f"Secret Flying feed error: {feed.bozo_exception}")
            return candidates

        log.info(f"Secret Flying: {len(feed.entries)} entries in feed")
        cutoff = now_utc() - timedelta(hours=MISPRICE_MAX_AGE)

        for entry in feed.entries[:25]:
            title   = entry.get("title", "")
            link    = entry.get("link", "")
            pub_str = entry.get("published", "")
            summary = entry.get("summary", "")
            full    = f"{title} {summary}"

            pub_dt  = parse_pub_date(pub_str)
            if pub_dt and pub_dt < cutoff:
                continue  # too old

            if not is_luxury(full):
                continue
            if not is_safe_destination(full):
                log.debug(f"Skipping (unsafe destination): {title}")
                continue

            amount, currency = parse_price(full)
            if amount is None:
                # Still include it but flag as unpriced
                log.debug(f"No price found in: {title}")

            candidates.append({
                "hotel":    title,
                "location": _extract_location(title),
                "price":    f"{currency} {amount:.0f}" if amount else "see link",
                "amount":   amount,
                "currency": currency,
                "source":   "Secret Flying",
                "link":     link,
                "pub_dt":   pub_dt,
                "raw":      title,
            })
            log.info(f"Candidate: {title[:80]} | {amount} {currency}")

    except Exception as e:
        log.error(f"Secret Flying check failed: {e}")
    return candidates


def _extract_location(title):
    """Best-effort: extract 'in [Location]' from Secret Flying title."""
    m = re.search(r'\bin\s+([A-Z][^,]+?)(?:\s+for only|\s+from|\s*$)', title, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return "Unknown"


# ── Source 2: FlyerTalk Hotel Deals ──────────────────────────────────────────
def check_flyertalk():
    log.info("Checking FlyerTalk Hotel Deals forum...")
    candidates = []
    url = "https://www.flyertalk.com/forum/hotel-deals/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cutoff = now_utc() - timedelta(hours=MISPRICE_MAX_AGE)

        # FlyerTalk thread rows
        rows = soup.select("li.discussionListItem") or soup.select("tr.threadbit")
        log.info(f"FlyerTalk: {len(rows)} threads found")

        for row in rows[:25]:
            title_el = row.select_one("a.PreviewTooltip") or row.select_one("h3.title a") or row.select_one("a[id^='thread_title']")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            link  = title_el.get("href", "")
            if link and not link.startswith("http"):
                link = "https://www.flyertalk.com" + link

            # Skip if no price visible in title
            if not (PRICE_RE.search(title) or ALT_PRICE_RE.search(title)):
                continue
            if not is_luxury(title):
                continue
            if not is_safe_destination(title):
                continue

            amount, currency = parse_price(title)
            candidates.append({
                "hotel":    title,
                "location": _extract_location(title),
                "price":    f"{currency} {amount:.0f}" if amount else "see link",
                "amount":   amount,
                "currency": currency,
                "source":   "FlyerTalk",
                "link":     link,
                "pub_dt":   None,  # FlyerTalk listing pages don't always show exact time
                "raw":      title,
            })
            log.info(f"FlyerTalk candidate: {title[:80]}")

    except requests.RequestException as e:
        log.error(f"FlyerTalk check failed (network): {e}")
    except Exception as e:
        log.error(f"FlyerTalk check failed: {e}")
    return candidates


# ── Source 3: Fly4Free Hotel Mistake Rate ─────────────────────────────────────
def check_fly4free():
    log.info("Checking Fly4Free hotel mistake rates...")
    candidates = []
    url = "https://www.fly4free.com/flight-deals/hotel-mistake-rate/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cutoff = now_utc() - timedelta(hours=MISPRICE_MAX_AGE)

        # Fly4Free deal cards / article list items
        articles = soup.select("article") or soup.select(".deal-item") or soup.select("li.postitem")
        log.info(f"Fly4Free: {len(articles)} articles found")

        for article in articles[:20]:
            title_el = article.select_one("h2 a") or article.select_one("h3 a") or article.select_one("a[class*='title']")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            link  = title_el.get("href", "") or url

            # Try to get date
            date_el = article.select_one("time") or article.select_one(".date")
            pub_str = date_el.get("datetime", "") if date_el else ""
            pub_dt  = parse_pub_date(pub_str)
            if pub_dt and pub_dt < cutoff:
                continue

            if not is_luxury(title):
                continue
            if not is_safe_destination(title):
                continue

            full_text = article.get_text(" ", strip=True)
            amount, currency = parse_price(f"{title} {full_text}")

            candidates.append({
                "hotel":    title,
                "location": _extract_location(title),
                "price":    f"{currency} {amount:.0f}" if amount else "see link",
                "amount":   amount,
                "currency": currency,
                "source":   "Fly4Free",
                "link":     link,
                "pub_dt":   pub_dt,
                "raw":      title,
            })
            log.info(f"Fly4Free candidate: {title[:80]}")

    except requests.RequestException as e:
        log.error(f"Fly4Free check failed (network): {e}")
    except Exception as e:
        log.error(f"Fly4Free check failed: {e}")
    return candidates


# ── Verification: estimate normal price from Booking.com search ───────────────
def estimate_normal_price(hotel_name, location):
    """
    Scrape Booking.com search results for a rough normal price estimate.
    Returns (normal_price_float, currency) or (None, None).
    This is a best-effort estimate — treat as indicative only.
    """
    query = f"{hotel_name} {location}"
    search_url = (
        f"https://www.booking.com/search.html"
        f"?ss={requests.utils.quote(query)}&checkin=2026-05-01&checkout=2026-05-02"
        f"&selected_currency=USD&lang=en-us"
    )
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Booking.com price elements (data-testid or class based)
        price_els = (
            soup.select("[data-testid='price-and-discounted-price']")
            or soup.select(".prco-valign-middle-helper")
            or soup.select("span[aria-hidden='true']")
        )
        prices = []
        for el in price_els[:10]:
            txt = el.get_text(strip=True)
            amount, currency = parse_price(txt)
            if amount and amount > 20:   # ignore nonsensical low values
                prices.append(amount)

        if prices:
            median = sorted(prices)[len(prices) // 2]
            return median, "USD"
    except Exception as e:
        log.debug(f"Booking.com estimate failed for {hotel_name}: {e}")
    return None, None


def verify_misprice(candidate):
    """
    Determine if candidate is a real misprice.
    Returns (status, discount_pct, normal_price).
    status: "VERIFIED" | "UNVERIFIED" | "REJECTED" | "UNKNOWN"
    """
    misprice_amount = candidate.get("amount")
    if misprice_amount is None:
        return "UNKNOWN", None, None

    hotel    = candidate.get("hotel", "")
    location = candidate.get("location", "")

    normal_price, _ = estimate_normal_price(hotel, location)

    if normal_price is None:
        log.info(f"Could not fetch normal price for {hotel} — marking UNKNOWN")
        return "UNKNOWN", None, None

    if normal_price <= misprice_amount:
        return "REJECTED", 0, normal_price

    discount_pct = round((1 - misprice_amount / normal_price) * 100)

    if discount_pct >= 30:
        return "VERIFIED", discount_pct, normal_price
    if discount_pct >= 10:
        return "UNVERIFIED", discount_pct, normal_price
    return "REJECTED", discount_pct, normal_price


# ── Dashboard HTML ────────────────────────────────────────────────────────────
def build_alert_card(m):
    mins     = age_minutes(m["pub_dt"])
    icon, label, color, urgency = urgency_label(mins)
    price    = m.get("price", "see link")
    normal   = m.get("normal_price")
    disc     = m.get("discount_pct")
    status   = m.get("status", "UNKNOWN")
    source   = m.get("source", "")
    link     = m.get("link", "#")
    hotel    = m.get("hotel", "Unknown Hotel")
    location = m.get("location", "")
    stars    = "★★★★★" if "5" in m.get("raw", "") else "★★★★"

    normal_line = ""
    if normal:
        normal_line = f'<p style="font-size:13px;color:#556;text-decoration:line-through;margin:2px 0;">Normal: ~${normal:.0f}/night</p>'
    disc_line = ""
    if disc:
        disc_line = f'<p style="font-size:13px;color:#32c864;margin:2px 0 10px;">~{disc}% OFF</p>'

    urgency_banner = ""
    if mins < 60 and urgency:
        urgency_banner = f'''
        <div style="background:rgba(200,50,50,0.1);border:1px solid rgba(200,50,50,0.3);
                    border-radius:6px;padding:8px 12px;margin-bottom:10px;">
          <p style="font-size:12px;color:#ff6464;margin:0;font-weight:bold;">
            ⏰ {urgency} — Posted {mins} minute{'s' if mins != 1 else ''} ago, window closing fast
          </p>
        </div>'''

    unverified_note = ""
    if status == "UNVERIFIED":
        unverified_note = '<p style="font-size:11px;color:#f0a830;margin:0 0 8px;">⚠️ Unconfirmed discount — book refundable</p>'
    elif status == "UNKNOWN":
        unverified_note = '<p style="font-size:11px;color:#667;margin:0 0 8px;">Price comparison unavailable — verify before booking</p>'

    return f'''
    <div style="background:#1a1a30;border:2px solid rgba(200,50,50,0.4);
                border-radius:10px;padding:18px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
        <div>
          <p style="font-size:10px;color:{color};letter-spacing:2px;
                    text-transform:uppercase;margin:0 0 4px;">{stars} · {location} · {source}</p>
          <h3 style="font-size:15px;color:#fff;margin:0;font-weight:400;
                     line-height:1.3;">{hotel[:60]}</h3>
        </div>
        <span style="background:rgba(200,50,50,0.15);color:{color};
                     border:1px solid rgba(200,50,50,0.4);font-size:9px;font-weight:700;
                     padding:4px 10px;border-radius:20px;white-space:nowrap;flex-shrink:0;
                     margin-left:8px;">{icon} {label}</span>
      </div>

      <p style="font-size:26px;color:#ff6464;font-weight:bold;margin:0;">{price}</p>
      {normal_line}
      {disc_line}
      {unverified_note}
      {urgency_banner}

      <div style="border-top:1px solid rgba(255,255,255,0.06);padding-top:12px;display:flex;gap:8px;">
        <a href="https://www.booking.com/search.html?ss={requests.utils.quote(hotel)}"
           style="flex:1;color:#fff;text-decoration:none;background:rgba(200,50,50,0.15);
                  border:1px solid rgba(200,50,50,0.3);padding:7px;border-radius:5px;
                  text-align:center;font-size:11px;font-weight:bold;">BOOK NOW →</a>
        <a href="{link}"
           style="flex:1;color:#ff6464;text-decoration:none;border:1px solid rgba(200,50,50,0.3);
                  padding:7px;border-radius:5px;text-align:center;font-size:11px;">See Post →</a>
      </div>
    </div>'''


def update_dashboard(new_misprices):
    ts = now_utc().strftime("%a %d %b %Y · %H:%M UTC")

    # Build alert cards or empty state
    if new_misprices:
        cards_html = "\n".join(build_alert_card(m) for m in new_misprices)
        alert_inner = f'''
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;">
          {cards_html}
        </div>'''
    else:
        alert_inner = f'''
        <div style="display:flex;align-items:center;gap:12px;background:rgba(255,255,255,0.03);
                    border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:16px 20px;">
          <div style="width:8px;height:8px;background:#445;border-radius:50%;flex-shrink:0;"></div>
          <p style="font-size:13px;color:#556;margin:0;line-height:1.5;">
            No active misprices right now. Monitoring Secret Flying, FlyerTalk &amp; Fly4Free hourly.
            Check sources directly:
            <a href="https://www.secretflying.com/hotel-deals/" style="color:#ff6464;">Secret Flying</a> ·
            <a href="https://www.flyertalk.com/forum/hotel-deals/" style="color:#ff6464;">FlyerTalk</a> ·
            <a href="https://www.fly4free.com/flight-deals/hotel-mistake-rate/" style="color:#ff6464;">Fly4Free</a>
          </p>
        </div>'''

    source_status = f'''
    <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);
                border-radius:8px;padding:14px 20px;margin-bottom:24px;
                display:flex;flex-wrap:wrap;gap:14px;">
      <span style="font-size:11px;color:#556;">🕐 Last scan: {ts}</span>
      <span style="font-size:11px;color:#32c864;">● Secret Flying</span>
      <span style="font-size:11px;color:#32c864;">● FlyerTalk</span>
      <span style="font-size:11px;color:#32c864;">● Fly4Free</span>
      <span style="font-size:11px;color:#{'32c864' if new_misprices else '445'};">
        {'● ' + str(len(new_misprices)) + ' misprice(s) active' if new_misprices else '● No misprices detected'}
      </span>
    </div>'''

    misprice_section = f'''
    <div style="background:rgba(200,50,50,0.06);border:2px solid rgba(200,50,50,0.25);
                border-radius:12px;padding:22px;margin-bottom:28px;">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
        <span style="font-size:22px;">🚨</span>
        <h2 style="font-size:16px;color:#ff6464;margin:0;letter-spacing:1.5px;
                   text-transform:uppercase;font-weight:600;">Live Misprice Alerts — Last 24 Hours</h2>
      </div>
      {alert_inner}
    </div>'''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <meta http-equiv="refresh" content="1800"/>
  <title>🏨 Luxury Hotel Misprice Monitor</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0d0d1a;color:#d0d4e0;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         padding:24px;min-height:100vh}}
    a{{color:inherit}}
    .header{{text-align:center;padding:32px 0 24px;
             border-bottom:1px solid rgba(255,255,255,0.07);margin-bottom:24px}}
    .header h1{{font-size:26px;font-weight:300;letter-spacing:3px;
                color:#fff;text-transform:uppercase}}
    .header p{{font-size:13px;color:#556;margin-top:8px;letter-spacing:1px}}
    .section-label{{font-size:11px;letter-spacing:3px;text-transform:uppercase;
                    color:#556;margin-bottom:16px;padding-bottom:8px;
                    border-bottom:1px solid rgba(255,255,255,0.05)}}
    .section-label span{{color:#c8a85a}}
    .deals-grid{{display:grid;
                 grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
                 gap:16px;margin-bottom:32px}}
    .deal{{background:#111122;border:1px solid rgba(255,255,255,0.07);
           border-radius:10px;padding:18px;transition:border-color 0.2s}}
    .deal:hover{{border-color:rgba(200,168,90,0.3)}}
    footer{{text-align:center;font-size:11px;color:#334;padding-top:24px;
            border-top:1px solid rgba(255,255,255,0.04);margin-top:16px}}
  </style>
</head>
<body>
  <div class="header">
    <h1>🏨 Luxury Hotel Misprice Monitor</h1>
    <p>Secret Flying · FlyerTalk · Fly4Free — Updated hourly via GitHub Actions</p>
  </div>

  {source_status}
  {misprice_section}

  <p class="section-label"><span>★★★★★ &amp; ★★★★</span> Best Tracked Deals</p>
  <div class="deals-grid">
    <div class="deal">
      <p style="font-size:10px;color:#c8a85a;letter-spacing:2px;
                text-transform:uppercase;margin:0 0 4px;">★★★★★ · Phuket, Thailand</p>
      <h3 style="font-size:14px;color:#fff;font-weight:400;margin:0 0 8px;">
        Supicha Pool Access Hotel</h3>
      <p style="font-size:22px;color:#c8a85a;margin:0 0 2px;">$41 <span style="font-size:13px;color:#556;">/night</span></p>
      <p style="font-size:11px;color:#32c864;margin:0 0 10px;">~70% below market rate</p>
      <p style="font-size:11px;color:#556;line-height:1.5;margin:0 0 12px;">
        Verify via Booking.com before booking. Always select free cancellation.</p>
      <div style="display:flex;gap:8px;">
        <a href="https://www.booking.com/search.html?ss=Supicha+Pool+Access+Hotel+Phuket"
           style="flex:1;background:rgba(200,168,90,0.12);border:1px solid rgba(200,168,90,0.3);
                  color:#c8a85a;text-decoration:none;padding:6px;border-radius:5px;
                  text-align:center;font-size:11px;font-weight:700;">BOOK →</a>
        <a href="https://www.secretflying.com/hotel-deals/"
           style="flex:1;border:1px solid rgba(255,255,255,0.08);color:#667;
                  text-decoration:none;padding:6px;border-radius:5px;
                  text-align:center;font-size:11px;">Source →</a>
      </div>
    </div>

    <div class="deal">
      <p style="font-size:10px;color:#c8a85a;letter-spacing:2px;
                text-transform:uppercase;margin:0 0 4px;">★★★★★ · Nanjing, China</p>
      <h3 style="font-size:14px;color:#fff;font-weight:400;margin:0 0 8px;">
        Nanjing Central Hotel</h3>
      <p style="font-size:22px;color:#c8a85a;margin:0 0 2px;">$42 <span style="font-size:13px;color:#556;">/night</span></p>
      <p style="font-size:11px;color:#32c864;margin:0 0 10px;">~65% below comparable rate</p>
      <p style="font-size:11px;color:#556;line-height:1.5;margin:0 0 12px;">
        China requires advance visa. Always book refundable.</p>
      <div style="display:flex;gap:8px;">
        <a href="https://www.booking.com/search.html?ss=Nanjing+Central+Hotel"
           style="flex:1;background:rgba(200,168,90,0.12);border:1px solid rgba(200,168,90,0.3);
                  color:#c8a85a;text-decoration:none;padding:6px;border-radius:5px;
                  text-align:center;font-size:11px;font-weight:700;">BOOK →</a>
        <a href="https://www.secretflying.com/hotel-deals/"
           style="flex:1;border:1px solid rgba(255,255,255,0.08);color:#667;
                  text-decoration:none;padding:6px;border-radius:5px;
                  text-align:center;font-size:11px;">Source →</a>
      </div>
    </div>

    <div class="deal">
      <p style="font-size:10px;color:#c8a85a;letter-spacing:2px;
                text-transform:uppercase;margin:0 0 4px;">★★★★★ · Muscat, Oman</p>
      <h3 style="font-size:14px;color:#fff;font-weight:400;margin:0 0 8px;">
        5★ Muscat Properties</h3>
      <p style="font-size:22px;color:#c8a85a;margin:0 0 2px;">$70–150 <span style="font-size:13px;color:#556;">/night</span></p>
      <p style="font-size:11px;color:#32c864;margin:0 0 10px;">~70% below Western equivalents</p>
      <p style="font-size:11px;color:#556;line-height:1.5;margin:0 0 12px;">
        Shangri-La, Alila &amp; others consistently underpriced. Worth monitoring.</p>
      <div style="display:flex;gap:8px;">
        <a href="https://www.booking.com/searchresults.html?ss=Muscat+Oman&stars=5"
           style="flex:1;background:rgba(200,168,90,0.12);border:1px solid rgba(200,168,90,0.3);
                  color:#c8a85a;text-decoration:none;padding:6px;border-radius:5px;
                  text-align:center;font-size:11px;font-weight:700;">SEARCH →</a>
        <a href="https://www.secretflying.com/hotel-deals/"
           style="flex:1;border:1px solid rgba(255,255,255,0.08);color:#667;
                  text-decoration:none;padding:6px;border-radius:5px;
                  text-align:center;font-size:11px;">Source →</a>
      </div>
    </div>
  </div>

  <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);
              border-radius:8px;padding:14px 20px;margin-top:8px;">
    <p style="font-size:11px;color:#445;line-height:1.7;">
      ⚠️ <strong style="color:#556;">Disclaimer:</strong>
      Always book refundable rates when targeting potential misprices. Hotels may cancel or
      correct pricing errors without notice. Verify prices directly on Booking.com before
      confirming any booking. This dashboard is for informational purposes only.
    </p>
  </div>

  <footer>
    <p>Luxury Hotel Misprice Monitor · Auto-updated hourly via GitHub Actions · jajudua/hotel-deals-automation</p>
  </footer>
</body>
</html>'''

    try:
        with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        log.info(f"Dashboard written to {DASHBOARD_FILE}")
    except Exception as e:
        log.error(f"Failed to write dashboard: {e}")


# ── Email alert ───────────────────────────────────────────────────────────────
def send_email_alert(misprice):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.info("Email not configured (set GMAIL_USER + GMAIL_APP_PASSWORD secrets)")
        return

    hotel    = misprice.get("hotel", "Unknown Hotel")
    price    = misprice.get("price", "see link")
    location = misprice.get("location", "")
    source   = misprice.get("source", "")
    link     = misprice.get("link", "#")
    mins     = age_minutes(misprice.get("pub_dt"))
    disc     = misprice.get("discount_pct")
    normal   = misprice.get("normal_price")
    status   = misprice.get("status", "UNKNOWN")

    disc_line   = f"<p>~{disc}% below normal rate</p>" if disc else ""
    normal_line = f"<p>Normal rate: ~${normal:.0f}/night</p>" if normal else ""
    verify_note = {
        "VERIFIED":   "✅ Verified — price is 30%+ below Booking.com",
        "UNVERIFIED": "⚠️ Unconfirmed — book refundable",
        "UNKNOWN":    "ℹ️ Price comparison unavailable — verify manually",
    }.get(status, "")

    body = f"""
    <html><body style="font-family:sans-serif;background:#0d0d1a;color:#d0d4e0;padding:24px;">
      <div style="max-width:600px;margin:0 auto;">
        <h1 style="color:#ff6464;font-size:22px;">🚨 Hotel Misprice Alert</h1>
        <div style="background:#1a1a30;border:2px solid rgba(200,50,50,0.4);
                    border-radius:10px;padding:20px;margin:16px 0;">
          <h2 style="color:#fff;font-size:18px;margin:0 0 8px;">{hotel}</h2>
          <p style="color:#888;margin:0 0 12px;">📍 {location} · {source}</p>
          <p style="font-size:28px;color:#ff6464;font-weight:bold;margin:0;">{price}</p>
          {normal_line}{disc_line}
          <p style="color:#f0a830;font-size:13px;">⏰ Posted {mins} minute{'s' if mins!=1 else ''} ago</p>
          <p style="color:#889;font-size:12px;">{verify_note}</p>
        </div>
        <div style="text-align:center;margin:20px 0;">
          <a href="https://www.booking.com/search.html?ss={requests.utils.quote(hotel)}"
             style="background:#ff6464;color:#fff;text-decoration:none;padding:12px 28px;
                    border-radius:6px;font-size:14px;font-weight:bold;display:inline-block;">
            BOOK NOW →</a>
          &nbsp;
          <a href="{link}"
             style="background:transparent;color:#ff6464;text-decoration:none;padding:12px 20px;
                    border:1px solid #ff6464;border-radius:6px;font-size:14px;display:inline-block;">
            See Post →</a>
        </div>
        <p style="font-size:11px;color:#445;margin-top:24px;">
          ⚠️ Always book refundable. Hotel may cancel this rate without notice.
          This alert was generated automatically by your hotel misprice monitor.
        </p>
      </div>
    </body></html>"""

    subject = f"🚨 MISPRICE ALERT — {hotel[:40]} at {price}"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_EMAIL
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASS)
            smtp.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"Hotel Misprice Check — {now_utc().isoformat()}")
    log.info("=" * 60)

    # Step 1: Load dedup log
    seen = load_misprice_log()
    log.info(f"Loaded {len(seen)} recent entries from dedup log")

    # Step 2: Fetch candidates from all sources
    candidates = []
    candidates += check_secret_flying()
    candidates += check_flyertalk()
    candidates += check_fly4free()
    log.info(f"Total candidates: {len(candidates)}")

    # Step 3: Filter, deduplicate, verify
    new_misprices = []
    for c in candidates:
        key = f"{c['hotel'].lower()}|{c['location'].lower()}"

        # Dedup check
        if key in seen:
            ts = seen[key]["timestamp"]
            delta = now_utc() - ts
            if delta.total_seconds() < DEDUP_HOURS * 3600:
                log.info(f"SKIP (dedup): {c['hotel']}")
                continue

        # Age check
        mins = age_minutes(c.get("pub_dt"))
        if mins > MISPRICE_MAX_AGE * 60:
            log.info(f"SKIP (expired): {c['hotel']}")
            continue

        # Verify
        status, disc_pct, normal_price = verify_misprice(c)
        if status == "REJECTED":
            log.info(f"REJECTED (not a misprice): {c['hotel']}")
            continue

        c["status"]       = status
        c["discount_pct"] = disc_pct
        c["normal_price"] = normal_price

        log.info(f"NEW MISPRICE: {c['hotel']} | {c['price']} | {status} | {disc_pct}% off")
        new_misprices.append(c)
        append_misprice_log(c["hotel"], c["location"], c["price"], c["source"])

    # Step 4: Update dashboard
    update_dashboard(new_misprices)

    # Step 5: Email alerts for new misprices
    for m in new_misprices:
        send_email_alert(m)

    log.info(f"Done — {len(new_misprices)} new misprice(s) found")
    return len(new_misprices)


if __name__ == "__main__":
    main()
