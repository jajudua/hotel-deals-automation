#!/usr/bin/env python3
"""
Hotel Misprice Checker — COMPLETE VERSION
Monitors Secret Flying, FlyerTalk, Fly4Free for luxury hotel mistakes
Runs hourly via GitHub Actions, updates dashboard and sends alerts
"""

import os
import json
import requests
from datetime import datetime, timedelta
from urllib.parse import urljoin
import feedparser
import re
from bs4 import BeautifulSoup

# Configuration
OUTPUT_DIR = os.getenv("OUTPUT_DIR", ".")
MISPRICE_LOG_FILE = os.path.join(OUTPUT_DIR, "misprice-log.txt")
DASHBOARD_FILE = os.path.join(OUTPUT_DIR, "luxury-hotel-deals-report.html")
GMAIL_TOKEN = os.getenv("GMAIL_TOKEN", "")

# Safe destinations (Level 1 + 2 countries only)
SAFE_COUNTRIES = {
    "Turks and Caicos", "Turks & Caicos", "Barbados", "Cayman Islands", "Antigua",
    "Portugal", "Italy", "France", "Greece", "Spain", "Croatia", "Switzerland", "Monaco",
    "Costa Rica", "Belize", "Panama",
    "Japan", "Singapore", "Maldives", "Thailand", "Bali", "Dubai", "UAE",
    "Canada", "USA", "Mexico"
}

STAR_KEYWORDS = ["5 star", "5-star", "luxury", "four star", "4 star", "4-star", "premium"]

# Must contain at least one of these to count as a real misprice/deal
MISPRICE_KEYWORDS = [
    "mistake", "misprice", "mistake rate", "error rate", "pricing error",
    "glitch", "bug rate", "accidental", "sale", "flash sale",
    "% off", "percent off", "discount", "deal", "offer",
    "cheap", "mistake fare", "error fare", "from £", "from $", "from €",
    "award rate", "reward night", "free night", "points rate"
]

# Noise to exclude — blog articles, opinion pieces, general news
EXCLUDE_KEYWORDS = [
    "credit card", "amex", "american express", "points strategy",
    "how to earn", "tier points", "status match", "mobile check-in",
    "joins star alliance", "leaves marriott", "acquisition", "review:",
    "interview", "opinion", "guide to", "best credit", "earn miles",
    "lounge review", "flight review", "transfer bonus"
]

def load_misprice_log():
    """Load existing misprices for deduplication"""
    misprices = {}
    if os.path.exists(MISPRICE_LOG_FILE):
        try:
            with open(MISPRICE_LOG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("|")
                    if len(parts) >= 4:
                        hotel, location, price, timestamp = parts[0], parts[1], parts[2], parts[3]
                        key = f"{hotel}|{location}"
                        misprices[key] = {
                            "hotel": hotel,
                            "location": location,
                            "price": price,
                            "timestamp": timestamp,
                            "source": parts[4] if len(parts) > 4 else "unknown",
                            "link": parts[5] if len(parts) > 5 else "#"
                        }
        except Exception as e:
            print(f"Error loading misprice log: {e}")
    return misprices


def get_all_recent_misprices(misprices_log):
    """Get all misprices from the last 24 hours for dashboard display"""
    recent = []
    now = datetime.utcnow()
    for key, data in misprices_log.items():
        try:
            ts = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            age = now.replace(tzinfo=ts.tzinfo) - ts
            if age < timedelta(hours=24):
                mins = int(age.total_seconds() / 60)
                recent.append({
                    "hotel": data["hotel"],
                    "location": data["location"],
                    "price": data["price"],
                    "normal_price": "N/A",
                    "source": data["source"],
                    "link": data.get("link", "#"),
                    "minutes_ago": mins
                })
        except:
            pass
    # Sort by most recent first
    recent.sort(key=lambda x: x["minutes_ago"])
    return recent

def save_misprice(hotel, location, price, source, link="#"):
    """Save a new misprice to the log"""
    timestamp = datetime.utcnow().isoformat() + "Z"
    try:
        with open(MISPRICE_LOG_FILE, 'a') as f:
            f.write(f"{hotel}|{location}|{price}|{timestamp}|{source}|{link}\n")
    except Exception as e:
        print(f"Error saving misprice: {e}")

def is_duplicate(hotel, location, misprices_log):
    """Check if this misprice was already alerted on in the last 6 hours"""
    key = f"{hotel}|{location}"
    if key not in misprices_log:
        return False

    timestamp_str = misprices_log[key]["timestamp"]
    try:
        timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if datetime.utcnow().replace(tzinfo=timestamp.tzinfo) - timestamp < timedelta(hours=6):
            return True
    except:
        pass
    return False

def check_secret_flying_rss():
    """Monitor Secret Flying RSS feed for new hotel misprices"""
    misprices = []
    try:
        feed = feedparser.parse("https://www.secretflying.com/posts/category/hotel-star-rating/feed/")
        for entry in feed.entries[:15]:  # Check last 15 posts
            title = entry.get('title', '').lower()
            published = entry.get('published', '')
            link = entry.get('link', '')
            summary = entry.get('summary', '')

            # Check if recent (last 4 hours)
            try:
                pub_time = datetime.strptime(published[:19], "%Y-%m-%dT%H:%M:%S")
                if datetime.utcnow() - pub_time > timedelta(hours=4):
                    continue
            except:
                pass

            # Look for hotel mentions and prices
            if any(star in title for star in STAR_KEYWORDS):
                # Extract price if present
                price_match = re.search(r'[£$€][\d,]+', title)
                location = ""
                for country in SAFE_COUNTRIES:
                    if country.lower() in title:
                        location = country
                        break

                if price_match and location:
                    # Extract hotel name (usually first part of title)
                    hotel_name = title.split("|")[0].strip() if "|" in title else title[:50]

                    misprices.append({
                        "hotel": hotel_name,
                        "location": location,
                        "price": price_match.group(),
                        "normal_price": "N/A",
                        "source": "Secret Flying",
                        "link": link,
                        "published": published
                    })
    except Exception as e:
        print(f"Error checking Secret Flying RSS: {e}")

    return misprices

def check_flyertalk():
    """Monitor FlyerTalk Hotel Deals forum"""
    misprices = []
    try:
        response = requests.get(
            "https://www.flyertalk.com/forum/hotel-deals/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            # Look for thread titles with misprice keywords
            threads = soup.find_all('a', class_='thread-title')
            for thread in threads[:10]:
                title = thread.get_text(strip=True).lower()
                link = thread.get('href', '')

                if any(keyword in title for keyword in ['mistake', 'error', 'misprice', 'bug']):
                    if any(star in title for star in STAR_KEYWORDS):
                        location = ""
                        for country in SAFE_COUNTRIES:
                            if country.lower() in title:
                                location = country
                                break

                        if location:
                            price_match = re.search(r'[£$€][\d,]+', title)
                            misprices.append({
                                "hotel": title[:60],
                                "location": location,
                                "price": price_match.group() if price_match else "Check thread",
                                "normal_price": "N/A",
                                "source": "FlyerTalk",
                                "link": "https://www.flyertalk.com" + link if link.startswith('/') else link,
                                "published": datetime.utcnow().isoformat() + "Z"
                            })
    except Exception as e:
        print(f"Error checking FlyerTalk: {e}")

    return misprices

def check_fly4free():
    """Monitor Fly4Free for hotel mistakes"""
    misprices = []
    try:
        response = requests.get(
            "https://www.fly4free.com/flight-deals/hotel-mistake-rate/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            # Look for articles with deal keywords
            articles = soup.find_all('article')
            for article in articles[:10]:
                title = article.get_text(strip=True).lower()
                link = article.find('a')
                link = link.get('href', '') if link else ''

                if any(keyword in title for keyword in ['hotel', '4-star', '5-star', 'luxury']):
                    location = ""
                    for country in SAFE_COUNTRIES:
                        if country.lower() in title:
                            location = country
                            break

                    if location and any(star in title for star in STAR_KEYWORDS):
                        price_match = re.search(r'[£$€][\d,]+', title)
                        misprices.append({
                            "hotel": title[:60],
                            "location": location,
                            "price": price_match.group() if price_match else "Check deal",
                            "normal_price": "N/A",
                            "source": "Fly4Free",
                            "link": link if link.startswith('http') else "https://www.fly4free.com",
                            "published": datetime.utcnow().isoformat() + "Z"
                        })
    except Exception as e:
        print(f"Error checking Fly4Free: {e}")

    return misprices

def get_minutes_ago(timestamp_str):
    """Calculate minutes since timestamp"""
    try:
        timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        delta = datetime.utcnow().replace(tzinfo=timestamp.tzinfo) - timestamp
        return int(delta.total_seconds() / 60)
    except:
        return 0

def update_dashboard(misprices):
    """Update the HTML dashboard using string replacement (preserves formatting)"""
    if not os.path.exists(DASHBOARD_FILE):
        print(f"Dashboard file not found at {DASHBOARD_FILE}")
        return

    with open(DASHBOARD_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # --- Build misprice cards HTML ---
    if misprices:
        cards = ""
        for mp in misprices:
            hotel    = mp.get("hotel", "Hotel")
            location = mp.get("location", "Location")
            price    = mp.get("price", "Price")
            link     = mp.get("link", "#")
            source   = mp.get("source", "Deal Site")
            mins     = mp.get("minutes_ago", 0)
            urgency  = "LIVE" if mins < 60 else ("COOLING" if mins < 360 else "FADING")
            color    = "#ff6464" if mins < 60 else ("#ff9944" if mins < 360 else "#888")

            cards += f'''<div class="misprice-card">
      <div class="misprice-location">{location}</div>
      <h4>{hotel}</h4>
      <div class="misprice-price">{price}</div>
      <div class="misprice-time">{urgency} — {mins} mins ago · Source: {source}</div>
      <a class="misprice-link" href="{link}">BOOK NOW →</a> <a class="misprice-link" href="{link}">See Post →</a>
    </div>
    '''
        alerts_content = cards
    else:
        alerts_content = f'<p class="misprice-empty">No active misprices at the moment. Checking 12 sources hourly. Last checked: {now_str}</p>'

    # --- Replace content inside <div id="mispriceAlerts"> ... </div> ---
    pattern = r'(<div id="mispriceAlerts">)(.*?)(</div>)'
    match = re.search(pattern, html, re.DOTALL)
    if match:
        html = html[:match.start(2)] + '\n    ' + alerts_content + '\n    ' + html[match.end(2):]
        print(f"✓ Injected {len(misprices)} misprice cards into #mispriceAlerts")
    else:
        print("⚠ Could not find <div id=\"mispriceAlerts\"> in dashboard HTML")

    # --- Replace content inside <div id="mispriceLogo"> ... </div> (activity log) ---
    if misprices:
        log_entries = ""
        for mp in misprices:
            log_entries += f'''<div class="log-entry">
      <span class="log-hotel">{mp.get("hotel","Hotel")} — {mp.get("location","")}</span>
      <span class="log-time">{mp.get("price","N/A")} · {mp.get("source","?")} · {mp.get("minutes_ago",0)} mins ago</span>
    </div>
    '''
        log_content = log_entries
    else:
        log_content = f'<p class="log-empty">No misprices spotted yet. Last checked: {now_str}</p>'

    log_pattern = r'(<div id="mispriceLogo">)(.*?)(</div>)'
    log_match = re.search(log_pattern, html, re.DOTALL)
    if log_match:
        html = html[:log_match.start(2)] + '\n    ' + log_content + '\n    ' + html[log_match.end(2):]
        print(f"✓ Updated activity log")

    # --- Write back (unchanged except for injected sections) ---
    with open(DASHBOARD_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"✓ Dashboard updated with {len(misprices)} misprices at {now_str}")

def is_real_deal(combined):
    """Returns True only if text contains misprice/deal signals and no noise keywords"""
    has_deal_signal = any(kw in combined for kw in MISPRICE_KEYWORDS)
    has_noise = any(kw in combined for kw in EXCLUDE_KEYWORDS)
    return has_deal_signal and not has_noise


def scrape_rss_feed(url, source_name, require_price=False):
    """Generic RSS feed scraper with consistent filtering"""
    misprices = []
    try:
        feed = feedparser.parse(url)
        print(f"   {source_name}: {len(feed.entries)} entries in feed")
        for entry in feed.entries[:25]:
            title = entry.get('title', '')
            title_lower = title.lower()
            summary = entry.get('summary', '').lower()
            combined = title_lower + ' ' + summary
            link = entry.get('link', '')
            published = entry.get('published', '')

            # Must be hotel related
            is_hotel = any(kw in combined for kw in STAR_KEYWORDS + [
                'hotel', 'resort', 'suite', 'palace', 'villa', 'hyatt', 'marriott',
                'hilton', 'aman', 'four seasons', 'ritz', 'accor', 'ihg', 'radisson'
            ])
            if not is_hotel:
                continue

            # Must be a real deal, not a blog article
            if not is_real_deal(combined):
                continue

            location = "International"
            for country in SAFE_COUNTRIES:
                if country.lower() in combined:
                    location = country
                    break

            price_match = re.search(r'[£$€]\s*[\d,]+', title)
            price = price_match.group().replace(' ', '') if price_match else "See post"

            if require_price and not price_match:
                continue

            misprices.append({
                "hotel": title[:80],
                "location": location,
                "price": price,
                "normal_price": "N/A",
                "source": source_name,
                "link": link,
                "published": published or datetime.utcnow().isoformat()
            })
    except Exception as e:
        print(f"   Error checking {source_name}: {e}")
    return misprices


def check_head_for_points():
    return scrape_rss_feed("https://www.headforpoints.com/feed/", "Head for Points")

def check_view_from_the_wing():
    return scrape_rss_feed("https://viewfromthewing.com/feed/", "View from the Wing")

def check_one_mile_at_a_time():
    return scrape_rss_feed("https://onemileatatime.com/feed/", "One Mile at a Time")

def check_holiday_pirates():
    return scrape_rss_feed("https://www.holidaypirates.com/feeds/deals.rss", "Holiday Pirates")

def check_the_points_guy():
    """The Points Guy — major US travel site, covers hotel mistake rates"""
    return scrape_rss_feed("https://thepointsguy.com/feed/", "The Points Guy")

def check_frequent_miler():
    """Frequent Miler — US blog, excellent hotel misprice coverage"""
    return scrape_rss_feed("https://frequentmiler.com/feed/", "Frequent Miler")

def check_doctor_of_credit():
    """Doctor of Credit — catches pricing errors quickly"""
    return scrape_rss_feed("https://www.doctorofcredit.com/feed/", "Doctor of Credit")

def check_miles_to_memories():
    """Miles to Memories — US travel deals blog"""
    return scrape_rss_feed("https://milestomemories.com/feed/", "Miles to Memories")


def check_reddit_travel():
    """Monitor Reddit travel communities for hotel misprices"""
    misprices = []
    subreddits = ["TravelHacks", "deals", "shoestring", "awardtravel"]
    try:
        for sub in subreddits:
            response = requests.get(
                f"https://www.reddit.com/r/{sub}/new.json?limit=25",
                headers={"User-Agent": "HotelMispriceBot/1.0"},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                posts = data.get('data', {}).get('children', [])
                print(f"   Reddit r/{sub}: {len(posts)} posts")
                for post in posts:
                    d = post.get('data', {})
                    title = d.get('title', '')
                    title_lower = title.lower()
                    text = d.get('selftext', '').lower()
                    combined = title_lower + ' ' + text
                    reddit_link = f"https://reddit.com{d.get('permalink', '')}"

                    is_hotel = any(kw in combined for kw in STAR_KEYWORDS + ['hotel', 'resort', 'suite', 'palace'])
                    if not is_hotel or not is_real_deal(combined):
                        continue

                    location = "International"
                    for country in SAFE_COUNTRIES:
                        if country.lower() in combined:
                            location = country
                            break

                    price_match = re.search(r'[£$€]\s*[\d,]+', title)
                    price = price_match.group().replace(' ', '') if price_match else "See post"

                    misprices.append({
                        "hotel": title[:80],
                        "location": location,
                        "price": price,
                        "normal_price": "N/A",
                        "source": f"Reddit r/{sub}",
                        "link": reddit_link,
                        "published": datetime.utcnow().isoformat() + "Z"
                    })
    except Exception as e:
        print(f"   Error checking Reddit: {e}")
    return misprices


def main():
    """Main execution"""
    print("=" * 50)
    print("Hotel Misprice Checker — COMPLETE VERSION")
    print("=" * 50)
    print(f"Running at {datetime.utcnow().isoformat()}Z")

    # Load existing misprices
    misprices_log = load_misprice_log()
    print(f"Loaded {len(misprices_log)} misprices from log")

    # Check all sources
    print("\n📡 Checking Secret Flying RSS...")
    sf_misprices = check_secret_flying_rss()
    print(f"   Found {len(sf_misprices)} entries from Secret Flying")

    print("📡 Checking FlyerTalk...")
    ft_misprices = check_flyertalk()
    print(f"   Found {len(ft_misprices)} entries from FlyerTalk")

    print("📡 Checking Fly4Free...")
    ff_misprices = check_fly4free()
    print(f"   Found {len(ff_misprices)} entries from Fly4Free")

    print("📡 Checking Head for Points...")
    hfp_misprices = check_head_for_points()
    print(f"   Found {len(hfp_misprices)} entries from Head for Points")

    print("📡 Checking View from the Wing...")
    vftw_misprices = check_view_from_the_wing()
    print(f"   Found {len(vftw_misprices)} entries from View from the Wing")

    print("📡 Checking One Mile at a Time...")
    omat_misprices = check_one_mile_at_a_time()
    print(f"   Found {len(omat_misprices)} entries from One Mile at a Time")

    print("📡 Checking Holiday Pirates...")
    hp_misprices = check_holiday_pirates()
    print(f"   Found {len(hp_misprices)} entries from Holiday Pirates")

    print("📡 Checking The Points Guy...")
    tpg_misprices = check_the_points_guy()
    print(f"   Found {len(tpg_misprices)} entries from The Points Guy")

    print("📡 Checking Frequent Miler...")
    fm_misprices = check_frequent_miler()
    print(f"   Found {len(fm_misprices)} entries from Frequent Miler")

    print("📡 Checking Doctor of Credit...")
    doc_misprices = check_doctor_of_credit()
    print(f"   Found {len(doc_misprices)} entries from Doctor of Credit")

    print("📡 Checking Miles to Memories...")
    mtm_misprices = check_miles_to_memories()
    print(f"   Found {len(mtm_misprices)} entries from Miles to Memories")

    print("📡 Checking Reddit Travel communities...")
    reddit_misprices = check_reddit_travel()
    print(f"   Found {len(reddit_misprices)} entries from Reddit")

    # Combine and deduplicate
    all_misprices = (sf_misprices + ft_misprices + ff_misprices +
                     hfp_misprices + vftw_misprices + omat_misprices +
                     hp_misprices + tpg_misprices + fm_misprices +
                     doc_misprices + mtm_misprices + reddit_misprices)
    new_misprices = []

    for mp in all_misprices:
        hotel = mp.get("hotel", "")
        location = mp.get("location", "")

        if hotel and location:
            if not is_duplicate(hotel, location, misprices_log):
                mp["minutes_ago"] = get_minutes_ago(mp.get("published", datetime.utcnow().isoformat()))
                new_misprices.append(mp)
                save_misprice(hotel, location, mp.get("price", "N/A"), mp.get("source", "unknown"), mp.get("link", "#"))
                print(f"✓ NEW MISPRICE: {hotel} ({location}) at {mp.get('price', 'N/A')}")
            else:
                print(f"⊘ Duplicate: {hotel} ({location}) — already alerted")

    # Reload log to get ALL misprices (old + new), then show last 24hrs on dashboard
    updated_log = load_misprice_log()
    all_recent = get_all_recent_misprices(updated_log)
    print(f"\n📊 Total misprices in last 24 hours: {len(all_recent)}")
    update_dashboard(all_recent)

    print(f"✓ Check complete. Found {len(new_misprices)} new misprices this run")
    print("=" * 50)

if __name__ == "__main__":
    main()
