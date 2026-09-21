#!/usr/bin/env python3
"""av.by Car Monitor — GitHub Actions edition.
Uses Playwright (Chromium) to scrape av.by, uses av.by's built-in
price labels (below market / above market / significantly below).
Sends fresh deals to Telegram.
"""
import os, json, re, time, sqlite3, asyncio, sys
from datetime import datetime
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "5795308229")
MAX_BYN = 13000       # max price in BYN
PAGES = 3             # first 3 pages is enough for fresh deals
DB_FILE = Path("avby_seen.db")
STATE_FILE = Path("avby_state.json")

# ─── Helpers ─────────────────────────────────────────────────────────────
def to_byn(t: str) -> float:
    """Parse '12 500 ₽' or '25 000 $' → numeric value"""
    try:
        t = t.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
        # Identify currency
        if "$" in t or "USD" in t:
            v = float(re.sub(r"[^\d.]", "", t.replace("$", "").replace("USD", "")))
            return round(v * 2.58)  # approximate USD→BYN
        elif "€" in t or "EUR" in t:
            v = float(re.sub(r"[^\d.]", "", t.replace("€", "").replace("EUR", "")))
            return round(v * 2.95)
        elif "₽" in t:
            return float(re.sub(r"[^\d.]", "", t))
        else:
            return float(re.sub(r"[^\d.]", "", t))
    except:
        return 0

def parse_age(text: str) -> float:
    """'5 минут назад' → hours, '2 часа' → 2, 'день' → 24, 'вчера' → 24"""
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

def get_price_label(price_text: str) -> str:
    """Extract av.by's built-in price label from the listing"""
    t = price_text.lower().strip()
    if "сильно ниже рынка" in t:
        return "🔥 Сильно ниже рынка"
    elif "ниже рынка" in t:
        return "📉 Ниже рынка"
    elif "выше рынка" in t:
        return "📈 Выше рынка"
    elif "средняя" in t:
        return "📊 Средняя"
    return ""

def send_tg(text: str, photo_url: str = None):
    """Send message or photo to Telegram"""
    if not TG_TOKEN:
        print("No TG_TOKEN, skipping Telegram")
        return
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
                if r2.ok: return
        except:
            pass
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True},
            timeout=10,
        )
    except:
        pass

# ─── Scraper ─────────────────────────────────────────────────────────────
async def run():
    from playwright.async_api import async_playwright

    print(f"av.by check at {datetime.now().strftime('%H:%M')}")

    async with async_playwright() as pw:
        try:
            # local Windows — use system Chrome
            browser = await pw.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled"]
            )
        except Exception:
            # GitHub Actions / no Chrome — use bundled Playwright chromium
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
            await page.goto(f"https://cars.av.by/filter?page={pg}",
                           wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)

            items = await page.query_selector_all(".listing-item")
            if not items:
                break

            for item in items:
                try:
                    title_el = await item.query_selector(".listing-item__title")
                    if not title_el: continue
                    title = (await title_el.inner_text()).strip()

                    params_el = await item.query_selector(".listing-item__params")
                    params = (await params_el.inner_text()).strip() if params_el else ""

                    date_el = await item.query_selector(".listing-item__date")
                    ds = (await date_el.inner_text()).strip() if date_el else ""
                    hr = parse_age(ds)
                    if hr > 6: continue
                    if is_junk(title, params): continue

                    link_el = await item.query_selector(".listing-item__link")
                    link = ""
                    if link_el:
                        link = (await link_el.get_attribute("href")) or ""
                        if link and not link.startswith("http"):
                            link = "https://cars.av.by" + link
                    ad_id = link.split("/")[-1] if "/" in link else ""

                    price_el = await item.query_selector(".listing-item__price")
                    price_text = (await price_el.inner_text()).strip() if price_el else ""
                    price_byn = to_byn(price_text)
                    if price_byn > MAX_BYN: continue

                    # Get av.by's own market price label
                    label_el = await item.query_selector(".listing-item__price-remark, .listing-item__price-label, [class*=price-remark], [class*=price-label]")
                    label = ""
                    if label_el:
                        label = (await label_el.inner_text()).strip()
                    price_label = get_price_label(label or price_text)

                    yr_m = re.search(r"(\d{4})\s*г", params)
                    yr = int(yr_m.group(1)) if yr_m else 0
                    if yr > 0 and yr < 1995: continue

                    loc_el = await item.query_selector(".listing-item__location")
                    location = (await loc_el.inner_text()).strip() if loc_el else ""

                    # Get photo from detail page (only for good deals)
                    photo = None
                    if price_label and ("ниже" in price_label):
                        try:
                            await page.goto(link, wait_until="domcontentloaded", timeout=15000)
                            await page.wait_for_timeout(500)
                            gal = await page.query_selector(".card__gallery")
                            if gal:
                                imgs = await gal.query_selector_all("img")
                                for img in imgs:
                                    src = (await img.get_attribute("src")) or ""
                                    ds = (await img.get_attribute("data-src")) or ""
                                    u = ds or src
                                    if "avcdn" in u:
                                        photo = u.replace("/advertbig/", "/advertmedium/").replace(".avif", ".jpg")
                                        break
                            # Go back to filter page
                            await page.go_back()
                            await page.wait_for_timeout(500)
                        except:
                            pass

                    fresh.append({
                        "id": ad_id, "title": title, "link": link,
                        "price_text": price_text, "price_byn": price_byn,
                        "params": params, "year": yr, "location": location,
                        "ago": ds, "hours": hr,
                        "price_label": price_label, "photo": photo,
                    })
                except:
                    continue

            await asyncio.sleep(0.5)

        await browser.close()

    # ─── Dedup & Send ────────────────────────────────────────────────────
    conn = sqlite3.connect(str(DB_FILE))
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, sent_at TEXT)")
    # Clean old entries (>7 days)
    cur.execute("DELETE FROM seen WHERE sent_at < datetime('now', '-7 days')")
    conn.commit()

    sent = 0
    for ad in fresh:
        cur.execute("SELECT 1 FROM seen WHERE id=?", (ad["id"],))
        if cur.fetchone():
            continue  # already seen

        # Build message
        emoji = "🔥" if "сильно ниже" in ad.get("price_label", "") else "📉" if "ниже" in ad.get("price_label", "") else "⚡"
        label = ad.get("price_label", "")
        loc = ad.get("location", "")
        p = ad.get("params", "")[:55]
        age = ad.get("ago", "")

        text = (
            f"{emoji} <b>{ad['title']}</b>\n"
            f"💰 {ad['price_text']} ({label})\n"
            f"📅 {ad['year']} | {p}\n"
            f"⏱ {age} | 📍 {loc}\n"
            f"🔗 <a href='{ad['link']}'>Open av.by</a>"
        )

        send_tg(text, ad.get("photo"))

        cur.execute("INSERT INTO seen VALUES (?, ?)", (ad["id"], datetime.now().isoformat()))
        conn.commit()
        sent += 1
        print(f"  [{sent}] {ad['title'][:40]}")
        time.sleep(0.6)

    conn.close()
    print(f"Sent: {sent} fresh deal(s)")
    if sent == 0:
        print("Nothing new")

if __name__ == "__main__":
    asyncio.run(run())