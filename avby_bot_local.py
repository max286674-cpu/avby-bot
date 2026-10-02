#!/usr/bin/env python3
"""av.by Car Monitor — local Windows edition (cron every 5 min).

Логика (по запросу владельца):
- Фильтров почти нет. Единственный жёсткий ценовой потолок: 15 000 руб.
- Оценка рынка — ТОЛЬКО встроенная оценка av.by («Оценка стоимости авто»):
  av.by сам считает среднюю цену по активным и архивным объявлениям
  за 4 месяца, отбрасывая аномальные значения. Свой расчёт не делаем.
- Шлём в Telegram только то, что заметно ДЕШЕВЛЕ оценки av.by:
  -15% и больше (от -25% помечаем как «горячее»).
- Ссылку на оценку av.by кладёт прямо на страницу каждого объявления,
  так что для каждого кандидата: страница объявления -> страница оценки.
  Оценки кэшируются в sqlite на 6 часов.
- Объявление помечается «seen» только после вердикта, поэтому если
  оценка не успела получиться — оно будет рассмотрено в следующий тик.
"""

import os, re, time, sqlite3, random
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ─── Config ──────────────────────────────────────────────────────────────
def load_env():
    env = {}
    env_path = BASE / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_env = load_env()
TG_TOKEN = _env.get("TG_TOKEN", os.environ.get("TG_TOKEN", ""))
TG_CHAT = _env.get("TG_CHAT", os.environ.get("TG_CHAT", "5795308229"))

MAX_PRICE = 15000.0    # всё дороже не интересно вовсе
DEAL_PCT = 0.15        # шлём, если цена <= оценка * (1 - 0.15)
HOT_PCT = 0.25         # минус 25% и больше — «горячее»
CACHE_HOURS = 6        # сколько живёт кэш оценки av.by
MAX_AGE_HOURS = 48     # рассматриваем только свежие объявления
PAGES = 3              # страниц общего фильтра за один запуск
MAX_EVALS = 15         # максимум объявлений, оцениваемых за один тик (лимит запросов)
DB_FILE = BASE / "avby_seen.db"
LOG_FILE = BASE / "avby_bot.log"

import requests as req
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    "Referer": "https://cars.av.by/",
    "Connection": "keep-alive",
}

class WafBlock(Exception):
    pass

# ─── Helpers ─────────────────────────────────────────────────────────────
def to_byn(t: str) -> float:
    t = t.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    if not t:
        return 0.0
    try:
        v = float(re.sub(r"[^\d.]", "", t))
    except Exception:
        return 0.0
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
    if "сегодня" in t:
        return 3
    if "только что" in t:
        return 0
    return 999


def is_junk(title: str, params: str) -> bool:
    # не фильтр поиска, а отсев мусора: запчасти/битые/не авто
    txt = (title + " " + params).lower()
    for kw in [
        "запчаст", "бит", "авари", "не на ходу", "поврежден", "тотал",
        "разбит", "на запчасти", "ремонт", "неисправн", "разбор",
        "мотоцикл", "мопед", "скутер", "квадроцикл",
    ]:
        i = txt.find(kw)
        if i == -1:
            continue
        # граница слова слева (не кусок другого слова)
        if i > 0 and (txt[i - 1].isalpha() or txt[i - 1].isdigit()):
            continue
        # отрицание рядом ("не битый", "без ремонта") — не мусор
        before = txt[max(0, i - 12):i]
        if before.rstrip().endswith(("не", "без", "не ", "без ")):
            continue
        return True
    return False


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_tg(text: str, photo_url: str = None) -> bool:
    if not TG_TOKEN:
        log("  [TG] No TG_TOKEN")
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
        log(f"  [TG] {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"  [TG] {e}")
    return False


def extract_photo(soup_item):
    img = soup_item.select_one("img")
    if not img:
        return None
    src = img.get("data-src") or img.get("src") or ""
    if "avcdn" in src:
        return src.replace("/advertpreview/", "/advertmedium/")
    return None


def fetch(session, url: str, attempts: int = 2, timeout: int = 25):
    """GET с одной повторной попыткой (ноут просыпается — DNS бывает не готов)."""
    last = None
    for i in range(attempts):
        try:
            return session.get(url, timeout=timeout)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(2.0)
    raise last


def waf_check(resp):
    if resp.status_code in (403, 423, 468):
        raise WafBlock(f"HTTP {resp.status_code}")

# ─── WAF Cooldown ──────────────────────────────────────────────
COOLDOWN_FILE = BASE / ".avby_waf_block"
COOLDOWN_HOURS = 2.0

def check_cooldown():
    if not COOLDOWN_FILE.exists():
        return False
    try:
        blocked_since = datetime.fromisoformat(
            COOLDOWN_FILE.read_text(encoding="utf-8").strip()
        )
        elapsed = (datetime.now() - blocked_since).total_seconds() / 3600
        if elapsed < COOLDOWN_HOURS:
            log(f"WAF cooldown ({elapsed:.1f}h ago), skip")
            return True
        COOLDOWN_FILE.unlink()
        log("WAF cooldown expired, retrying")
    except Exception:
        COOLDOWN_FILE.unlink(missing_ok=True)
    return False

# ─── Встроенная оценка av.by ────────────────────────────────────────────
OCENKA_URL = "https://av.by/ocenka-avto"
OCENKA_MAP_TTL_DAYS = 30  # кэш резолва марка|модель|год -> slug

# av.by убрал ссылку на оценку со страниц объявлений, поэтому теперь slug
# собираем сами через каталог /ocenka-avto: бренд -> модель -> поколение.
# SSR страницы отдаёт JSON-состояние с опциями вида
#   {"id":892,"label":"Nissan","popular":false,"intValue":892}   (бренды/модели)
#   {"id":4982,"name":"iii-t32-restajling-2016-2025","label":"III (T32) ·
#    Рестайлинг, 2016…2025","metadata":{"yearFrom":2016,"yearTo":2025,...}}
_OPTION_RE = re.compile(
    r'\{"id":(\d+),"label":"([^"]+)","(?:popular":(?:true|false),"intValue":|intValue":)\1\}'
)
_GEN_RE = re.compile(
    r'"name":"([a-z0-9\-]+)","label":"[^"]*","metadata":\{"yearFrom":(\d+)(?:,"yearTo":(\d+))?'
)
_CYR = "абвгдежзийклмнопрстуфхцчшщъыьэюяєіїґ"
_LAT = "abvgdezhzijklmnoprstufhcchsh`y`e`uyieigg"
_CYR_TR = {ord(c): l for c, l in zip(_CYR, _LAT)}


def _norm(s: str) -> str:
    """Регистр/дефисы/скобки в сторону: 'Lada (ВАЗ)' == 'lada-vaz'."""
    s = s.lower().translate(_CYR_TR)
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE)


def _widget_slice(html: str, widget: str) -> str:
    """Кусок SSR-состояния, относящийся к полю brand/model/generation:
    от ближайшего назад \"options\":[ до \"widget\":\"price_statistics_<widget>\"."""
    m = re.search(r'"widget":"price_statistics_%s"' % widget, html)
    if not m:
        return ""
    start = html.rfind('"options":[', 0, m.start())
    if start < 0:
        return ""
    return html[start:m.start()]


def _ocenka_options(html: str, widget: str):
    """[(id, label), ...] брендов или моделей из SSR-состояния."""
    seg = _widget_slice(html, widget)
    seen, out = set(), []
    for m in _OPTION_RE.finditer(seg):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), m.group(2)))
    return out


def _match_option(options, needle: str):
    """Точное совпадение по нормализованному label, иначе по первому слову."""
    n = _norm(needle)
    if not n:
        return None
    for oid, label in options:
        if _norm(label) == n:
            return oid
    first = re.split(r"[\W_]+", needle.lower())[0]
    if not first:
        return None
    for oid, label in options:
        lab = _norm(label)
        if lab.startswith(first):
            return oid
    return None


def _pick_generation(html: str, year: int):
    """Поколение, покрывающее год (самое свежее из подходящих), иначе ближайшее."""
    seg = _widget_slice(html, "generation")
    gens, seen = [], set()
    for m in _GEN_RE.finditer(seg):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            y_to = int(m.group(3)) if m.group(3) else 2100  # открытый диапазон «2020…»
            gens.append((m.group(1), int(m.group(2)), y_to))
    if not gens:
        return None
    covering = [g for g in gens if g[1] <= year <= g[2]]
    if covering:
        return max(covering, key=lambda g: g[1])[0]
    return min(gens, key=lambda g: min(abs(year - g[1]), abs(year - g[2])))[0]


def _resolve_ocenka_slug(session, make_slug: str, model_slug: str, year: int):
    """Каталог /ocenka-avto: бренд -> модель -> поколение -> slug.
    Возврат: slug | None (не смогли резолвнуть)."""
    resp = fetch(session, OCENKA_URL, timeout=40)
    waf_check(resp)
    brand_id = _match_option(_ocenka_options(resp.text, "brand"), make_slug)
    if not brand_id:
        log(f"  [OC] бренд не найден в каталоге: {make_slug}")
        return None
    time.sleep(random.uniform(1.0, 2.0))

    resp = fetch(session, f"{OCENKA_URL}?brand={brand_id}", timeout=40)
    waf_check(resp)
    model_id = _match_option(_ocenka_options(resp.text, "model"), model_slug)
    if not model_id:
        log(f"  [OC] модель не найдена: {make_slug} / {model_slug}")
        return None
    time.sleep(random.uniform(1.0, 2.0))

    resp = fetch(
        session,
        f"{OCENKA_URL}?brand={brand_id}&model={model_id}&year={year}",
        timeout=40,
    )
    waf_check(resp)
    gen = _pick_generation(resp.text, year)
    if not gen:
        log(f"  [OC] нет поколения для {make_slug} {model_slug} {year}")
        return None
    return f"{make_slug}_{model_slug}_{gen}_{year}"


def _avg_for_slug(session, slug: str, conn, cur):
    """Средняя цена для известного slug (кэш estimates 6ч).
    Возврат: (avg_byn, slug) | None | 'skip'."""
    cur.execute("SELECT avg_byn, updated_at FROM estimates WHERE slug=?", (slug,))
    row = cur.fetchone()
    if row:
        try:
            age_h = (datetime.now() - datetime.fromisoformat(row[1])).total_seconds() / 3600
        except Exception:
            age_h = CACHE_HOURS + 1
        if age_h < CACHE_HOURS:
            if row[0] > 0:
                return (row[0], slug)
            # в кэше 0 — уже пробовали и не нашли цены
            return "skip"

    try:
        resp = fetch(session, f"{OCENKA_URL}/{slug}", timeout=40)
    except WafBlock:
        raise
    except Exception as e:
        log(f"  [EST] {slug}: {e}")
        return None
    waf_check(resp)
    if resp.status_code == 404:
        cur.execute(
            "INSERT OR REPLACE INTO estimates VALUES (?, 0, ?)",
            (slug, datetime.now().isoformat()),
        )
        conn.commit()
        return "skip"  # страницы оценки с таким slug нет

    m = re.search(
        r'stats__price-primary">\s*([\d\s\u202f\xa0]+)', resp.text
    )
    if not m:
        # SSR иногда отдаёт страницу без блока цены — одна повторная попытка
        time.sleep(2)
        resp = fetch(session, f"{OCENKA_URL}/{slug}", timeout=40)
        waf_check(resp)
        m = re.search(
            r'stats__price-primary">\s*([\d\s\u202f\xa0]+)', resp.text
        )
    avg = 0.0
    if m:
        avg = to_byn(m.group(1))
    cur.execute(
        "INSERT OR REPLACE INTO estimates VALUES (?, ?, ?)",
        (slug, avg, datetime.now().isoformat()),
    )
    conn.commit()
    if avg <= 0:
        # 0 уже в кэше → следующая попытка будет последней (там скип)
        log(f"  [EST] {slug}: не нашли среднюю цену (retry later)")
        return None
    return (avg, slug)


def get_av_estimate(session, ad_url: str, conn, cur):
    """Официальная средняя цена av.by для этого объявления.
    Шаг 0: старый путь — ссылка /ocenka-avto/<slug> на странице объявления
           (или ранее сохранённый slug).
    Шаг 1: новый путь — резолв через каталог /ocenka-avto
           (бренд -> модель -> поколение), кэш ocenka_map на 30 дней.
    Возврат: (avg_byn, slug) | None (нет оценки) | 'skip' (404/нет модели).
    """
    cur.execute("SELECT slug FROM ad_slugs WHERE url=?", (ad_url,))
    row = cur.fetchone()
    cached = row[0] if row else ""
    if cached == "404":
        return "skip"
    if cached and cached != "none":
        return _avg_for_slug(session, cached, conn, cur)

    try:
        resp = fetch(session, ad_url)
    except WafBlock:
        raise
    except Exception as e:
        log(f"  [AD] {e}")
        return None
    waf_check(resp)
    if resp.status_code == 404:
        cur.execute("INSERT OR REPLACE INTO ad_slugs VALUES (?, '404')", (ad_url,))
        conn.commit()
        return "skip"

    m = re.search(
        r'href="https://av\.by/ocenka-avto/([a-z0-9_\-]+)"', resp.text
    )
    if m:
        slug = m.group(1)
        cur.execute(
            "INSERT OR REPLACE INTO ad_slugs VALUES (?, ?)", (ad_url, slug)
        )
        conn.commit()
        time.sleep(random.uniform(1.0, 2.0))
        return _avg_for_slug(session, slug, conn, cur)

    # ─── Новый резолв: марка/модель из URL, год из h1 ────────────────────
    pm = re.match(r"https://cars\.av\.by/([a-z0-9\-]+)/([a-z0-9\-]+)/\d+", ad_url)
    make_slug, model_slug = (pm.group(1), pm.group(2)) if pm else ("", "")
    year = 0
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", resp.text, re.S)
    if h1:
        ym = re.search(r"(\d{4})\s*г", re.sub(r"<[^>]+>", "", h1.group(1)))
        if ym:
            year = int(ym.group(1))
    if not year:
        ym = re.search(r"(\d{4})\s*г", resp.text)
        if ym:
            year = int(ym.group(1))
    if not (make_slug and model_slug and year):
        log(f"  [AD] нет марки/модели/года для резолва: {ad_url}")
        return "skip"

    key = f"{make_slug}|{model_slug}|{year}"
    cur.execute("SELECT slug, updated_at FROM ocenka_map WHERE key=?", (key,))
    row = cur.fetchone()
    if row:
        try:
            age_d = (datetime.now() - datetime.fromisoformat(row[1])).total_seconds() / 86400
        except Exception:
            age_d = OCENKA_MAP_TTL_DAYS + 1
        if age_d < OCENKA_MAP_TTL_DAYS:
            if row[0]:
                cur.execute(
                    "INSERT OR REPLACE INTO ad_slugs VALUES (?, ?)", (ad_url, row[0])
                )
                conn.commit()
                return _avg_for_slug(session, row[0], conn, cur)
            return "skip"  # резолвить уже пробовали — модели нет в каталоге

    slug = _resolve_ocenka_slug(session, make_slug, model_slug, year)
    time.sleep(random.uniform(1.0, 2.0))
    if not slug:
        cur.execute(
            "INSERT OR REPLACE INTO ocenka_map VALUES (?, '', ?)",
            (key, datetime.now().isoformat()),
        )
        conn.commit()
        return "skip"
    # проверяем slug живой оценкой; если 404 — не кэшируем
    est = _avg_for_slug(session, slug, conn, cur)
    if est != "skip":
        # страница есть; None = цена не распарсилась, slug всё равно верный
        cur.execute(
            "INSERT OR REPLACE INTO ocenka_map VALUES (?, ?, ?)",
            (key, slug, datetime.now().isoformat()),
        )
        conn.commit()
        cur.execute(
            "INSERT OR REPLACE INTO ad_slugs VALUES (?, ?)", (ad_url, slug)
        )
        conn.commit()
    return est

# ─── Main ────────────────────────────────────────────────────────────────
def run():
    if check_cooldown():
        return
    log("av.by check")

    session = req.Session()
    session.headers.update(HEADERS)

    conn = sqlite3.connect(str(DB_FILE))
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, sent_at TEXT)")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS estimates "
        "(slug TEXT PRIMARY KEY, avg_byn REAL, updated_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ad_slugs "
        "(url TEXT PRIMARY KEY, slug TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ocenka_map "
        "(key TEXT PRIMARY KEY, slug TEXT, updated_at TEXT)"
    )
    cur.execute("DELETE FROM seen WHERE sent_at < datetime('now', '-7 days')")
    conn.commit()

    # ─── Сбор свежих объявлений ─────────────────────────────────────────
    fresh = []
    run_ids = set()
    for pg in range(1, PAGES + 1):
        try:
            resp = fetch(session, f"https://cars.av.by/filter?sort=4&page={pg}")  # sort=4: сначала новые
            waf_check(resp)
        except WafBlock as e:
            log(f"  Page {pg}: WAF block ({e})")
            COOLDOWN_FILE.write_text(datetime.now().isoformat())
            break
        except Exception as e:
            log(f"  Page {pg}: {e}")
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select(".listing-item")
        if not items:
            log(f"  Page {pg}: no listings")
            continue

        for it in items:
            try:
                link_el = it.select_one(".listing-item__link")
                if not link_el:
                    continue
                href = link_el.get("href", "")
                ad_id = href.split("/")[-1] if "/" in href else ""
                if not ad_id or ad_id in run_ids:
                    continue
                run_ids.add(ad_id)
                link = f"https://cars.av.by{href}" if href.startswith("/") else href

                title = link_el.get_text(" ", strip=True)

                price_el = it.select_one(".listing-item__price-primary")
                price_text = price_el.get_text(strip=True) if price_el else ""
                price_byn = to_byn(price_text)

                params_el = it.select_one(".listing-item__params")
                params = params_el.get_text(" ", strip=True) if params_el else ""

                yr = 0
                ym = re.search(r"(\d{4})\s*г", params)
                if ym:
                    yr = int(ym.group(1))

                date_el = it.select_one(".listing-item__date")
                ds = date_el.get_text(strip=True) if date_el else ""

                loc_el = it.select_one(".listing-item__location")
                location = loc_el.get_text(strip=True) if loc_el else ""

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
                        "photo": extract_photo(it),
                    }
                )
            except Exception as e:
                log(f"  [ERR item] {e}")
                continue

        time.sleep(random.uniform(2.0, 4.0))

    # ─── Отбор: цена против официальной оценки av.by ────────────────────
    sent = 0
    evaluated = 0
    for ad in fresh:
        if evaluated >= MAX_EVALS:
            log(f"  лимит {MAX_EVALS} оценок за тик, остальное — в следующий раз")
            break

        cur.execute("SELECT 1 FROM seen WHERE id=?", (ad["id"],))
        if cur.fetchone():
            continue

        if ad["price_byn"] <= 0:
            log(f"  [SKIP] {ad['title'][:40]} — нет цены")
            cur.execute(
                "INSERT OR REPLACE INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
            continue
        if ad["price_byn"] > MAX_PRICE:
            cur.execute(
                "INSERT OR REPLACE INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
            continue  # дороже 15000 — даже не смотрим
        if parse_age(ad["ago"]) > MAX_AGE_HOURS:
            cur.execute(
                "INSERT OR REPLACE INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
            continue
        if is_junk(ad["title"], ad["params"]):
            cur.execute(
                "INSERT OR REPLACE INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
            continue

        evaluated += 1
        try:
            est = get_av_estimate(session, ad["link"], conn, cur)
        except WafBlock:
            log(f"  [WAF] page blocked, cooldown")
            COOLDOWN_FILE.write_text(datetime.now().isoformat())
            break
        except Exception as e:
            log(f"  [ERR] {ad['title'][:40]} — оценка упала ({e}), повтор later")
            continue

        if est == "skip":
            cur.execute(
                "INSERT OR REPLACE INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
            continue
        if est is None:
            # не помечаем seen — попробуем в следующий тик
            log(f"  [WAIT] {ad['title'][:40]} — оценка не получена, повтор later")
            continue

        avg, slug = est
        # seen для не-дилов ставим после вердикта ниже; для дилов — после успешной отправки

        if avg <= 0:
            continue

        dev = (ad["price_byn"] - avg) / avg
        log(
            f"  [EVAL] {ad['title'][:40]} — {ad['price_byn']:.0f} vs av.by {avg:.0f} → {dev*100:+.0f}%"
        )
        if dev > -DEAL_PCT:
            cur.execute(
                "INSERT OR REPLACE INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
            continue

        emoji = "🔥" if dev <= -HOT_PCT else "✅"
        text = (
            f"{emoji} <b>{ad['title']}</b>\n"
            f"\U0001f4b0 {ad['price_text']}  |  оценка av.by: ~{avg:,.0f} руб.".replace(",", " ") + "\n"
            f"\U0001f4c9 {dev*100:+.0f}% от средней цены av.by\n"
            f"\U0001f4c5 {ad['year']} | {ad['params'][:60]}\n"
            f"\u23f1 {ad['ago']} | \U0001f4cd {ad['location']}\n"
            f"\U0001f517 <a href='{ad['link']}'>Open av.by</a>"
        )

        if send_tg(text, ad.get("photo")):
            sent += 1
            log(f"  [SENT] {ad['title'][:40]}")
            cur.execute(
                "INSERT OR REPLACE INTO seen VALUES (?, ?)",
                (ad["id"], datetime.now().isoformat()),
            )
            conn.commit()
        else:
            # не помечаем seen — ретрай в следующий тик
            log(f"  [SKIP] {ad['title'][:40]} — TG fail, повтор later")
        time.sleep(0.6)

    conn.close()
    log(f"Sent: {sent} deal(s), evaluated: {evaluated}")
    if sent == 0:
        log("Nothing new")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log(f"FATAL: {e}")
