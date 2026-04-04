#!/usr/bin/env python3
"""Hotel Misprice Checker — Monitors for luxury hotel mistakes"""

import os
import json
import requests
from datetime import datetime, timedelta
import feedparser
import re

OUTPUT_DIR = os.getenv("OUTPUT_DIR", ".")
MISPRICE_LOG_FILE = os.path.join(OUTPUT_DIR, "misprice-log.txt")
DASHBOARD_FILE = os.path.join(OUTPUT_DIR, "luxury-hotel-deals-report.html")

SAFE_COUNTRIES = {
    "Turks and Caicos", "Barbados", "Cayman Islands", "Portugal", "Italy",
    "France", "Greece", "Spain", "Costa Rica", "Japan", "Singapore", "Maldives"
}

def load_misprice_log():
    """Load existing misprices from last 24 hours"""
    misprices = {}
    if os.path.exists(MISPRICE_LOG_FILE):
        try:
            with open(MISPRICE_LOG_FILE, 'r') as f:
                for line in f:
                    parts = line.strip().split("|")
                    if len(parts) >= 4:
                        key = f"{parts[0]}|{parts[1]}"
                        misprices[key] = {"timestamp": parts[3]}
        except:
            pass
    return misprices

def save_misprice(hotel, location, price, source):
    """Save new misprice to log"""
    timestamp = datetime.utcnow().isoformat() + "Z"
    try:
        with open(MISPRICE_LOG_FILE, 'a') as f:
            f.write(f"{hotel}|{location}|{price}|{timestamp}|{source}\n")
    except:
        pass

def check_secret_flying():
    """Check Secret Flying RSS for misprices"""
    misprices = []
    try:
        feed = feedparser.parse("https://www.secretflying.com/posts/category/hotel-star-rating/feed/")
        for entry in feed.entries[:5]:
            title = entry.get('title', '').lower()
            if any(country in title for country in SAFE_COUNTRIES) and any(star in title for star in ["5 star", "4 star"]):
                misprices.append({
                    "source": "Secret Flying",
                    "title": entry.get('title', ''),
                    "link": entry.get('link', ''),
                    "published": entry.get('published', '')
                })
    except:
        pass
    return misprices

def main():
    """Main check"""
    print("Checking for misprices...")
    misprices_log = load_misprice_log()
    misprices = check_secret_flying()
    print(f"Found {len(misprices)} potential misprices")

if __name__ == "__main__":
    main()
