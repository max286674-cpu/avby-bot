#!/usr/bin/env python3
"""Мини-тест нового get_av_estimate: 3 свежих объявления -> (title, avg, slug)."""
import sqlite3, sys
import requests as req
import avby_bot_local as bot

def main():
    session = req.Session()
    session.headers.update(bot.HEADERS)
    conn = sqlite3.connect(str(bot.DB_FILE))
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, sent_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS estimates (slug TEXT PRIMARY KEY, avg_byn REAL, updated_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS ad_slugs (url TEXT PRIMARY KEY, slug TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS ocenka_map (key TEXT PRIMARY KEY, slug TEXT, updated_at TEXT)")
    conn.commit()

    ads = []
    for pg in (1, 2):
        resp = bot.fetch(session, f"https://cars.av.by/filter?page={pg}")
        bot.waf_check(resp)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "lxml")
        for it in soup.select(".listing-item"):
            link_el = it.select_one(".listing-item__link")
            if not link_el:
                continue
            href = link_el.get("href", "")
            ads.append((link_el.get_text(" ", strip=True), f"https://cars.av.by{href}"))
            if len(ads) >= 3:
                break
        if len(ads) >= 3:
            break
    if len(ads) < 3:
        print(f"WARN: only {len(ads)} ads found")

    for title, url in ads[:3]:
        est = bot.get_av_estimate(session, url, conn, cur)
        if est == "skip":
            print(f"SKIP | {title} | {url}")
        elif est is None:
            print(f"None | {title} | {url} | причина в логе")
        else:
            print(f"OK   | {title} | avg={est[0]:.2f} BYN | slug={est[1]}")
    conn.close()

if __name__ == "__main__":
    main()
