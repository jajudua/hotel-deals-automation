#!/usr/bin/env python3
"""
Hotel Misprice Checker — v2 (with Live Deal Cards)
- Monitors 12 sources hourly via GitHub Actions
- Populates 🚨 Misprice Alerts (strict: error/mistake/flash sale signals)
- Populates 🔴 Latest Scraped Deals card grid (broader: any hotel deal from 12 sources, kept 7 days)
"""

import os
import json
import requests
from datetime import datetime, timedelta
import feedparser
import re
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────────────
OUTPUT_DIR      = os.getenv("OUTPUT_DIR", ".")
MISPRICE_LOG    = os.path.join(OUTPUT_DIR, "misprice-log.txt")
DEALS_LOG       = os.path.join(OUTPUT_DIR, "deals-log.txt")
DASHBOARD_FILE  = os.path.join(OUTPUT_DIR, "luxury-hotel-deals-report.html")

# Safe destinations (FCDO Level 1 & 2)
SAFE_COUNTRIES = {
    "Turks and Caicos", "Turks & Caicos", "Barbados", "Cayman Islands",
    "Antigua", "St Lucia", "Saint Lucia", "Jamaica", "Bermuda", "Bahamas",
    "Portugal", "Italy", "France", "Greece", "Spain", "Croatia", "Switzerland",
    "Monaco", "Austria", "Germany", "Netherlands", "Czech Republic",
    "Costa Rica", "Belize", "Panama", "Peru", "Chile",
    "Japan", "Singapore", "Maldives", "Thailand", "Bali", "Indonesia",
    "Vietnam", "Malaysia", "South Korea",
    "Dubai", "UAE", "United Arab Emirates", "Oman", "Abu Dhabi",
    "Canada", "USA", "Mexico"
}

STAR_KEYWORDS = [
    "5 star", "5-star", "luxury", "four star", "4 star", "4-star",
    "4.5 star", "4.5-star", "premium", "boutique", "deluxe", "superior"
]

HOTEL_BRANDS = [
    "hyatt", "marriott", "hilton", "aman", "four seasons", "ritz", "accor",
    "ihg", "radisson", "jumeirah", "rosewood", "mandarin oriental",
    "park hyatt", "grand hyatt", "jw marriott", "w hotel", "sofitel",
    "sheraton", "westin", "le meridien", "st regis", "waldorf", "Conrad",
    "intercontinental", "raffles", "capella", "belmond", "one&only",
    "anantara", "six senses", "soneva", "cheval blanc", "sandy lane",
    "atlantis", "iberostar", "riu", "melia", "barcelo", "sandals"
]

# Strict: must have one of these for the 🚨 misprice alerts box
MISPRICE_KEYWORDS = [
    "mistake", "misprice", "mistake rate", "error rate", "pricing error",
    "glitch", "bug rate", "accidental", "flash sale",
    "mistake fare", "error fare", "award rate", "reward night", "free night",
    "points rate", "error price"
]

# Broad: any of these qualify a post as a deal worth showing in the card grid
DEAL_KEYWORDS = MISPRICE_KEYWORDS + [
    "% off", "percent off", "discount", "deal", "offer", "sale",
    "cheap", "from £", "from $", "from €", "save", "reduced",
    "limited time", "promo", "promotion", "code", "voucher",
    "early bird", "last minute", "flash deal", "exclusive rate",
    "best rate", "lowest price", "special offer", "limited offer"
]

# Blog noise — skip these entirely
EXCLUDE_KEYWORDS = [
    "credit card", "amex", "american express", "points strategy",
    "how to earn", "tier points", "status match", "mobile check-in",
    "joins star alliance", "leaves marriott", "acquisition", "review:",
    "interview", "opinion", "guide to", "best credit", "earn miles",
    "lounge review", "flight review", "transfer bonus", "card review",
    "annual fee", "sign-up bonus", "welcome bonus", "referral"
]


# ── Log helpers ──────────────────────────────────────────────────────────────

def load_log(filepath):
    """Load a pipe-delimited log file into a dict keyed by hotel|location"""
    records = {}
    if not os.path.exists(filepath):
        return records
    try:
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) >= 4:
                    key = f"{parts[0]}|{parts[1]}"
                    records[key] = {
                        "hotel":     parts[0],
                        "location":  parts[1],
                        "price":     parts[2],
                        "timestamp": parts[3],
                        "source":    parts[4] if len(parts) > 4 else "unknown",
                        "link":      parts[5] if len(parts) > 5 else "#"
                    }
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
    return records


def append_to_log(filepath, hotel, location, price, source, link="#"):
    timestamp = datetime.utcnow().isoformat() + "Z"
    try:
        with open(filepath, 'a') as f:
            f.write(f"{hotel}|{location}|{price}|{timestamp}|{source}|{link}\n")
    except Exception as e:
        print(f"Error writing to {filepath}: {e}")


def is_duplicate_in_log(hotel, location, log, hours=6):
    key = f"{hotel}|{location}"
    if key not in log:
        return False
    try:
        ts = datetime.fromisoformat(log[key]["timestamp"].replace("Z", "+00:00"))
        age = datetime.utcnow().replace(tzinfo=ts.tzinfo) - ts
        return age < timedelta(hours=hours)
    except:
        return False


def get_recent_from_log(log, hours=168):
    """Pull records younger than `hours` from a log dict, sorted newest first"""
    recent = []
    now = datetime.utcnow()
    for key, data in log.items():
        try:
            ts = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            age = now.replace(tzinfo=ts.tzinfo) - ts
            if age < timedelta(hours=hours):
                mins = int(age.total_seconds() / 60)
                recent.append({**data, "minutes_ago": mins})
        except:
            pass
    recent.sort(key=lambda x: x["minutes_ago"])
    return recent


# ── Filters ──────────────────────────────────────────────────────────────────

def is_hotel_content(combined):
    return (
        any(kw in combined for kw in STAR_KEYWORDS) or
        any(brand in combined for brand in HOTEL_BRANDS) or
        "hotel" in combined or "resort" in combined
    )

def is_real_misprice(combined):
    has_misprice = any(kw in combined for kw in MISPRICE_KEYWORDS)
    has_noise    = any(kw in combined for kw in EXCLUDE_KEYWORDS)
    return has_misprice and not has_noise

def is_real_deal(combined):
    has_deal  = any(kw in combined for kw in DEAL_KEYWORDS)
    has_noise = any(kw in combined for kw in EXCLUDE_KEYWORDS)
    return has_deal and not has_noise


# ── Scrapers ─────────────────────────────────────────────────────────────────

def extract_entry(entry, source_name):
    """Turn a feedparser entry into a normalised dict"""
    title   = entry.get('title', '')
    summary = entry.get('summary', '').lower()
    link    = entry.get('link', '')
    pub     = entry.get('published', datetime.utcnow().isoformat())
    combined = title.lower() + ' ' + summary

    location = "International"
    for country in SAFE_COUNTRIES:
        if country.lower() in combined:
            location = country
            break

    price_match = re.search(r'[£$€]\s*[\d,]+', title)
    price = price_match.group().replace(' ', '') if price_match else "See post"

    return {
        "hotel":       title[:90],
        "location":    location,
        "price":       price,
        "normal_price":"N/A",
        "source":      source_name,
        "link":        link,
        "published":   pub,
        "combined":    combined
    }


def scrape_rss(url, source_name, limit=25):
    entries = []
    try:
        feed = feedparser.parse(url)
        print(f"   {source_name}: {len(feed.entries)} feed entries")
        for e in feed.entries[:limit]:
            entry = extract_entry(e, source_name)
            if is_hotel_content(entry["combined"]):
                entries.append(entry)
    except Exception as ex:
        print(f"   Error {source_name}: {ex}")
    return entries


def check_secret_flying_rss():
    return scrape_rss(
        "https://www.secretflying.com/posts/category/hotel-star-rating/feed/",
        "Secret Flying"
    )

def check_head_for_points():
    return scrape_rss("https://www.headforpoints.com/feed/", "Head for Points")

def check_view_from_the_wing():
    return scrape_rss("https://viewfromthewing.com/feed/", "View from the Wing")

def check_one_mile_at_a_time():
    return scrape_rss("https://onemileatatime.com/feed/", "One Mile at a Time")

def check_holiday_pirates():
    return scrape_rss("https://www.holidaypirates.com/feeds/deals.rss", "Holiday Pirates")

def check_the_points_guy():
    return scrape_rss("https://thepointsguy.com/feed/", "The Points Guy")

def check_frequent_miler():
    return scrape_rss("https://frequentmiler.com/feed/", "Frequent Miler")

def check_doctor_of_credit():
    return scrape_rss("https://www.doctorofcredit.com/feed/", "Doctor of Credit")

def check_miles_to_memories():
    return scrape_rss("https://milestomemories.com/feed/", "Miles to Memories")


def check_flyertalk():
    entries = []
    try:
        r = requests.get(
            "https://www.flyertalk.com/forum/hotel-deals/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            threads = soup.find_all('a', class_='thread-title')
            print(f"   FlyerTalk: {len(threads)} threads found")
            for t in threads[:20]:
                title = t.get_text(strip=True)
                link  = t.get('href', '')
                combined = title.lower()
                if not is_hotel_content(combined):
                    continue
                location = "International"
                for c in SAFE_COUNTRIES:
                    if c.lower() in combined:
                        location = c
                        break
                price_m = re.search(r'[£$€]\s*[\d,]+', title)
                entries.append({
                    "hotel":    title[:90],
                    "location": location,
                    "price":    price_m.group().replace(' ', '') if price_m else "See thread",
                    "source":   "FlyerTalk",
                    "link":     "https://www.flyertalk.com" + link if link.startswith('/') else link,
                    "published":datetime.utcnow().isoformat() + "Z",
                    "combined": combined
                })
    except Exception as ex:
        print(f"   Error FlyerTalk: {ex}")
    return entries


def check_fly4free():
    entries = []
    try:
        r = requests.get(
            "https://www.fly4free.com/flight-deals/hotel-mistake-rate/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            articles = soup.find_all('article')
            print(f"   Fly4Free: {len(articles)} articles found")
            for art in articles[:15]:
                title   = art.get_text(strip=True)[:90]
                combined = title.lower()
                a_tag   = art.find('a')
                link    = a_tag.get('href', 'https://www.fly4free.com') if a_tag else 'https://www.fly4free.com'
                if not is_hotel_content(combined):
                    continue
                location = "International"
                for c in SAFE_COUNTRIES:
                    if c.lower() in combined:
                        location = c
                        break
                price_m = re.search(r'[£$€]\s*[\d,]+', title)
                entries.append({
                    "hotel":    title,
                    "location": location,
                    "price":    price_m.group().replace(' ', '') if price_m else "Check deal",
                    "source":   "Fly4Free",
                    "link":     link if link.startswith('http') else "https://www.fly4free.com",
                    "published":datetime.utcnow().isoformat() + "Z",
                    "combined": combined
                })
    except Exception as ex:
        print(f"   Error Fly4Free: {ex}")
    return entries


def check_travelzoo():
    """Travelzoo — flash hotel deals, very active for 4 and 5-star properties"""
    entries = []
    try:
        # Travelzoo RSS feed for hotel deals
        feed = feedparser.parse("https://www.travelzoo.com/blog/feed/")
        print(f"   Travelzoo blog: {len(feed.entries)} entries")
        for e in feed.entries[:25]:
            entry = extract_entry(e, "Travelzoo")
            if is_hotel_content(entry["combined"]):
                entries.append(entry)

        # Also scrape their top 20 deals page
        r = requests.get(
            "https://www.travelzoo.com/local-deals/international/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            deals = soup.find_all(['h3', 'h2'], limit=30)
            for deal in deals:
                title = deal.get_text(strip=True)
                combined = title.lower()
                if not is_hotel_content(combined):
                    continue
                link_tag = deal.find('a') or deal.find_parent('a')
                link = link_tag.get('href', 'https://www.travelzoo.com') if link_tag else 'https://www.travelzoo.com'
                if not link.startswith('http'):
                    link = 'https://www.travelzoo.com' + link
                location = "International"
                for c in SAFE_COUNTRIES:
                    if c.lower() in combined:
                        location = c
                        break
                price_m = re.search(r'[£$€]\s*[\d,]+', title)
                entries.append({
                    "hotel":     title[:90],
                    "location":  location,
                    "price":     price_m.group().replace(' ', '') if price_m else "See deal",
                    "source":    "Travelzoo",
                    "link":      link,
                    "published": datetime.utcnow().isoformat() + "Z",
                    "combined":  combined
                })
    except Exception as ex:
        print(f"   Error Travelzoo: {ex}")
    return entries


def check_secret_escapes():
    """Secret Escapes — luxury hotel flash sales, 50-70% off"""
    entries = []
    try:
        r = requests.get(
            "https://www.secretescapes.com/hotels",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            # Secret Escapes uses various card layouts — grab anything with a title
            cards = soup.find_all(['h2', 'h3', 'h4'], limit=40)
            print(f"   Secret Escapes: {len(cards)} headings found")
            seen = set()
            for card in cards:
                title = card.get_text(strip=True)
                if not title or title in seen or len(title) < 5:
                    continue
                seen.add(title)
                combined = title.lower()
                if not is_hotel_content(combined):
                    continue
                location = "International"
                for c in SAFE_COUNTRIES:
                    if c.lower() in combined:
                        location = c
                        break
                price_m = re.search(r'[£$€]\s*[\d,]+', title)
                entries.append({
                    "hotel":     title[:90],
                    "location":  location,
                    "price":     price_m.group().replace(' ', '') if price_m else "Members deal",
                    "source":    "Secret Escapes",
                    "link":      "https://www.secretescapes.com/hotels",
                    "published": datetime.utcnow().isoformat() + "Z",
                    "combined":  combined
                })
    except Exception as ex:
        print(f"   Error Secret Escapes: {ex}")
    return entries


def check_luxury_escapes():
    """Luxury Escapes — flash sales on 5-star resorts"""
    entries = []
    try:
        r = requests.get(
            "https://luxuryescapes.com/au/offers",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            cards = soup.find_all(['h2', 'h3', 'h4'], limit=40)
            print(f"   Luxury Escapes: {len(cards)} headings found")
            seen = set()
            for card in cards:
                title = card.get_text(strip=True)
                if not title or title in seen or len(title) < 5:
                    continue
                seen.add(title)
                combined = title.lower()
                if not is_hotel_content(combined):
                    continue
                location = "International"
                for c in SAFE_COUNTRIES:
                    if c.lower() in combined:
                        location = c
                        break
                price_m = re.search(r'[£$€$A]\s*[\d,]+', title)
                entries.append({
                    "hotel":     title[:90],
                    "location":  location,
                    "price":     price_m.group().replace(' ', '') if price_m else "Flash sale",
                    "source":    "Luxury Escapes",
                    "link":      "https://luxuryescapes.com/offers",
                    "published": datetime.utcnow().isoformat() + "Z",
                    "combined":  combined
                })
    except Exception as ex:
        print(f"   Error Luxury Escapes: {ex}")
    return entries


def check_loyalty_lobby():
    """Loyalty Lobby — hotel loyalty program errors, award glitches, rate mistakes"""
    return scrape_rss("https://loyaltylobby.com/feed/", "Loyalty Lobby")


def check_reddit_travel():
    entries = []
    subreddits = ["TravelHacks", "deals", "shoestring", "awardtravel"]
    try:
        for sub in subreddits:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/new.json?limit=25",
                headers={"User-Agent": "HotelMispriceBot/1.0"},
                timeout=10
            )
            if r.status_code == 200:
                posts = r.json().get('data', {}).get('children', [])
                print(f"   Reddit r/{sub}: {len(posts)} posts")
                for post in posts:
                    d = post.get('data', {})
                    title    = d.get('title', '')
                    combined = (title + ' ' + d.get('selftext', '')).lower()
                    if not is_hotel_content(combined):
                        continue
                    location = "International"
                    for c in SAFE_COUNTRIES:
                        if c.lower() in combined:
                            location = c
                            break
                    price_m = re.search(r'[£$€]\s*[\d,]+', title)
                    entries.append({
                        "hotel":    title[:90],
                        "location": location,
                        "price":    price_m.group().replace(' ', '') if price_m else "See post",
                        "source":   f"Reddit r/{sub}",
                        "link":     f"https://reddit.com{d.get('permalink', '')}",
                        "published":datetime.utcnow().isoformat() + "Z",
                        "combined": combined
                    })
    except Exception as ex:
        print(f"   Error Reddit: {ex}")
    return entries


# ── Dashboard updaters ───────────────────────────────────────────────────────

def _inject(html, div_id, content):
    """
    Replace content between sentinel comments inside a div.
    Sentinels: <!-- SENTINEL:div_id:START --> ... <!-- SENTINEL:div_id:END -->
    This is immune to nested divs and attribute ordering bugs.
    """
    start_marker = f'<!-- SENTINEL:{div_id}:START -->'
    end_marker   = f'<!-- SENTINEL:{div_id}:END -->'
    start_idx = html.find(start_marker)
    end_idx   = html.find(end_marker)
    if start_idx == -1 or end_idx == -1:
        print(f"  ⚠ Sentinel markers not found for #{div_id} — skipping injection")
        return html
    # Replace everything between the sentinels (not including the sentinels themselves)
    content_start = start_idx + len(start_marker)
    html = html[:content_start] + '\n    ' + content + '\n    ' + html[end_idx:]
    print(f"  ✓ Injected content into #{div_id}")
    return html


# ── Star rating classification ───────────────────────────────────────────────

FIVE_STAR_SIGNALS = [
    "5-star", "5 star", "five star", "ultra luxury", "ultra-luxury",
    "aman ", "amanjiwo", "amanyara", "four seasons", "ritz-carlton", "ritz carlton",
    "rosewood", "cheval blanc", "mandarin oriental", "belmond", "one&only", "one and only",
    "oberoi", "taj hotel", "soneva", "sandy lane", "bulgari hotel",
    "waldorf astoria", "st regis", "peninsula hotel", "raffles",
    "orient express", "burj al arab", "park hyatt"
]

FOUR_HALF_STAR_SIGNALS = [
    "4.5-star", "4.5 star", "four and a half", "superior deluxe",
    "jw marriott", "w hotel", "w resort", "w maldives", "capella",
    "sofitel legend", "sofitel luxury", "hotel arts", "bairro alto",
    "jumeirah", "atlantis the palm", "one&only royal", "excellence resort",
    "hyatt ziva", "secrets resort", "zoetry", "paradisus", "anantara",
    "six senses", "alila", "banyan tree"
]

FOUR_STAR_SIGNALS = [
    "4-star", "4 star", "four star",
    "hyatt regency", "hyatt centric", "hyatt place", "andaz",
    "marriott", "sheraton", "westin", "le meridien", "delta hotels",
    "hilton", "doubletree", "curio collection", "tapestry collection",
    "iberostar", "riu ", "melia ", "barcelo", "sandals", "royalton",
    "hard rock hotel", "moon palace", "grand palladium", "occidental",
    "bahia duque", "sol hotel", "novotel", "pullman", "mercure",
    "radisson", "ihg ", "voco ", "crowne plaza", "holiday inn resort"
]

def classify_stars(combined):
    """Return '5', '4.5', or '4' based on keywords in combined text"""
    for sig in FIVE_STAR_SIGNALS:
        if sig in combined:
            return "5"
    for sig in FOUR_HALF_STAR_SIGNALS:
        if sig in combined:
            return "4.5"
    for sig in FOUR_STAR_SIGNALS:
        if sig in combined:
            return "4"
    # Default: if it mentions luxury generically, assume 4.5; otherwise 4
    if "luxury" in combined or "boutique" in combined:
        return "4.5"
    return "4"


def _build_deal_card(d):
    """Render a single deal dict as a deal-card HTML block"""
    mins = d.get("minutes_ago", 0)
    if mins < 60:
        age_str = f"{mins} mins ago"
    elif mins < 1440:
        age_str = f"{mins // 60}h ago"
    else:
        age_str = f"{mins // 1440}d ago"
    return (
        f'<div class="deal-card" style="border-color:rgba(201,168,76,0.4);">\n'
        f'      <div class="location">{d.get("location","International")}</div>\n'
        f'      <h3>{d.get("hotel","Deal")[:75]}</h3>\n'
        f'      <div class="price">{d.get("price","See post")}</div>\n'
        f'      <div class="desc" style="font-size:11px; color:#556;">'
        f'📡 {d.get("source","")} &nbsp;·&nbsp; {age_str}</div>\n'
        f'      <a href="{d.get("link","#")}">View Deal →</a>\n'
        f'    </div>\n    '
    )


def update_dashboard(misprice_alerts, scraped_deals):
    if not os.path.exists(DASHBOARD_FILE):
        print(f"Dashboard file not found: {DASHBOARD_FILE}")
        return

    with open(DASHBOARD_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ── 1. Misprice alerts (strict, last 24 hrs) ──────────────────────────
    if misprice_alerts:
        cards = ""
        for mp in misprice_alerts:
            mins    = mp.get("minutes_ago", 0)
            urgency = "LIVE" if mins < 60 else ("COOLING" if mins < 360 else "FADING")
            cards += (
                f'<div class="misprice-card">\n'
                f'      <div class="misprice-location">{mp.get("location","")}</div>\n'
                f'      <h4>{mp.get("hotel","Hotel")}</h4>\n'
                f'      <div class="misprice-price">{mp.get("price","")}</div>\n'
                f'      <div class="misprice-time">{urgency} — {mins} mins ago · Source: {mp.get("source","")}</div>\n'
                f'      <a class="misprice-link" href="{mp.get("link","#")}">BOOK NOW →</a>&nbsp;'
                f'<a class="misprice-link" href="{mp.get("link","#")}">See Post →</a>\n'
                f'    </div>\n    '
            )
        html = _inject(html, "mispriceAlerts", cards)
    else:
        html = _inject(html, "mispriceAlerts",
            f'<p class="misprice-empty">No active misprices right now. '
            f'Checking 12 sources hourly. Last checked: {now_str}</p>')

    # ── 2. Activity log ───────────────────────────────────────────────────
    if misprice_alerts:
        log_rows = "".join(
            f'<div class="log-entry">'
            f'<span class="log-hotel">{m.get("hotel","")[:55]} — {m.get("location","")}</span>'
            f'<span class="log-time">{m.get("price","")} · {m.get("source","")} · {m.get("minutes_ago",0)} mins ago</span>'
            f'</div>\n    '
            for m in misprice_alerts
        )
        html = _inject(html, "mispriceLogo", log_rows)
    else:
        html = _inject(html, "mispriceLogo",
            f'<p class="log-empty">No misprices logged yet. Last checked: {now_str}</p>')

    # ── 3. Split scraped deals by star rating ─────────────────────────────
    five_star    = []
    four_half    = []
    four_star    = []

    for d in scraped_deals:
        rating = classify_stars(d.get("combined", d.get("hotel","")).lower())
        if rating == "5":
            five_star.append(d)
        elif rating == "4.5":
            four_half.append(d)
        else:
            four_star.append(d)

    print(f"  Deals split: {len(five_star)} × 5★  |  {len(four_half)} × 4.5★  |  {len(four_star)} × 4★")

    # ── 4. Inject into star sections (live deals appear above static watchlist) ─
    def build_star_block(deals, cap=20):
        if not deals:
            return ""
        header = (
            '<div style="font-size:11px; color:#32c864; letter-spacing:2px; '
            'text-transform:uppercase; margin-bottom:10px; padding:6px 10px; '
            'background:rgba(50,200,100,0.08); border:1px solid rgba(50,200,100,0.2); '
            'border-radius:6px; display:inline-block;">🔴 LIVE DEALS FROM 12 SOURCES</div>\n    '
        )
        cards = "".join(_build_deal_card(d) for d in deals[:cap])
        return header + cards

    html = _inject(html, "fiveStarLive",    build_star_block(five_star))
    html = _inject(html, "fourHalfStarLive",build_star_block(four_half))
    html = _inject(html, "fourStarLive",    build_star_block(four_star))

    # ── 5. Orange "latest deals" summary section (all deals, last 7 days) ─
    if scraped_deals:
        all_cards = "".join(_build_deal_card(d) for d in scraped_deals[:40])
        html = _inject(html, "latestDeals", all_cards)
    else:
        html = _inject(html, "latestDeals",
            f'<p style="color:#556; font-size:13px;">No deals found in the last 7 days. '
            f'Last checked: {now_str}</p>')

    with open(DASHBOARD_FILE, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✓ Dashboard written — {len(misprice_alerts)} alerts, "
          f"{len(five_star)}+{len(four_half)}+{len(four_star)} star deals, "
          f"{len(scraped_deals)} in summary section")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("Hotel Misprice Checker v2 — with Live Deal Cards")
    print("=" * 55)
    print(f"Running at {datetime.utcnow().isoformat()}Z\n")

    # Load existing logs
    misprice_log = load_log(MISPRICE_LOG)
    deals_log    = load_log(DEALS_LOG)
    print(f"Existing: {len(misprice_log)} misprices, {len(deals_log)} deals in logs\n")

    # Scrape all 12 sources
    sources = [
        ("📡 Secret Flying",        check_secret_flying_rss),
        ("📡 FlyerTalk",            check_flyertalk),
        ("📡 Fly4Free",             check_fly4free),
        ("📡 Head for Points",      check_head_for_points),
        ("📡 View from the Wing",   check_view_from_the_wing),
        ("📡 One Mile at a Time",   check_one_mile_at_a_time),
        ("📡 Holiday Pirates",      check_holiday_pirates),
        ("📡 The Points Guy",       check_the_points_guy),
        ("📡 Frequent Miler",       check_frequent_miler),
        ("📡 Doctor of Credit",     check_doctor_of_credit),
        ("📡 Miles to Memories",    check_miles_to_memories),
        ("📡 Reddit",               check_reddit_travel),
        ("📡 Travelzoo",            check_travelzoo),
        ("📡 Secret Escapes",       check_secret_escapes),
        ("📡 Luxury Escapes",       check_luxury_escapes),
        ("📡 Loyalty Lobby",        check_loyalty_lobby),
    ]

    all_entries = []
    for label, fn in sources:
        print(label + "...")
        found = fn()
        print(f"   → {len(found)} hotel entries")
        all_entries.extend(found)

    print(f"\nTotal raw hotel entries across all sources: {len(all_entries)}\n")

    new_misprices = 0
    new_deals     = 0

    for entry in all_entries:
        hotel    = entry.get("hotel", "")
        location = entry.get("location", "")
        price    = entry.get("price", "N/A")
        source   = entry.get("source", "unknown")
        link     = entry.get("link", "#")
        combined = entry.get("combined", hotel.lower())
        pub      = entry.get("published", datetime.utcnow().isoformat())

        # Calculate minutes ago for display
        try:
            ts  = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            age = datetime.utcnow().replace(tzinfo=ts.tzinfo) - ts
            mins = int(age.total_seconds() / 60)
        except:
            mins = 0
        entry["minutes_ago"] = mins

        # Strict misprice check → misprice log (6-hr dedup)
        if is_real_misprice(combined) and not is_duplicate_in_log(hotel, location, misprice_log, hours=6):
            append_to_log(MISPRICE_LOG, hotel, location, price, source, link)
            misprice_log = load_log(MISPRICE_LOG)   # reload to stay fresh
            new_misprices += 1
            print(f"  🚨 MISPRICE: {hotel[:50]} ({location}) — {price}")

        # Broad deal check → deals log (24-hr dedup, kept 7 days)
        if is_real_deal(combined) and not is_duplicate_in_log(hotel, location, deals_log, hours=24):
            append_to_log(DEALS_LOG, hotel, location, price, source, link)
            deals_log = load_log(DEALS_LOG)
            new_deals += 1
            print(f"  ✅ DEAL: {hotel[:50]} ({location}) — {price}")

    # Get display data
    recent_misprices = get_recent_from_log(misprice_log, hours=24)
    recent_deals     = get_recent_from_log(deals_log,    hours=168)  # 7 days

    print(f"\n📊 Last 24h misprices: {len(recent_misprices)}")
    print(f"📊 Last 7d deals:      {len(recent_deals)}")
    print(f"📊 New this run:       {new_misprices} misprices, {new_deals} deals\n")

    update_dashboard(recent_misprices, recent_deals)

    print(f"\n✓ Done. {new_misprices} new misprices, {new_deals} new deals this run.")
    print("=" * 55)


if __name__ == "__main__":
    main()
