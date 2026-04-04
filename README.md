# Hotel Deals Automation

24/7 luxury hotel misprice monitoring via GitHub Actions.

- Checks Secret Flying, FlyerTalk, Fly4Free hourly
- Updates dashboard live
- Sends email alerts for new misprices
- Runs on GitHub's servers (no laptop needed)

## Files

- `misprice_checker.py` - Main script
- `.github/workflows/hourly-misprice-check.yml` - Automation trigger
- `luxury-hotel-deals-report.html` - Your live dashboard

## Setup

Push these files to GitHub, enable Actions, done. Runs 24/7 automatically.
