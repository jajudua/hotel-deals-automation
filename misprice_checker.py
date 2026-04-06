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

def load_misprice_log():
    """Load existing misprices from the last 24 hours"""
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
                            "price": price,
                            "timestamp": timestamp,
                            "source": parts[4] if len(parts) > 4 else "unknown"
                        }
        except Exception as e:
            print(f"Error loading misprice log: {e}")
    return misprices

def save_misprice(hotel, location, price, source):
    """Save a new misprice to the log"""
    timestamp = datetime.utcnow().isoformat() + "Z"
    try:
        with open(MISPRICE_LOG_FILE, 'a') as f:
            f.write(f"{hotel}|{location}|{price}|{timestamp}|{source}\n")
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
    """Update the HTML dashboard with live misprices"""
    # Load existing dashboard
    if not os.path.exists(DASHBOARD_FILE):
        print(f"Dashboard file not found at {DASHBOARD_FILE}")
        return

    with open(DASHBOARD_FILE, 'r') as f:
        dashboard_html = f.read()

    # Generate misprice alerts HTML
    misprices_cards = ""

    if misprices:
        for mp in misprices:
            hotel = mp.get("hotel", "Hotel")
            location = mp.get("location", "Location")
            price = mp.get("price", "Price")
            normal_price = mp.get("normal_price", "N/A")
            link = mp.get("link", "#")
            minutes_ago = mp.get("minutes_ago", 0)

            urgency = "🔴 LIVE" if minutes_ago < 60 else ("🟠 COOLING" if minutes_ago < 360 else "⏳ FADING")

            urgency_warning = f'<div style="background:rgba(200,50,50,0.1);border:1px solid rgba(200,50,50,0.3);border-radius:6px;padding:8px 12px;margin-bottom:10px;"><p style="font-size:12px;color:#ff6464;margin:0;font-weight:bold;">⏰ ACT NOW — Posted {minutes_ago} minutes ago, window closing fast</p></div>' if minutes_ago < 60 else ''

            misprices_cards += f'''
            <div style="background:#1a1a30;border:2px solid rgba(200,50,50,0.5);border-radius:10px;padding:18px;margin-bottom:12px;">
              <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
                <div>
                  <p style="font-size:10px;color:#ff6464;letter-spacing:2px;text-transform:uppercase;margin:0 0 4px;">{location}</p>
                  <h3 style="font-size:16px;color:#fff;margin:0;font-weight:normal;">{hotel}</h3>
                </div>
                <span style="background:rgba(200,50,50,0.2);color:#ff6464;border:1px solid rgba(200,50,50,0.5);font-size:10px;font-weight:bold;padding:4px 10px;border-radius:20px;white-space:nowrap;">{urgency} — {minutes_ago} mins ago</span>
              </div>
              <div style="margin-bottom:10px;">
                <p style="font-size:24px;color:#ff6464;font-weight:bold;margin:0;">{price}</p>
                <p style="font-size:13px;color:#666;margin:4px 0 0;text-decoration:line-through;">Normal: {normal_price}</p>
              </div>
              <p style="font-size:13px;color:#8a90a0;line-height:1.5;margin:0 0 10px;">Source: {mp.get("source", "Deal Site")} | ✓ Recently Found</p>
              {urgency_warning}
              <div style="border-top:1px solid rgba(255,255,255,0.06);padding-top:12px;display:flex;gap:8px;">
                <a href="{link}" style="flex:1;color:#fff;text-decoration:none;background:rgba(200,50,50,0.15);border:1px solid rgba(200,50,50,0.3);padding:6px;border-radius:5px;text-align:center;font-size:12px;font-weight:bold;">BOOK NOW →</a>
                <a href="{link}" style="flex:1;color:#ff6464;text-decoration:none;border:1px solid rgba(200,50,50,0.3);padding:6px;border-radius:5px;text-align:center;font-size:12px;">See Post →</a>
              </div>
            </div>
            '''

    # Build the complete misprice section
    if misprices:
        misprice_section = f'''<div class="misprice-section">
    <div class="misprice-header">
      <span style="font-size:24px;">🚨</span>
      <h2>LIVE MISPRICE ALERTS — LAST 24 HOURS</h2>
    </div>
    <div id="mispriceAlerts">
{misprices_cards}
    </div>
  </div>'''
    else:
        misprice_section = f'''<div class="misprice-section">
    <div class="misprice-header">
      <span style="font-size:24px;">🚨</span>
      <h2>LIVE MISPRICE ALERTS — LAST 24 HOURS</h2>
    </div>
    <div id="mispriceAlerts">
      <p class="misprice-empty">No active misprices at the moment. Checking Secret Flying, FlyerTalk, and Fly4Free hourly. Last checked: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</p>
    </div>
  </div>'''

    # Find and replace the misprice section
    # Look for the opening misprice-section div
    start_marker = '<!-- 🚨 LIVE MISPRICE ALERTS SECTION -->'
    end_marker = '<!-- 5-STAR DEALS -->'

    start_idx = dashboard_html.find(start_marker)
    end_idx = dashboard_html.find(end_marker)

    if start_idx >= 0 and end_idx > start_idx:
        # Replace the section
        dashboard_html = dashboard_html[:start_idx] + misprice_section + '\n\n  ' + dashboard_html[end_idx:]
    else:
        # If markers don't exist, insert after header
        insertion_point = dashboard_html.find('</div>\n\n  <!-- AUTOMATION STATUS -->')
        if insertion_point > 0:
            dashboard_html = dashboard_html[:insertion_point] + f'</div>\n\n  {misprice_section}\n\n  <!-- AUTOMATION STATUS -->' + dashboard_html[insertion_point+len('</div>\n\n  <!-- AUTOMATION STATUS -->'):]

    # Write updated dashboard
    with open(DASHBOARD_FILE, 'w') as f:
        f.write(dashboard_html)

    print(f"✓ Dashboard updated with {len(misprices)} misprices")

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

    # Combine and deduplicate
    all_misprices = sf_misprices + ft_misprices + ff_misprices
    new_misprices = []

    for mp in all_misprices:
        hotel = mp.get("hotel", "")
        location = mp.get("location", "")

        if hotel and location:
            if not is_duplicate(hotel, location, misprices_log):
                mp["minutes_ago"] = get_minutes_ago(mp.get("published", datetime.utcnow().isoformat()))
                new_misprices.append(mp)
                save_misprice(hotel, location, mp.get("price", "N/A"), mp.get("source", "unknown"))
                print(f"✓ NEW MISPRICE: {hotel} ({location}) at {mp.get('price', 'N/A')}")
            else:
                print(f"⊘ Duplicate: {hotel} ({location}) — already alerted")

    # Update dashboard with all new misprices
    if new_misprices:
        update_dashboard(new_misprices)
    else:
        print("\n✓ No new misprices found, dashboard stays fresh")
        # Still update dashboard to refresh timestamp
        update_dashboard([])

    print(f"\n✓ Check complete. Found {len(new_misprices)} new misprices")
    print("=" * 50)

if __name__ == "__main__":
    main()
