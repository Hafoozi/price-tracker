"""
Price Tracker - Main Script
Scrapes product prices and images using bucket-based config structure.
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
from urllib.parse import urlparse, urljoin

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
PRICE_LOG   = os.path.join(os.path.dirname(__file__), "price_history.csv")

def load_config():
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    config["email"]["sender_email"]    = os.environ.get("SENDER_EMAIL",    config["email"].get("sender_email", ""))
    config["email"]["app_password"]    = os.environ.get("APP_PASSWORD",    config["email"].get("app_password", ""))
    config["email"]["recipient_email"] = os.environ.get("RECIPIENT_EMAIL", config["email"].get("recipient_email", ""))
    return config

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

def clean_image_url(img, page_url: str) -> str | None:
    if not img or not isinstance(img, str):
        return None
    img = img.strip()
    if not img:
        return None
    if img.startswith("//"):
        img = "https:" + img
    if not img.startswith("http"):
        img = urljoin(page_url, img)
    if img.startswith("http://"):
        img = "https://" + img[7:]
    try:
        parsed = urlparse(img)
        if not parsed.netloc:
            return None
    except Exception:
        return None
    return img

def scrape_product(url: str, label: str, retailer: str) -> dict:
    result = {"price": None, "image": None}
    name   = f"{label} - {retailer}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"  [ERROR] Could not fetch {name}: {e}")
        return result

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
                result["price"] = price
                break

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data  = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if result["price"] is None:
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0]
                    price_raw = offers.get("price") or offers.get("lowPrice")
                    if price_raw:
                        price = clean_price(str(price_raw))
                        if price and price > 0:
                            result["price"] = price
                if result["image"] is None:
                    img = item.get("image")
                    if isinstance(img, list):
                        img = img[0]
                    if isinstance(img, dict):
                        img = img.get("url")
                    cleaned = clean_image_url(img, url)
                    if cleaned:
                        result["image"] = cleaned
        except Exception:
            continue

    if result["image"] is None:
        og  = soup.find("meta", property="og:image")
        img = og.get("content", "") if og else ""
        cleaned = clean_image_url(img, url)
        if cleaned:
            result["image"] = cleaned

    if result["price"]:
        print(f"  [OK] {name}: ${result['price']:.2f}  image={'yes' if result['image'] else 'no'}")
    else:
        print(f"  [WARN] Could not extract price for {name}")
    return result

def read_last_price(label: str, retailer: str) -> float | None:
    name = f"{label} - {retailer}"
    if not os.path.exists(PRICE_LOG):
        return None
    with open(PRICE_LOG, "r", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["name"] == name]
    if not rows:
        return None
    return float(rows[-1]["price"])

def read_price_7days_ago(label: str, retailer: str) -> float | None:
    name   = f"{label} - {retailer}"
    cutoff = datetime.now() - timedelta(days=7)
    if not os.path.exists(PRICE_LOG):
        return None
    with open(PRICE_LOG, "r", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["name"] == name]
    past_rows = [r for r in rows if datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S") <= cutoff]
    if not past_rows:
        return None
    return float(past_rows[-1]["price"])

def log_price(label: str, retailer: str, url: str, price: float, image: str | None):
    name        = f"{label} - {retailer}"
    file_exists = os.path.exists(PRICE_LOG)
    with open(PRICE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "name", "price", "url", "image"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name":      name,
            "price":     f"{price:.2f}",
            "url":       url,
            "image":     image or "",
        })

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

def send_weekly_summary(config: dict, buckets: list[dict], current_prices: dict):
    print("\n  [SUNDAY] Building weekly summary email...")
    subject = f"📊 Weekly Price Summary — {datetime.now().strftime('%B %d, %Y')}"
    rows = ""
    for bucket in buckets:
        label = bucket["label"]
        for r in bucket["retailers"]:
            name      = f"{label} - {r['name']}"
            current   = current_prices.get(name)
            last_week = read_price_7days_ago(label, r["name"])
            current_str = f"${current:.2f}" if current else "<em>unavailable</em>"
            if current is None:
                change_str = "—"
            elif last_week is None:
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
                f"<td style='padding:8px;border:1px solid #ddd'>{name}</td>"
                f"<td style='padding:8px;border:1px solid #ddd;font-weight:bold'>{current_str}</td>"
                f"<td style='padding:8px;border:1px solid #ddd'>{change_str}</td>"
                f"</tr>"
            )
    html = f"""
    <html><body style='font-family:Arial,sans-serif;'>
    <h2 style='color:#2c3e50'>📊 Weekly Price Summary</h2>
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

def run(weekly: bool = False):
    config  = load_config()
    buckets = config["buckets"]
    alerts  = []
    current_prices = {}

    print(f"\n{'='*55}")
    print(f"  Price Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if weekly:
        print(f"  Mode: Weekly Summary")
    print(f"{'='*55}")

    for bucket in buckets:
        label = bucket["label"]
        print(f"\n── {label}")
        for retailer in bucket["retailers"]:
            rname = retailer["name"]
            url   = retailer["url"]
            name  = f"{label} - {rname}"
            print(f"  Checking {rname}...")

            result    = scrape_product(url, label, rname)
            new_price = result["price"]
            image     = result["image"]
            current_prices[name] = new_price

            if new_price is None:
                continue

            old_price = read_last_price(label, rname)
            log_price(label, rname, url, new_price, image)

            if old_price is None:
                print(f"    [INFO] Baseline set: ${new_price:.2f}")
            elif new_price < old_price:
                drop = old_price - new_price
                pct  = (drop / old_price) * 100
                print(f"    [DROP] ${old_price:.2f} → ${new_price:.2f} (-${drop:.2f}, -{pct:.1f}%)")
                alerts.append({
                    "name": name, "url": url,
                    "old_price": old_price, "new_price": new_price,
                    "drop": drop, "pct": pct,
                })
            else:
                print(f"    [NO CHANGE] ${new_price:.2f} (was ${old_price:.2f})")

            time.sleep(2)

    if alerts:
        send_alert(config, alerts)
    else:
        print("\n  No price drops detected this run.")

    if weekly:
        send_weekly_summary(config, buckets, current_prices)

    print(f"\n{'='*55}\n")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Price Tracker")
    parser.add_argument("--test",   action="store_true", help="Send a test price drop email")
    parser.add_argument("--weekly", action="store_true", help="Run in weekly summary mode")
    args = parser.parse_args()

    if args.test:
        config = load_config()
        print("\nSending test email...")
        fake_alerts = [
            {
                "name": "Turin DF83 Grinder - EspressoOutlet",
                "url":  "https://espressooutlet.com/products/turin-df83-gen-2-coffee-espresso-grinder?variant=41773030113355",
                "old_price": 399.00, "new_price": 349.00, "drop": 50.00, "pct": 12.5,
            },
            {
                "name": "Lelit Bianca - CliveCoffee",
                "url":  "https://clivecoffee.com/products/lelit-bianca-dual-boiler-espresso-machine?variant=31233815117912",
                "old_price": 2799.00, "new_price": 2599.00, "drop": 200.00, "pct": 7.1,
            },
        ]
        send_alert(config, fake_alerts)
        print("Done. Check your inbox at toadgranola+pricetrackerd@gmail.com")
    else:
        run(weekly=args.weekly)
