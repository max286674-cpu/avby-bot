#!/usr/bin/env python3
"""av.by Car Monitor — GitHub Actions edition.
Uses requests + BeautifulSoup to scrape cars.av.by.
Sends fresh deals to Telegram.
"""

import os, re, time, sqlite3
from datetime import datetime
from pathlib import Path

import requests as req
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "5795308229")
MAX_BYN = 35000
PAGES = 5
DB_FILE = Path("avby_seen.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,*/*;q=0.9",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://cars.av.by/filter",
}

# ─── Helpers ─────────────────────────────────────────────────────────────
def to_byn(t: str) -> float:
    t = t.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    if not t:
        return 0.0
    v = float(re.sub(r"[^\d.]", "", t))
    if "$" in t:
        return v * 2.58
    if "€" in t:
        return v * 2.95
    return v


def parse_age(text: str) -> float:
    t = text.lower().strip()
    if not t:
        return 999
    m = re.search(r"(\d+)\s*минут", t)
    if m:
        return int(m.group(1)) / 60
    m = re.search(r"(\d+)\s*час", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*дн", t)
    if m:
        return int(m.group(1)) * 24
    if "вчера" in t:
        return 24
    if "только что" in t:
        return 0
    return 999


def is_junk(title: str, params: str) -> bool:
    txt = (title + " " + params).lower()
    for kw in [
        "запчаст", "бит", "авари", "не на ходу", "поврежден", "тотал",
        "разбит", "на запчасти", "ремонт", "неисправн", "разбор",
        "мотоцикл", "мопед", "скутер", "квадроцикл",
    ]:
        if kw in txt:
            return True
    return False


def send_tg(text: str, photo_url: str = None) -> bool:
    if not TG_TOKEN:
        print("  [TG] No TG_TOKEN")
        return False
    text = text[:1024]
    if photo_url:
        try:
            r = req.get(photo_url, timeout=15)
            if r.status_code == 200 and len(r.content) > 1000:
                r2 = req.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    data={"chat_id": TG_CHAT, "caption": text, "parse_mode": "HTML"},
                    files={"photo": ("img.jpg", r.content, "image/jpeg")},
                    timeout=20,
                )
                if r2.ok:
                    return True
        except Exception:
            pass
    try:
        r = req.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.ok:
            return True
        print(f"  [TG] {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  [TG] {e}")
    return False


def extract_photo(soup_item) -> str | None:
    img = soup_item.select_one("img")
    if not img:
        return None
    src = img.get("data-src") or img.get("src") or ""
    if "avcdn" in src:
        return src.replace("/advertpreview/", "/advertmedium/")
    return None


# ─── Main ────────────────────────────────────────────────────────────────
def run():
    print(f"av.by check at {datetime.now().strftime('%H:%M')}")

    session = req.Session()
    session.headers.update(HEADERS)

    fresh = []

    for pg in range(1, PAGES + 1):
        print(f"Page {pg}...")
        try:
            resp = session.get(
                f"https://cars.av.by/filter?page={pg}", timeout=25
            )
            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code}")
                continue
        except Exception as e:
            print(f"  request: {e}")
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select(".listing-item")
        if not items:
            print(f"  no listings (page {pg})")
            continue

        for it in items:
            try:
                link_el = it.select_one(".listing-item__link")
                if not link_el:
                    continue
                href = link_el.get("href", "")
                ad_id = href.split("/")[-1] if "/" in href else ""
                link = (
                    f"https://cars.av.by{href}"
                    if href.startswith("/")
                    else href
                )

                title = link_el.get_text(strip=True)

                # Price
                price_el = it.select_one(".listing-item__price-primary")
                price_text = price_el.get_text(strip=True) if price_el else ""
                price_byn = to_byn(price_text)
                if price_byn > MAX_BYN:
                    continue

                # Params
                params_el = it.select_one(".listing-item__params")
                params = params_el.get_text(strip=True) if params_el else ""

                # Year
                yr = 0
                ym = re.search(r"(\d{4})\s*г", params)
                if ym:
                    yr = int(ym.group(1))
                if yr > 0 and yr < 1995:
                    continue

                # Age
                date_el = it.select_one(".listing-item__date")
                ds = date_el.get_text(strip=True) if date_el else ""
                hr = parse_age(ds)
                if hr > 12:
                    continue

                # Junk
                if is_junk(title, params):
                    continue

                # Location
                loc_el = it.select_one(".listing-item__location")
                location = loc_el.get_text(strip=True) if loc_el else ""

                # Photo
                photo = extract_photo(it)

                fresh.append(
                    {
                        "id": ad_id,
                        "title": title,
                        "link": link,
                        "price_text": price_text,
                        "price_byn": price_byn,
                        "params": params,
                        "year": yr,
                        "location": location,
                        "ago": ds,
                        "photo": photo,
                    }
                )
                print(f"  [{len(fresh)}] {title[:40]} — {price_text} — {ds}")
            except Exception as e:
                print(f"  [ERR] {e}")
                continue

        time.sleep(0.3)

    # ─── Dedup ────────────────────────────────────────────────────────────────
    conn = sqlite3.connect(str(DB_FILE))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, sent_at TEXT)"
    )
    cur.execute(
        "DELETE FROM seen WHERE sent_at < datetime('now', '-7 days')"
    )
    conn.commit()

    sent = 0
    for ad in fresh:
        cur.execute("SELECT 1 FROM seen WHERE id=?", (ad["id"],))
        if cur.fetchone():
            print(f"  [SKIP] {ad['title'][:40]} — seen")
            continue

        # ─── Scoring ─────────────────────────────────────────────────────────
        price = ad.get("price_byn", 0)
        year = ad.get("year", 0)

        score = 30
        if price < 5000:
            score += 20
        elif price < 10000:
            score += 15
        elif price < 15000:
            score += 10
        elif price < 20000:
            score += 5

        if year >= 2020:
            score += 20
        elif year >= 2015:
            score += 15
        elif year >= 2010:
            score += 10
        elif year >= 2005:
            score += 5

        if score < 30:
            print(f"  [SKIP] {ad['title'][:40]} — score {score}")
            continue

        # ─── Send ─────────────────────────────────────────────────────────────
        if score >= 60:
            emoji = "\U0001f525"  # 🔥
        elif score >= 45:
            emoji = "\u2705"      # ✅
        else:
            emoji = "\u26a1"      # ⚡

        text = (
            f"{emoji} <b>{ad['title']}</b>\n"
            f"\U0001f4b0 {ad['price_text']}\n"
            f"\U0001f4c5 {ad['year']} | {ad['params'][:60]}\n"
            f"\u23f1 {ad['ago']} | \U0001f4cd {ad['location']}\n"
            f"\U0001f517 <a href='{ad['link']}'>Open av.by</a>"
        )

        if send_tg(text, ad.get("photo")):
            cur.execute(
                "INSERT INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
            sent += 1
            print(f"  [{sent}] SENT {ad['title'][:40]}")
        else:
            print(f"  [SKIP] {ad['title'][:40]} — TG fail")
        time.sleep(0.6)

    conn.close()
    print(f"Sent: {sent} fresh deal(s)")
    if sent == 0:
        print("Nothing new")


if __name__ == "__main__":
    run()