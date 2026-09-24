#!/usr/bin/env python3
"""av.by Car Monitor — GitHub Actions edition.
Uses Playwright (Chromium) to scrape cars.av.by.
Sends fresh deals to Telegram.
"""

import os, json, re, time, sqlite3, asyncio, sys
from datetime import datetime
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "5795308229")
MAX_BYN = 35000       # max price in BYN
PAGES = 5             # first 5 pages
DB_FILE = Path("avby_seen.db")

# ─── Helpers ─────────────────────────────────────────────────────────────
def to_byn(t: str) -> float:
    """Parse '12 500 ₽' or '25 000 руб.' → numeric value in BYN"""
    try:
        t = t.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
        if "$" in t or "USD" in t:
            v = float(re.sub(r"[^\d.]", "", t.replace("$", "").replace("USD", "")))
            return round(v * 2.58)
        elif "€" in t or "EUR" in t:
            v = float(re.sub(r"[^\d.]", "", t.replace("€", "").replace("EUR", "")))
            return round(v * 2.95)
        elif "₽" in t or "руб" in t.lower():
            return float(re.sub(r"[^\d.]", "", t))
        else:
            return float(re.sub(r"[^\d.]", "", t))
    except:
        return 0

def parse_age(text: str) -> float:
    """'5 минут назад' → hours, '2 часа' → 2, 'день' → 24"""
    t = text.lower().strip()
    m = re.search(r"(\d+)\s*минут", t)
    if m: return int(m.group(1)) / 60
    m = re.search(r"(\d+)\s*час", t)
    if m: return int(m.group(1))
    m = re.search(r"(\d+)\s*дн", t)
    if m: return int(m.group(1)) * 24
    if "вчера" in t: return 24
    if "только что" in t: return 0
    return 999

def is_junk(title: str, params: str) -> bool:
    txt = (title + " " + params).lower()
    for kw in ["запчаст", "бит", "авари", "не на ходу", "поврежден", "тотал",
               "разбит", "на запчасти", "ремонт", "неисправн", "разбор",
               "мотоцикл", "мопед", "скутер", "квадроцикл"]:
        if kw in txt: return True
    return False

def send_tg(text: str, photo_url: str = None) -> bool:
    """Send message or photo to Telegram, returns True on success"""
    if not TG_TOKEN:
        print("No TG_TOKEN, skipping Telegram")
        return False
    import requests
    text = text[:1024]
    if photo_url:
        try:
            r = requests.get(photo_url, timeout=15)
            if r.status_code == 200 and len(r.content) > 1000:
                r2 = requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    data={"chat_id": TG_CHAT, "caption": text, "parse_mode": "HTML"},
                    files={"photo": ("img.jpg", r.content, "image/jpeg")},
                    timeout=20,
                )
                if r2.ok:
                    return True
        except:
            pass
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True},
            timeout=10,
        )
        if r.ok:
            return True
        print(f"  TG sendMessage error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  TG sendMessage exception: {e}")
    return False

# ─── Scraper ─────────────────────────────────────────────────────────────
async def run():
    from playwright.async_api import async_playwright

    print(f"av.by check at {datetime.now().strftime('%H:%M')}")

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled"]
            )
        except Exception:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled"]
            )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )
        page = await ctx.new_page()

        fresh = []
        for pg in range(1, PAGES + 1):
            print(f"  Page {pg}...")
            await page.goto(f"https://cars.av.by/filter?page={pg}",
                           wait_until="domcontentloaded", timeout=25000)

            # Wait for listings to render (av.by loads async)
            try:
                await page.wait_for_selector(".listing-item", timeout=8000)
                await page.wait_for_timeout(500)
            except:
                print(f"  [PAGE {pg}] no .listing-item found")
                await page.wait_for_timeout(2000)
                items = await page.query_selector_all(".listing-item")
                if not items:
                    print(f"  [PAGE {pg}] still empty, skipping")
                    continue

            items = await page.query_selector_all(".listing-item")
            print(f"  [PAGE {pg}] {len(items)} items")

            for item in items:
                try:
                    title_el = await item.query_selector(".listing-item__title")
                    if not title_el:
                        print(f"  [SKIP] no title element")
                        continue
                    title = (await title_el.inner_text()).strip()

                    params_el = await item.query_selector(".listing-item__params")
                    params = (await params_el.inner_text()).strip() if params_el else ""

                    date_el = await item.query_selector(".listing-item__date")
                    ds = (await date_el.inner_text()).strip() if date_el else ""
                    hr = parse_age(ds)
                    if hr > 12:
                        print(f"  [SKIP] {title[:40]} — old {ds}")
                        continue
                    if is_junk(title, params):
                        print(f"  [SKIP] {title[:40]} — junk")
                        continue

                    link_el = await item.query_selector(".listing-item__link")
                    link = ""
                    if link_el:
                        link = (await link_el.get_attribute("href")) or ""
                        if link and not link.startswith("http"):
                            link = "https://cars.av.by" + link
                    ad_id = link.split("/")[-1] if "/" in link else ""

                    price_el = await item.query_selector(".listing-item__price-primary")
                    price_text = (await price_el.inner_text()).strip() if price_el else ""
                    if not price_el:
                        print(f"  [WARN] {title[:40]} — NO price element found")
                    price_byn = to_byn(price_text)
                    if price_byn > MAX_BYN:
                        print(f"  [SKIP] {title[:40]} — {price_byn} BYN > MAX")
                        continue

                    yr_m = re.search(r"(\d{4})\s*г", params)
                    yr = int(yr_m.group(1)) if yr_m else 0
                    if yr > 0 and yr < 1995: continue

                    loc_el = await item.query_selector(".listing-item__location")
                    location = (await loc_el.inner_text()).strip() if loc_el else ""

                    # Get thumbnail photo from listing card
                    photo = None
                    img_el = await item.query_selector(".listing-item__photo img")
                    if img_el:
                        src = (await img_el.get_attribute("data-src")) or (await img_el.get_attribute("src")) or ""
                        if "avcdn" in src:
                            photo = src.replace("/advertpreview/", "/advertmedium/").replace("/advertbig/", "/advertmedium/")

                    fresh.append({
                        "id": ad_id, "title": title, "link": link,
                        "price_text": price_text, "price_byn": price_byn,
                        "params": params, "year": yr, "location": location,
                        "ago": ds, "hours": hr, "photo": photo,
                    })
                    print(f"  [{len(fresh)}] {title[:40]} — {price_text} — {ds}")
                except Exception as e:
                    print(f"  [ERR] item parse: {e}")
                    continue

            await asyncio.sleep(0.5)

        await browser.close()

    # ─── Dedup & Send ────────────────────────────────────────────────────
    conn = sqlite3.connect(str(DB_FILE))
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, sent_at TEXT)")
    cur.execute("DELETE FROM seen WHERE sent_at < datetime('now', '-7 days')")
    conn.commit()

    sent = 0
    for ad in fresh:
        cur.execute("SELECT 1 FROM seen WHERE id=?", (ad["id"],))
        if cur.fetchone():
            print(f"  [SKIP] {ad['title'][:40]} — already seen")
            continue

        # --- Deal scoring based on price and year ---
        price = ad.get("price_byn", 0)
        year = ad.get("year", 0)

        score = 30
        if price < 5000: score += 20
        elif price < 10000: score += 15
        elif price < 15000: score += 10
        elif price < 20000: score += 5

        if year >= 2020: score += 20
        elif year >= 2015: score += 15
        elif year >= 2010: score += 10
        elif year >= 2005: score += 5

        if score < 30:
            print(f"  [SKIP] {ad['title'][:40]} — score {score}/100")
            continue

        # Build message
        if score >= 60:
            emoji = "🔥"
        elif score >= 45:
            emoji = "✅"
        else:
            emoji = "⚡"
        loc = ad.get("location", "")
        p = ad.get("params", "")[:60]
        age = ad.get("ago", "")

        text = (
            f"{emoji} <b>{ad['title']}</b>\n"
            f"💰 {ad['price_text']}\n"
            f"📅 {ad['year']} | {p}\n"
            f"⏱ {age} | 📍 {loc}\n"
            f"🔗 <a href='{ad['link']}'>Open av.by</a>"
        )

        if send_tg(text, ad.get("photo")):
            cur.execute("INSERT INTO seen VALUES (?, ?)", (ad["id"], datetime.now().isoformat()))
            conn.commit()
            sent += 1
            print(f"  [{sent}] SENT {ad['title'][:40]}")
        else:
            print(f"  [SKIP] {ad['title'][:40]} — TG send failed")
        time.sleep(0.6)

    conn.close()
    print(f"Sent: {sent} fresh deal(s)")
    if sent == 0:
        print("Nothing new")

if __name__ == "__main__":
    asyncio.run(run())