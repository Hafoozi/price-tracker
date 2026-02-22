"""
Price Tracker - Main Script
Scrapes product prices and sends email alerts on price drops.
Also sends a weekly Sunday summary at 6:30 PM Eastern.
Credentials are read from environment variables (set via GitHub Secrets).
"""

import requests
from bs4 import BeautifulSoup
import csv
import os
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import time
import re

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
PRICE_LOG   = os.path.join(os.path.dirname(__file__), "price_history.csv")

def load_config():
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    config["email"]["sender_email"]    = os.environ.get("SENDER_EMAIL",    config["email"].get("sender_email", ""))
    config["email"]["app_password"]    = os.environ.get("APP_PASSWORD",    config["email"].get("app_password", ""))
    config["email"]["recipient_email"] = os.environ.get("RECIPIENT_EMAIL", config["email"].get("recipient_email", ""))
    return config

# ── Price Extraction ───────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def clean_price(raw: str) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None

def scrape_price(url: str, name: str) -> float | None:
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"  [ERROR] Could not fetch {name}: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    selectors = [
        {"name": "span", "class": re.compile(r"price", re.I)},
        {"name": "div",  "class": re.compile(r"price__current|product__price|ProductPrice", re.I)},
        {"name": "span", "class": re.compile(r"product-price|sale-price|current-price", re.I)},
        {"name": "p",    "class": re.compile(r"price", re.I)},
    ]

    for sel in selectors:
        tag_name = sel.pop("name")
        el = soup.find(tag_name, sel)
        if el:
            price = clean_price(el.get_text())
            if price and price > 0:
                print(f"  [OK] {name}: ${price:.2f}")
                return price

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                price_raw = offers.get("price") or offers.get("lowPrice")
                if price_raw:
                    price = clean_price(str(price_raw))
                    if price and price > 0:
                        print(f"  [OK via JSON-LD] {name}: ${price:.2f}")
                        return price
        except Exception:
            continue

    print(f"  [WARN] Could not extract price for {name}. Page structure may need tuning.")
    return None

# ── Price History (CSV) ────────────────────────────────────────────────────────

def read_last_price(name: str) -> float | None:
    if not os.path.exists(PRICE_LOG):
        return None
    with open(PRICE_LOG, "r", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["name"] == name]
    if not rows:
        return None
    return float(rows[-1]["price"])

def read_price_7days_ago(name: str) -> float | None:
    """Return the closest price recorded ~7 days ago, or None."""
    if not os.path.exists(PRICE_LOG):
        return None
    cutoff = datetime.now() - timedelta(days=7)
    with open(PRICE_LOG, "r", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["name"] == name]
    # Find rows from 7+ days ago, take the most recent one before the cutoff
    past_rows = [r for r in rows if datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S") <= cutoff]
    if not past_rows:
        return None
    return float(past_rows[-1]["price"])

def log_price(name: str, url: str, price: float):
    file_exists = os.path.exists(PRICE_LOG)
    with open(PRICE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "name", "price", "url"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": name,
            "price": f"{price:.2f}",
            "url": url,
        })

# ── Email Helpers ──────────────────────────────────────────────────────────────

def send_email(config: dict, subject: str, html: str):
    cfg = config["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["sender_email"]
    msg["To"]      = cfg["recipient_email"]
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(cfg["sender_email"], cfg["app_password"])
            server.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"\n  [EMAIL] Sent to {cfg['recipient_email']}")
    except Exception as e:
        print(f"\n  [EMAIL ERROR] {e}")

# ── Price Drop Alert ───────────────────────────────────────────────────────────

def send_alert(config: dict, alerts: list[dict]):
    if not alerts:
        return
    subject = f"🔔 Price Drop Alert — {len(alerts)} item(s) dropped!"
    rows = ""
    for a in alerts:
        rows += (
            f"<tr>"
            f"<td style='padding:8px;border:1px solid #ddd'>{a['name']}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#888;text-decoration:line-through'>${a['old_price']:.2f}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#2ecc71;font-weight:bold'>${a['new_price']:.2f}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#e74c3c'>-${a['drop']:.2f} ({a['pct']:.1f}%)</td>"
            f"<td style='padding:8px;border:1px solid #ddd'><a href='{a['url']}'>View Product</a></td>"
            f"</tr>"
        )
    html = f"""
    <html><body style='font-family:Arial,sans-serif;'>
    <h2 style='color:#2c3e50'>💰 Price Drop Alert</h2>
    <p>The following products dropped in price since your last check:</p>
    <table style='border-collapse:collapse;width:100%'>
      <thead>
        <tr style='background:#2c3e50;color:white'>
          <th style='padding:8px'>Product</th>
          <th style='padding:8px'>Old Price</th>
          <th style='padding:8px'>New Price</th>
          <th style='padding:8px'>Savings</th>
          <th style='padding:8px'>Link</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style='color:#888;font-size:12px;margin-top:20px'>
      Checked on {datetime.now().strftime("%B %d, %Y at %I:%M %p")}
    </p>
    </body></html>
    """
    send_email(config, subject, html)

# ── Weekly Sunday Summary ──────────────────────────────────────────────────────

def send_weekly_summary(config: dict, products: list[dict], current_prices: dict):
    """Send a full price summary for all tracked products."""
    print("\n  [SUNDAY] Building weekly summary email...")
    subject = f"📊 Weekly Price Summary — {datetime.now().strftime('%B %d, %Y')}"

    rows = ""
    for product in products:
        name = product["name"]
        url  = product["url"]
        current = current_prices.get(name)
        last_week = read_price_7days_ago(name)

        if current is None:
            current_str = "<em>unavailable</em>"
            change_str  = "—"
        else:
            current_str = f"${current:.2f}"
            if last_week is None:
                change_str = "<span style='color:#888'>No history yet</span>"
            elif current < last_week:
                diff = last_week - current
                pct  = (diff / last_week) * 100
                change_str = f"<span style='color:#2ecc71'>▼ ${diff:.2f} ({pct:.1f}%)</span>"
            elif current > last_week:
                diff = current - last_week
                pct  = (diff / last_week) * 100
                change_str = f"<span style='color:#e74c3c'>▲ ${diff:.2f} ({pct:.1f}%)</span>"
            else:
                change_str = "<span style='color:#888'>— No change</span>"

        rows += (
            f"<tr>"
            f"<td style='padding:8px;border:1px solid #ddd'><a href='{url}'>{name}</a></td>"
            f"<td style='padding:8px;border:1px solid #ddd;font-weight:bold'>{current_str}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'>{change_str}</td>"
            f"</tr>"
        )

    html = f"""
    <html><body style='font-family:Arial,sans-serif;'>
    <h2 style='color:#2c3e50'>📊 Weekly Price Summary</h2>
    <p>Here's the current status of all your tracked products:</p>
    <table style='border-collapse:collapse;width:100%'>
      <thead>
        <tr style='background:#2c3e50;color:white'>
          <th style='padding:8px;text-align:left'>Product</th>
          <th style='padding:8px;text-align:left'>Current Price</th>
          <th style='padding:8px;text-align:left'>vs Last Week</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style='color:#888;font-size:12px;margin-top:20px'>
      Summary generated on {datetime.now().strftime("%B %d, %Y at %I:%M %p")} · Tracker is running normally ✅
    </p>
    </body></html>
    """
    send_email(config, subject, html)

# ── Main Loop ──────────────────────────────────────────────────────────────────

def run(weekly: bool = False):
    config   = load_config()
    products = config["products"]
    alerts   = []
    current_prices = {}

    print(f"\n{'='*55}")
    print(f"  Price Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if weekly:
        print(f"  Mode: Weekly Summary")
    print(f"{'='*55}")

    for product in products:
        name = product["name"]
        url  = product["url"]
        print(f"\nChecking: {name}")

        new_price = scrape_price(url, name)
        current_prices[name] = new_price
        if new_price is None:
            continue

        old_price = read_last_price(name)
        log_price(name, url, new_price)

        if old_price is None:
            print(f"  [INFO] First time tracking. Baseline price set: ${new_price:.2f}")
        elif new_price < old_price:
            drop = old_price - new_price
            pct  = (drop / old_price) * 100
            print(f"  [DROP] ${old_price:.2f} → ${new_price:.2f}  (-${drop:.2f}, -{pct:.1f}%)")
            alerts.append({
                "name": name, "url": url,
                "old_price": old_price, "new_price": new_price,
                "drop": drop, "pct": pct,
            })
        else:
            print(f"  [NO CHANGE] Current price: ${new_price:.2f}  (was ${old_price:.2f})")

        time.sleep(2)

    if alerts:
        send_alert(config, alerts)
    else:
        print("\n  No price drops detected this run.")

    if weekly:
        send_weekly_summary(config, products, current_prices)

    print(f"\n{'='*55}\n")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Price Tracker")
    parser.add_argument("--test",    action="store_true", help="Send a test price drop email")
    parser.add_argument("--weekly",  action="store_true", help="Run in weekly summary mode")
    args = parser.parse_args()

    if args.test:
        config = load_config()
        print("\nSending test email...")
        fake_alerts = [
            {
                "name": "Turin DF83 Grinder - EspressoOutlet",
                "url": "https://espressooutlet.com/products/turin-df83-gen-2-coffee-espresso-grinder?variant=41773030113355",
                "old_price": 399.00,
                "new_price": 349.00,
                "drop": 50.00,
                "pct": 12.5,
            },
            {
                "name": "Lelit Bianca - CliveCoffee",
                "url": "https://clivecoffee.com/products/lelit-bianca-dual-boiler-espresso-machine?variant=31233815117912",
                "old_price": 2799.00,
                "new_price": 2599.00,
                "drop": 200.00,
                "pct": 7.1,
            },
        ]
        send_alert(config, fake_alerts)
        print("Done. Check your inbox at toadgranola+pricetrackerd@gmail.com")
    else:
        run(weekly=args.weekly)
