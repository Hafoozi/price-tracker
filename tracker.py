"""
Price Tracker - Main Script
Hourly scraper with once-per-day email alerts and bucket-based config.
"""

import requests
from bs4 import BeautifulSoup
import csv, os, smtplib, json, time, re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin

CONFIG_FILE   = os.path.join(os.path.dirname(__file__), "config.json")
PRICE_LOG     = os.path.join(os.path.dirname(__file__), "price_history.csv")
ALERTED_FILE  = os.path.join(os.path.dirname(__file__), "last_alerted.json")

# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    config["email"]["sender_email"]    = os.environ.get("SENDER_EMAIL",    config["email"].get("sender_email", ""))
    config["email"]["app_password"]    = os.environ.get("APP_PASSWORD",    config["email"].get("app_password", ""))
    config["email"]["recipient_email"] = os.environ.get("RECIPIENT_EMAIL", config["email"].get("recipient_email", ""))
    return config

# ── Alert tracking (once per day per product) ─────────────────────────────────
def load_alerted() -> dict:
    if not os.path.exists(ALERTED_FILE):
        return {}
    with open(ALERTED_FILE, "r") as f:
        return json.load(f)

def save_alerted(data: dict):
    # Prune entries older than 2 days to prevent stale suppression
    cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in data.items() if v >= cutoff}
    with open(ALERTED_FILE, "w") as f:
        json.dump(pruned, f, indent=2)

def already_alerted_today(alerted: dict, name: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    return alerted.get(name) == today

def mark_alerted(alerted: dict, name: str):
    alerted[name] = datetime.now().strftime("%Y-%m-%d")

# ── HTTP / Scraping ───────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def clean_price(raw: str):
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None

def clean_image_url(img, page_url: str):
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


def is_out_of_stock(soup, url: str) -> bool:
    """
    Precision OOS detection — avoids false positives from multi-variant pages.

    Strategy: Only use signals that are SPECIFIC to the requested variant:
      1. JSON-LD structured data availability field (most reliable)
      2. Disabled primary add-to-cart / buy button
      3. product:availability meta tag

    We deliberately avoid scanning page text (span/div/p) because Shopify and
    most retailers show ALL variant availability on one page — a sold-out size
    or color will appear as "Sold Out" text even when the selected variant is
    in stock.
    """
    # Extract variant ID from URL if present (e.g. ?variant=41773030113355)
    variant_id = None
    vm = re.search(r'[?&](?:variant|sku_id|sku)=([\w\-]+)', url)
    if vm:
        variant_id = vm.group(1)

    # 1. JSON-LD structured data — check all offers, prefer variant-matched one
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                # Collect all offers including ProductGroup/hasVariant pattern
                all_offers = []
                direct = item.get("offers")
                if isinstance(direct, dict):
                    all_offers.append(direct)
                elif isinstance(direct, list):
                    all_offers += [o for o in direct if isinstance(o, dict)]
                for variant in (item.get("hasVariant") or []):
                    if not isinstance(variant, dict):
                        continue
                    v_offer = variant.get("offers")
                    if isinstance(v_offer, dict):
                        all_offers.append(v_offer)
                    elif isinstance(v_offer, list):
                        all_offers += [o for o in v_offer if isinstance(o, dict)]

                # If variant ID in URL, only check matching offers; else check all
                if variant_id:
                    matched = [o for o in all_offers if variant_id in o.get("url", "")]
                    candidate_offers = matched if matched else all_offers
                else:
                    candidate_offers = all_offers

                oos_signals = ["OutOfStock", "SoldOut", "Discontinued", "BackOrder"]
                for offer in candidate_offers:
                    avail = offer.get("availability", "")
                    if any(x in avail for x in oos_signals):
                        return True
                    if "InStock" in avail:
                        return False
        except Exception:
            pass

    # 2. product:availability meta tag (Facebook/OpenGraph — set by retailer explicitly)
    meta_avail = soup.find("meta", {"property": "product:availability"}) or \
                 soup.find("meta", {"name": "availability"})
    if meta_avail:
        val = (meta_avail.get("content") or "").strip().lower()
        if val in ("out of stock", "oos", "sold out", "backorder", "preorder"):
            return True
        if val in ("in stock", "instock", "available"):
            return False

    # 3. Disabled primary add-to-cart / buy button
    # Only count buttons whose text is specifically a purchase action
    add_to_cart_texts = {"add to cart", "add to bag", "buy now", "purchase", "checkout"}
    for btn in soup.find_all("button", limit=100):
        if btn.get("disabled") is None:
            continue
        btn_text = btn.get_text(strip=True).lower()
        # Strip common prefixes/suffixes to get the core action
        btn_text_clean = re.sub(r'[\-–—].*$', '', btn_text).strip()
        if btn_text_clean in add_to_cart_texts:
            return True

    return False

HEADERS_MOBILE = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def scrape_product(url: str, label: str, retailer: str) -> dict:
    result = {"price": None, "image": None, "oos": False}
    name   = f"{label} - {retailer}"

    # Try desktop UA first, fall back to mobile UA if blocked (403/429/503)
    response = None
    for hdrs in [HEADERS, HEADERS_MOBILE]:
        try:
            r = requests.get(url, headers=hdrs, timeout=15)
            if r.status_code == 200:
                response = r
                break
            elif r.status_code in (403, 429, 503):
                print(f"  [WARN] {name}: HTTP {r.status_code}, retrying with alternate UA...")
                time.sleep(3)
            else:
                r.raise_for_status()
        except requests.RequestException as e:
            print(f"  [ERROR] {name}: {e}")
            return result

    if response is None:
        print(f"  [ERROR] {name}: blocked on all attempts (403/429/503)")
        return result

    soup = BeautifulSoup(response.text, "html.parser")

    # ── Step 1: Try JSON-LD structured data first (most reliable, reflects actual price) ──
    # We do this BEFORE HTML scraping because HTML often has compare-at (crossed-out)
    # prices in the first price element, which would give us the wrong higher number.
    variant_id = None
    vm = re.search(r'[?&](?:variant|sku_id|sku)=([\w\-]+)', url)
    if vm:
        variant_id = vm.group(1)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data  = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                # ── Collect all offers from this item ──
                # Handles three structures:
                #   1. Standard: item.offers (object or list)
                #   2. ProductGroup: item.hasVariant[].offers (JSACoffee pattern)
                all_offers = []
                direct_offers = item.get("offers")
                if isinstance(direct_offers, dict):
                    all_offers.append(direct_offers)
                elif isinstance(direct_offers, list):
                    all_offers += [o for o in direct_offers if isinstance(o, dict)]
                # ProductGroup pattern — offers nested inside hasVariant
                for variant in (item.get("hasVariant") or []):
                    if not isinstance(variant, dict):
                        continue
                    v_offer = variant.get("offers")
                    if isinstance(v_offer, dict):
                        all_offers.append(v_offer)
                    elif isinstance(v_offer, list):
                        all_offers += [o for o in v_offer if isinstance(o, dict)]

                if result["price"] is None and all_offers:
                    # First pass: try to match variant ID if we have one
                    matched_offers = []
                    if variant_id:
                        matched_offers = [o for o in all_offers if variant_id in o.get("url", "")]
                    # Fall back to all offers if no variant match
                    candidate_offers = matched_offers if matched_offers else all_offers
                    for offer in candidate_offers:
                        price_raw = offer.get("price") or offer.get("lowPrice")
                        if price_raw:
                            price = clean_price(str(price_raw))
                            if price and price > 0:
                                result["price"] = price
                                break

                if result["image"] is None:
                    img = item.get("image")
                    if isinstance(img, list): img = img[0]
                    if isinstance(img, dict): img = img.get("url")
                    cleaned = clean_image_url(img, url)
                    if cleaned: result["image"] = cleaned
        except Exception:
            continue

    # ── Step 2: HTML fallback — only if JSON-LD gave us nothing ──
    # Explicitly skip compare-at / was / original price elements (these are crossed-out prices)
    COMPARE_AT_PATTERN = re.compile(
        r"compare[_\-]?at|was[_\-]?price|original[_\-]?price|price[_\-]?was|"
        r"price--compare|price__compare|crossed|strikethrough|line-through",
        re.I
    )
    if result["price"] is None:
        selectors = [
            {"tag": "span", "class": re.compile(r"price__sale|sale[_\-]?price|current[_\-]?price|price--sale", re.I)},
            {"tag": "div",  "class": re.compile(r"price__current|product__price|ProductPrice", re.I)},
            {"tag": "span", "class": re.compile(r"product-price|current-price", re.I)},
        ]
        for sel in selectors:
            el = soup.find(sel["tag"], {"class": sel["class"]})
            if el:
                # Skip if this element or any parent looks like a compare-at container
                skip = False
                for ancestor in [el] + el.parents:
                    cls = " ".join(ancestor.get("class", []))
                    if COMPARE_AT_PATTERN.search(cls):
                        skip = True
                        break
                if skip:
                    continue
                price = clean_price(el.get_text())
                if price and price > 0:
                    result["price"] = price
                    break

    # Last resort: broad price span, but explicitly exclude compare-at elements
    if result["price"] is None:
        for el in soup.find_all("span", {"class": re.compile(r"price", re.I)}):
            cls = " ".join(el.get("class", []))
            if COMPARE_AT_PATTERN.search(cls):
                continue
            # Also skip if it has a <s> or <del> parent (visually crossed out)
            if el.find_parent(["s", "del"]) or el.name in ["s", "del"]:
                continue
            price = clean_price(el.get_text())
            if price and price > 0:
                result["price"] = price
                break

    if result["image"] is None:
        og  = soup.find("meta", property="og:image")
        img = og.get("content", "") if og else ""
        cleaned = clean_image_url(img, url)
        if cleaned: result["image"] = cleaned

    # Check for out-of-stock signals
    result["oos"] = is_out_of_stock(soup, url)
    status = f"${result['price']:.2f}" if result["price"] else "NO PRICE"
    oos_tag = " [OOS]" if result["oos"] else ""
    print(f"  [{'OK' if result['price'] else 'WARN'}] {name}: {status}{oos_tag}  img={'yes' if result['image'] else 'no'}")
    return result

# ── CSV ───────────────────────────────────────────────────────────────────────
FIELDS = ["timestamp", "name", "price", "url", "image", "oos"]

def ensure_csv_header():
    if not os.path.exists(PRICE_LOG):
        with open(PRICE_LOG, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()
        return
    with open(PRICE_LOG, "r", newline="") as f:
        first_line = f.readline().strip()
    if first_line != ",".join(FIELDS):
        with open(PRICE_LOG, "r", newline="") as f:
            rows = list(csv.DictReader(f))
        with open(PRICE_LOG, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in FIELDS})
        print("  [INFO] CSV migrated to 5-column format")

def read_last_price(label: str, retailer: str):
    name = f"{label} - {retailer}"
    if not os.path.exists(PRICE_LOG): return None
    with open(PRICE_LOG, "r", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["name"] == name]
    return float(rows[-1]["price"]) if rows else None

def read_price_7days_ago(label: str, retailer: str):
    name   = f"{label} - {retailer}"
    cutoff = datetime.now() - timedelta(days=7)
    if not os.path.exists(PRICE_LOG): return None
    with open(PRICE_LOG, "r", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["name"] == name]
    past = [r for r in rows if datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S") <= cutoff]
    return float(past[-1]["price"]) if past else None

def log_price(label: str, retailer: str, url: str, price: float, image: str | None, oos: bool = False):
    with open(PRICE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name":      f"{label} - {retailer}",
            "price":     f"{price:.2f}",
            "url":       url,
            "image":     image or "",
            "oos":       "1" if oos else "",
        })

# ── Email ─────────────────────────────────────────────────────────────────────
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
        print(f"\n  [EMAIL] Sent: {subject}")
    except Exception as e:
        print(f"\n  [EMAIL ERROR] {e}")

def send_staleness_alert(config: dict, stale_products: list[str], hours: int):
    """Send a single email if any product hasn't been updated in over `hours` hours."""
    subject = f"⚠️ Price Tracker — Data Stale ({hours}h+)"
    items = "".join(f"<li style='padding:4px 0'>{p}</li>" for p in stale_products)
    html = f"""<html><body style='font-family:Arial,sans-serif'>
    <h2 style='color:#e74c3c'>⚠️ Stale Price Data Detected</h2>
    <p>The following products have not been updated in over <strong>{hours} hours</strong>,
    which may indicate a scraper failure:</p>
    <ul style='line-height:1.8'>{items}</ul>
    <p>Check your <a href='https://github.com/Hafoozi/price-tracker/actions'>GitHub Actions logs</a>
    for errors.</p>
    <p style='color:#888;font-size:12px;margin-top:20px'>
      Checked on {datetime.now().strftime("%B %d, %Y at %I:%M %p")}
    </p>
    </body></html>"""
    send_email(config, subject, html)

def send_alert(config: dict, alerts: list[dict]):
    if not alerts: return
    subject = f"🔔 Price Drop Alert — {len(alerts)} item(s) dropped!"
    rows = ""
    for a in alerts:
        rows += (
            f"<tr>"
            f"<td style='padding:8px;border:1px solid #ddd'>{a['name']}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#888;text-decoration:line-through'>${a['old_price']:.2f}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#2ecc71;font-weight:bold'>${a['new_price']:.2f}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#e74c3c'>-${a['drop']:.2f} ({a['pct']:.1f}%)</td>"
            f"<td style='padding:8px;border:1px solid #ddd'><a href='{a['url']}'>View</a></td>"
            f"</tr>"
        )
    html = f"""<html><body style='font-family:Arial,sans-serif'>
    <h2 style='color:#2c3e50'>💰 Price Drop Alert</h2>
    <table style='border-collapse:collapse;width:100%'>
      <thead><tr style='background:#2c3e50;color:white'>
        <th style='padding:8px'>Product</th><th style='padding:8px'>Old</th>
        <th style='padding:8px'>New</th><th style='padding:8px'>Savings</th><th style='padding:8px'>Link</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style='color:#888;font-size:12px;margin-top:20px'>Checked on {datetime.now().strftime("%B %d, %Y at %I:%M %p")}</p>
    </body></html>"""
    send_email(config, subject, html)

def send_weekly_summary(config: dict, buckets: list, current_prices: dict):
    print("\n  [WEEKLY] Building summary email...")
    subject = f"📊 Weekly Price Summary — {datetime.now().strftime('%B %d, %Y')}"
    rows = ""
    for bucket in buckets:
        label = bucket["label"]
        for r in bucket["retailers"]:
            name      = f"{label} - {r['name']}"
            current   = current_prices.get(name)
            last_week = read_price_7days_ago(label, r["name"])
            cur_str   = f"${current:.2f}" if current else "<em>unavailable</em>"
            if current is None:             chg = "—"
            elif last_week is None:         chg = "<span style='color:#888'>No history</span>"
            elif current < last_week:
                d = last_week - current
                chg = f"<span style='color:#2ecc71'>▼ ${d:.2f} ({d/last_week*100:.1f}%)</span>"
            elif current > last_week:
                d = current - last_week
                chg = f"<span style='color:#e74c3c'>▲ ${d:.2f} ({d/last_week*100:.1f}%)</span>"
            else:                           chg = "<span style='color:#888'>No change</span>"
            rows += f"<tr><td style='padding:8px;border:1px solid #ddd'>{name}</td><td style='padding:8px;border:1px solid #ddd;font-weight:bold'>{cur_str}</td><td style='padding:8px;border:1px solid #ddd'>{chg}</td></tr>"
    html = f"""<html><body style='font-family:Arial,sans-serif'>
    <h2 style='color:#2c3e50'>📊 Weekly Price Summary</h2>
    <table style='border-collapse:collapse;width:100%'>
      <thead><tr style='background:#2c3e50;color:white'>
        <th style='padding:8px;text-align:left'>Product</th>
        <th style='padding:8px;text-align:left'>Current</th>
        <th style='padding:8px;text-align:left'>vs Last Week</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style='color:#888;font-size:12px;margin-top:20px'>
      {datetime.now().strftime("%B %d, %Y at %I:%M %p")} · Tracker running normally ✅
    </p></body></html>"""
    send_email(config, subject, html)

# ── Main ──────────────────────────────────────────────────────────────────────
def run(weekly: bool = False):
    ensure_csv_header()
    config  = load_config()
    buckets = config["buckets"]
    alerted = load_alerted()
    alerts  = []
    current_prices = {}

    print(f"\n{'='*55}")
    print(f"  Price Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if weekly: print("  Mode: Weekly Summary")
    print(f"{'='*55}")

    for bucket in buckets:
        label = bucket["label"]
        print(f"\n── {label}")
        for retailer in bucket["retailers"]:
            rname = retailer["name"]
            url   = retailer["url"]
            name  = f"{label} - {rname}"
            print(f"  Checking {rname}...")

            try:
                result    = scrape_product(url, label, rname)
                new_price = result["price"]
                image     = result["image"]
                oos       = result["oos"]
                current_prices[name] = new_price if not oos else None

                if new_price is None:
                    continue

                # Always log the price (even OOS — dashboard shows it with OOS tag)
                old_price = read_last_price(label, rname)
                log_price(label, rname, url, new_price, image, oos)

                if oos:
                    print(f"    [OOS] ${new_price:.2f} — item sold out / unavailable, no alert triggered")
                    continue

                if old_price is None:
                    print(f"    [INFO] Baseline: ${new_price:.2f}")
                elif new_price < old_price:
                    drop = old_price - new_price
                    pct  = (drop / old_price) * 100
                    print(f"    [DROP] ${old_price:.2f} → ${new_price:.2f} (-${drop:.2f}, -{pct:.1f}%)")
                    if not already_alerted_today(alerted, name):
                        alerts.append({"name": name, "url": url, "old_price": old_price, "new_price": new_price, "drop": drop, "pct": pct})
                        mark_alerted(alerted, name)
                    else:
                        print(f"    [SKIP] Already alerted today for {name}")
                else:
                    print(f"    [OK] ${new_price:.2f} (was ${old_price:.2f})")

            except Exception as e:
                print(f"    [ERROR] Unexpected error scraping {name}: {e}")

            time.sleep(2)

    if alerts:
        send_alert(config, alerts)
    else:
        print("\n  No new alerts this run.")

    if weekly:
        send_weekly_summary(config, buckets, current_prices)

    save_alerted(alerted)

    # ── Staleness check — alert if any product hasn't logged data in 24h ──
    STALE_HOURS = 24
    stale = []
    if os.path.exists(PRICE_LOG):
        with open(PRICE_LOG, "r", newline="") as f:
            all_rows = list(csv.DictReader(f))
        cutoff_ts = datetime.now() - timedelta(hours=STALE_HOURS)
        # Build set of all tracked product names from config
        tracked = {f"{b['label']} - {r['name']}" for b in buckets for r in b["retailers"]}
        for name in tracked:
            product_rows = [r for r in all_rows if r["name"] == name]
            if not product_rows:
                stale.append(f"{name} (no data yet)")
            else:
                latest_ts = datetime.strptime(product_rows[-1]["timestamp"], "%Y-%m-%d %H:%M:%S")
                if latest_ts < cutoff_ts:
                    hours_ago = int((datetime.now() - latest_ts).total_seconds() / 3600)
                    stale.append(f"{name} (last seen {hours_ago}h ago)")
    if stale:
        print(f"\n  [STALE] {len(stale)} product(s) have data older than {STALE_HOURS}h — sending alert")
        send_staleness_alert(config, stale, STALE_HOURS)
    else:
        print(f"\n  [OK] All products have fresh data (within {STALE_HOURS}h)")

    print(f"\n{'='*55}\n")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",   action="store_true")
    parser.add_argument("--weekly", action="store_true")
    args = parser.parse_args()
    if args.test:
        config = load_config()
        send_alert(config, [{"name": "Test Product - TestStore", "url": "https://example.com", "old_price": 399.00, "new_price": 349.00, "drop": 50.00, "pct": 12.5}])
        print("Test email sent.")
    else:
        run(weekly=args.weekly)
