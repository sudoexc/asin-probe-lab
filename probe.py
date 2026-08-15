#!/usr/bin/env python3
"""Amazon price-visibility probe from GitHub Actions runners (Azure US egress).

Simulates the production load of one polling cycle: 53 product-page fetches
(5 real ASINs cycled), parses price / availability / seller / geo marker,
detects captcha / interstitial / degraded pages, retries on failure,
and writes machine-readable results for drift analysis.
"""
import json
import os
import random
import re
import time
import html as html_mod
from datetime import datetime, timezone

import requests

ASINS = [
    "B0FQFB8FMG",  # AirPods Pro 3
    "B0GR19X8DC",  # MacBook Air 13" M5
    "B0DZ7871B8",  # iPad 11" A16
    "B0GQVBJT4J",  # iPad Air 11" M4
    "B0FQF5BZ8Z",  # Apple Watch Series 11 46mm
]
CYCLE_SIZE = 53          # simulate full production cycle volume
MAX_RETRIES = 2
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

RE_PRICE_JSON = re.compile(r'"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)')
RE_PRICE_OFFSCREEN = re.compile(r'class="a-offscreen">\$([0-9,]+(?:\.[0-9]{2})?)<')
RE_GLOW = re.compile(r'glow-ingress-line2[^>]*>\s*([^<]{0,80}?)\s*<', re.S)
RE_AVAIL = re.compile(r'id="availability"(.{0,600}?)</div>', re.S)
RE_TAGS = re.compile(r'<[^>]+>')
RE_SOLD_BY = re.compile(r'(?:Sold by|"merchantName"\s*:\s*")\s*(?:</span>)?(.{0,300}?)(?:"|</)', re.S)


def classify(status, body):
    if status == 503 or "Sorry! Something went wrong" in body:
        return "dogs_503"
    if "/errors/validateCaptcha" in body or "Type the characters you see" in body:
        return "captcha"
    if "Continue shopping" in body and len(body) < 40000:
        return "interstitial"
    if status != 200:
        return f"http_{status}"
    return "page"


def parse(body):
    out = {}
    m = RE_PRICE_JSON.search(body)
    if m:
        out["price"] = float(m.group(1))
        out["price_src"] = "priceAmount"
    else:
        m = RE_PRICE_OFFSCREEN.search(body)
        if m:
            out["price"] = float(m.group(1).replace(",", ""))
            out["price_src"] = "a-offscreen"
    m = RE_GLOW.search(body)
    if m:
        out["glow"] = html_mod.unescape(m.group(1).strip())
    m = RE_AVAIL.search(body)
    if m:
        txt = RE_TAGS.sub(" ", m.group(1))
        out["availability"] = " ".join(txt.split())[:120]
    m = RE_SOLD_BY.search(body)
    if m:
        txt = RE_TAGS.sub(" ", m.group(1))
        out["seller"] = " ".join(txt.split())[:80]
    out["has_buybox"] = ('id="add-to-cart-button"' in body
                         or '"buyingOptionType":"NEW"' in body)
    return out


def fetch_one(asin):
    """Fetch one ASIN with retries, fresh session each attempt (models
    production: each ASIN hit once per cycle, no cookie carryover)."""
    row = {"asin": asin, "attempts": 0}
    for attempt in range(1 + MAX_RETRIES):
        sess = requests.Session()
        row["attempts"] = attempt + 1
        t0 = time.time()
        try:
            r = sess.get(f"https://www.amazon.com/dp/{asin}?th=1&psc=1",
                         headers=HEADERS, timeout=30)
            status, body = r.status_code, r.text
        except Exception as e:
            row.update(status=0, kind=f"exc:{type(e).__name__}", bytes=0)
            time.sleep(2 + attempt * 3)
            continue
        row["ms"] = int((time.time() - t0) * 1000)
        row["status"] = status
        row["bytes"] = len(body)
        kind = classify(status, body)
        row["kind"] = kind
        if kind == "page":
            row.update(parse(body))
            if "price" in row:
                row["ok"] = True
                return row
            # 200 but no price -> degraded page; retry
            row["ok"] = False
        time.sleep(2 + attempt * 3 + random.random() * 2)
    row.setdefault("ok", False)
    return row


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    trigger = os.environ.get("GITHUB_EVENT_NAME", "manual")

    # who are we / where are we
    try:
        ipinfo = requests.get("https://ipinfo.io/json", timeout=15).json()
    except Exception as e:
        ipinfo = {"error": str(e)}
    print(f"RUNNER IP: {json.dumps(ipinfo)}", flush=True)

    plan = []
    while len(plan) < CYCLE_SIZE:
        batch = ASINS[:]
        random.shuffle(batch)
        plan.extend(batch)
    plan = plan[:CYCLE_SIZE]

    rows = []
    t_start = time.time()
    for i, asin in enumerate(plan):
        row = fetch_one(asin)
        row["i"] = i
        rows.append(row)
        price = row.get("price", "-")
        print(f"[{i+1:02d}/{CYCLE_SIZE}] {asin} status={row.get('status')} "
              f"kind={row.get('kind')} bytes={row.get('bytes')} "
              f"price={price} attempts={row['attempts']}", flush=True)
        time.sleep(0.8 + random.random() * 1.2)

    elapsed = int(time.time() - t_start)
    n_ok = sum(1 for r in rows if r.get("ok"))
    n_captcha = sum(1 for r in rows if r.get("kind") == "captcha")
    n_503 = sum(1 for r in rows if r.get("kind") == "dogs_503")
    n_inter = sum(1 for r in rows if r.get("kind") == "interstitial")
    tot_attempts = sum(r["attempts"] for r in rows)
    summary = {
        "ts": ts, "run_id": run_id, "trigger": trigger,
        "ip": ipinfo.get("ip"), "ip_city": ipinfo.get("city"),
        "ip_region": ipinfo.get("region"), "ip_country": ipinfo.get("country"),
        "ip_org": ipinfo.get("org"),
        "cycle_size": CYCLE_SIZE, "ok": n_ok,
        "captcha": n_captcha, "dogs_503": n_503, "interstitial": n_inter,
        "attempts_total": tot_attempts,
        "attempts_per_asin": round(tot_attempts / CYCLE_SIZE, 2),
        "elapsed_s": elapsed,
        "coverage_pct": round(100 * n_ok / CYCLE_SIZE, 1),
        "prices": {a: next((r.get("price") for r in rows
                            if r["asin"] == a and r.get("ok")), None)
                   for a in ASINS},
        "glow": next((r.get("glow") for r in rows if r.get("glow")), None),
    }
    print("SUMMARY: " + json.dumps(summary), flush=True)

    os.makedirs("results", exist_ok=True)
    day = ts[:10]
    with open(f"results/cycles_{day}.jsonl", "a") as f:
        f.write(json.dumps({"summary": summary, "rows": rows}) + "\n")
    with open("results/summary.jsonl", "a") as f:
        f.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    main()
