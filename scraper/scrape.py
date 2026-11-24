#!/usr/bin/env python3
"""Byty Košice – zbiera inzeráty na prenájom 1,5–3 izbových bytov a posiela upozornenia.

Zdroje:
  * Bazoš (reality.bazos.sk)
  * Nehnuteľnosti.sk (rovnaká databáza ako Reality.sk a Byty.sk)
  * TopReality.sk

Spustenie:
  python scraper/scrape.py            # bežný beh (scrape + Telegram)
  python scraper/scrape.py --digest   # denný súhrn na mail (ak je nastavený)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import facebook  # Facebook skupiny cez Bright Data + Gemini (scraper/facebook.py)

# ---------------------------------------------------------------- nastavenia
MAX_PRICE = int(os.getenv("MAX_PRICE", "700"))   # €/mesiac – horná hranica VRÁTANE energií
GOOD_PRICE = int(os.getenv("GOOD_PRICE", "650"))  # celková suma pod touto hranicou = „dobrá cena“ 🔥
ENERGY_ASSUMED = int(os.getenv("ENERGY_ASSUMED", "100"))  # energie, ak ich inzerát neuvádza (minimum)
MIN_PRICE = 200                                    # nižšie ceny sú zvyčajne nezmysel/izby
SOURCE_MAX = MAX_PRICE                             # cenový strop pri sťahovaní zo zdrojov (nájom)
ROOMS_OK = ("1,5", "2", "3")                       # sledované byty ("?" = počet izieb neuvedený)
FILTER_VERSION = 3                                 # zmena filtrov → jednorazovo bez záplavy upozornení
KEEP_GONE_DAYS = 14                                # ako dlho držať zmiznuté inzeráty
FAIL_ALERT_AFTER = 8                               # po koľkých zlyhaniach zdroja poslať varovanie

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
LISTINGS_FILE = DATA_DIR / "listings.json"          # nešifrovaná verzia (len bez hesla)
STATUS_FILE = DATA_DIR / "status.json"
ENC_LISTINGS_FILE = DATA_DIR / "listings.enc.json"  # šifrovaná verzia (AES-GCM, heslo SITE_PASSWORD)
ENC_STATUS_FILE = DATA_DIR / "status.enc.json"
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")
PBKDF2_ITER = 200_000
STATE_FILE = DATA_DIR / "state.json"   # počítadlá chýb zdrojov

SITE_URL = os.getenv("SITE_URL", "")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHATS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7"})

NOW = datetime.now(timezone.utc)


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- šifrovanie dát pre web
def _key(password: str, salt: bytes, iterations: int) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen=32)


_SALT: bytes | None = None


def _shared_salt() -> bytes:
    """Jedna soľ pre všetky súbory → prehliadač odvodzuje kľúč len raz."""
    global _SALT
    if _SALT is None:
        import base64
        try:
            d = json.loads(ENC_LISTINGS_FILE.read_text("utf-8"))
            if d.get("iter") == PBKDF2_ITER:
                _SALT = base64.b64decode(d["salt"])
        except Exception:  # noqa: BLE001
            pass
        _SALT = _SALT or os.urandom(16)
    return _SALT


def encrypt_json(obj) -> str:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = _shared_salt(), os.urandom(12)
    ct = AESGCM(_key(SITE_PASSWORD, salt, PBKDF2_ITER)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return json.dumps({"v": 1, "iter": PBKDF2_ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)})


def decrypt_json(text: str):
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = json.loads(text)
    u = base64.b64decode
    pt = AESGCM(_key(SITE_PASSWORD, u(d["salt"]), d["iter"])).decrypt(u(d["iv"]), u(d["ct"]), None)
    return json.loads(pt)


def load_listings() -> list[dict] | None:
    """Vráti uložené inzeráty, alebo None ak ešte neexistujú."""
    if SITE_PASSWORD and ENC_LISTINGS_FILE.exists():
        try:
            return decrypt_json(ENC_LISTINGS_FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001 – zmenené heslo
            log("Nedá sa dešifrovať uložené dáta (zmenené heslo?) – začínam odznova bez upozornení")
            return None
    if LISTINGS_FILE.exists():  # pôvodný nešifrovaný súbor (prechod na heslo)
        return json.loads(LISTINGS_FILE.read_text("utf-8"))
    return None


def save_ai_data(listings: list[dict], status: dict):
    """Kompaktné dáta pre AI chat bota, šifrované kľúčom odvodeným z TELEGRAM_TOKEN (bot ho má tiež)."""
    if not TG_TOKEN:
        return
    import base64
    import hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    keep_keys = ("source", "title", "price", "rooms", "area", "location", "url", "first_seen", "active",
                 "gone_since", "price_drop_from", "also", "desc", "all_in", "energy", "total", "available", "contact", "advertiser", "warning")
    items = [{k: (x.get(k)[:200] if k == "desc" and x.get(k) else x.get(k)) for k in keep_keys if x.get(k) not in (None, "", [])}
             for x in listings if not x.get("dup_of")]
    payload = json.dumps({"status": status, "listings": items}, ensure_ascii=False).encode()
    key = hashlib.sha256(("k-watch-ai|" + TG_TOKEN).encode()).digest()
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, payload, None)
    (DATA_DIR / "ai.enc.json").write_text(json.dumps({"iv": base64.b64encode(iv).decode(),
                                                     "ct": base64.b64encode(ct).decode()}), "utf-8")


def save_data(listings: list[dict], status: dict, changed: bool):
    if SITE_PASSWORD:
        old_iter = None
        if ENC_LISTINGS_FILE.exists():
            try:
                old_iter = json.loads(ENC_LISTINGS_FILE.read_text("utf-8")).get("iter")
            except Exception:  # noqa: BLE001
                pass
        if changed or old_iter != PBKDF2_ITER:
            ENC_LISTINGS_FILE.write_text(encrypt_json(listings), "utf-8")
        ENC_STATUS_FILE.write_text(encrypt_json(status), "utf-8")
        for f in (LISTINGS_FILE, STATUS_FILE):  # nešifrované verzie zmazať
            f.unlink(missing_ok=True)
    else:
        log("UPOZORNENIE: SITE_PASSWORD nie je nastavené – dáta sú nešifrované")
        if changed or not LISTINGS_FILE.exists():
            LISTINGS_FILE.write_text(json.dumps(listings, ensure_ascii=False, indent=1), "utf-8")
        STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=1), "utf-8")


class NotFound(RuntimeError):
    pass


def get(url: str, **kw) -> requests.Response:
    last = None
    for attempt in range(3):
        try:
            r = S.get(url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                raise NotFound(f"{url} -> HTTP 404")
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # noqa: PERF203
            last = str(e)
        time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{url} -> {last}")


# ---------------------------------------------------------------- pomocné
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


RE_15 = re.compile(r"(?<![\d,.])1\s*[,.]\s*5\s*\.?\s*[-–]?\s*(izb|izieb)|(?<![\d,.])1[,.]5-?i\b|jedenapol", re.I)
RE_2 = re.compile(r"(?<![\d,.])2\s*(?:[,.]\s*5)?\s*\.?\s*[-–]?\s*(izb|izieb)|(?<![\d,.])2(?:[,.]5)?-?i\b|dvojizb|dvoj-izb|"
                  r"dvojgars|(?<![\d+])2\s*\+\s*(1|kk)\b|\b(one|1)[- ]?bedroom\b", re.I)
RE_3 = re.compile(r"(?<![\d,.])3\s*(?:[,.]\s*5)?\s*\.?\s*[-–]?\s*(izb|izieb)|(?<![\d,.])3(?:[,.]5)?-?i\b|trojizb|troj-izb|"
                  r"(?<![\d+])3\s*\+\s*(1|kk)\b|\b(two|2)[- ]?bedrooms?\b", re.I)
RE_4 = re.compile(r"(?<![\d,.])[4-6]\s*(?:[,.]\s*5)?\s*\.?\s*[-–]?\s*(izb|izieb)|(?<![\d,.])[4-6]-?i\b|stvorizb|patizb|"
                  r"(?<![\d+])[4-6]\s*\+\s*(1|kk)\b|\b(three|four|[3-5])[- ]?bedrooms?\b", re.I)
RE_1 = re.compile(r"garson|garzon|(?<![\d,.])1\s*\.?\s*[-–]?\s*(izb|izieb)|jednoizb|(?<![\d,.])1-?i\b|"
                  r"(?<![\d+])1\s*\+\s*(1|kk)\b|\b1kk\b|\bstudio\b", re.I)
# (texty sa porovnávajú bez diakritiky)
RE_SEEK = re.compile(r"\b(hladam|hladame|kupim|vymenim|vymena|dopyt)\b")
RE_ROOM_ONLY = re.compile(
    r"spolubyva|spolubyvajuc|\bizbu\b|\blozk|ubytovn|podnajom izby|prenajom izby|izba v \d|izby v \d|"
    r"miesto v izbe|miesto v byte|volne miesto|miesta v izbe|za osobu|na osobu|/osoba|za lozko|"
    r"zdielan|internat|kancelar|garaz na prenajom")


def detect_rooms(*texts: str) -> str | None:
    """Počet izieb z textu (prednosť má názov). "1" a "4" sa vracajú preto, aby ich filter vedome vyradil."""
    for t in texts:
        if not t:
            continue
        t = strip_accents(t)
        for pat, val in ((RE_15, "1,5"), (RE_2, "2"), (RE_3, "3"), (RE_4, "4"), (RE_1, "1")):
            if pat.search(t):
                return val
    return None


def has_rooms(t: str) -> bool:
    return bool(RE_15.search(t) or RE_2.search(t) or RE_3.search(t))


RE_ALLIN = re.compile(
    r"vratane (vsetkych )?(energii|poplatkov|nakladov|sluzieb)|s energiami|vratane energ|energie (su )?v cene|"
    r"v cene (su )?(aj )?energie|cena (je )?(vratane|so|s) energ|all[- ]?inclusive|including (all )?(utilities|bills|energ)|"
    r"(utilities|bills|energies) (are )?included")
RE_EN_AFTER = re.compile(
    r"(?:energi\w*|zaloh\w*|poplatk\w*|sluzb\w*|utilit\w*|bills)\D{0,30}?(\d{2,3})(?:[,.]\d+)?"
    r"(?:\s*[-–]\s*(\d{2,3}))?\s*(?:€|eur|,-)")
RE_EN_BEFORE = re.compile(
    r"(\d{2,3})(?:[,.]\d+)?\s*(?:€|eur|,-)?\s*(?:\+\s*)?(?:mesacne\s*)?(?:za\s*|na\s*)?(?:energi|zaloh|poplat|sluzb|utilit)")
RE_NOT_ENERGY = re.compile(r"\brk\b|realit|proviz|kauci|depozit|jednoraz|maklér|makler")


def parse_energy(t: str, price: int | None) -> int | None:
    """Mesačné energie/zálohy z textu (bez diakritiky, malé písmená). Pri rozpätí berie hornú hranicu."""
    for rx in (RE_EN_AFTER, RE_EN_BEFORE):
        for m in rx.finditer(t):
            ctx = m.group(0) if rx is RE_EN_AFTER else t[max(0, m.start() - 12):m.end()]
            if RE_NOT_ENERGY.search(ctx):  # poplatok RK, kaucia, provízia nie sú energie
                continue
            v = int(m.group(2) if rx is RE_EN_AFTER and m.group(2) else m.group(1))
            if 30 <= v <= 400 and v != price:
                return v
    return None


RE_PER_PERSON = re.compile(r"(za|na) osobu|/ ?osob[au]\b|za lozko|per person")
RE_COSTS_NEAR = re.compile(r"energi|zaloh|poplat|sluzb|voda|internet|vratane|utilit|bills")


def per_person_price(full: str) -> bool:
    """Cena za osobu/lôžko = izba. „Energie 50 € na osobu“ pri celom byte sa nepočíta."""
    for m in RE_PER_PERSON.finditer(full):
        if m.group(0) == "za lozko":
            return True
        if not RE_COSTS_NEAR.search(full[max(0, m.start() - 40):m.start()]):
            return True
    return False


def parse_price(text: str | None) -> int | None:
    if not text:
        return None
    t = text.replace("\xa0", " ")
    m = re.search(r"(\d[\d ]{1,7})(?:[,.]\d+)?\s*(€|eur)", t, re.I) or re.search(r"(\d[\d ]{1,7})", t)
    if not m:
        return None
    try:
        return int(m.group(1).replace(" ", ""))
    except ValueError:
        return None


def parse_area(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d{2,3}(?:[,.]\d{1,2})?)\s*(m2|m²|m\b)", text)
    return float(m.group(1).replace(",", ".")) if m else None


def norm_title(t: str) -> str:
    t = strip_accents(t.lower())
    return re.sub(r"[^a-z0-9]+", "", t)[:60]


# ---------------------------------------------------------------- zdroje
def scrape_bazos() -> list[dict]:
    base = "https://reality.bazos.sk/prenajmu/byt/"
    params = ("?hledat=&rubriky=reality&hlokalita=04001&humkreis=10"
              f"&cenaod=&cenado={SOURCE_MAX}&order=")
    out = []
    for offset in range(0, 400, 40):  # Bazoš zobrazuje 40 inzerátov na stránku
        url = base + (f"{offset}/" if offset else "") + params
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except NotFound:
            break  # za poslednou stránkou
        items = soup.select("div.inzeraty")
        if not items:
            break
        new_on_page = 0
        for it in items:
            a = it.select_one("h2.nadpis a")
            if not a:
                continue
            href = urljoin("https://reality.bazos.sk/", a.get("href", ""))
            m = re.search(r"/inzerat/(\d+)/", href)
            if not m:
                continue
            lid = "bazos:" + m.group(1)
            if any(x["id"] == lid for x in out):
                continue
            new_on_page += 1
            title = a.get_text(" ", strip=True)
            desc = (it.select_one(".popis") or it).get_text(" ", strip=True)
            price = parse_price((it.select_one(".inzeratycena") or it).get_text(" ", strip=True))
            loc = (it.select_one(".inzeratylok") or it).get_text(" ", strip=True)
            img = it.select_one("img")
            img = img.get("src") if img else None
            date_el = it.select_one(".velikost10")
            posted = None
            if date_el:
                dm = re.search(r"(\d{1,2})\.(\d{1,2})\.\s*(\d{4})", date_el.get_text())
                if dm:
                    posted = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
            out.append({
                "id": lid, "source": "Bazoš", "title": title, "desc": desc[:300],
                "price": price, "rooms": detect_rooms(title, desc), "area": parse_area(title + " " + desc),
                "location": re.sub(r"\s+", " ", loc), "url": href, "image": img, "posted": posted,
                "text_for_filter": title + " " + desc, "energy_text": desc,
            })
        if new_on_page == 0:
            break
        time.sleep(1.5)
    return out


def _nehnutelnosti_page(url: str) -> list[dict]:
    r = get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for sc in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(sc.string or "")
        except json.JSONDecodeError:
            continue
        for node in data.get("@graph", [data]):
            if node.get("@type") == "SearchResultsPage":
                items = node.get("mainEntity", {}).get("itemListElement", [])
    out = []
    for el in items:
        it = el.get("item", {})
        url_d = it.get("url", "")
        m = re.search(r"/detail/([^/]+)/", url_d)
        if not m:
            continue
        nid = m.group(1)
        # lokalita z HTML karty (best effort)
        loc = ""
        a = soup.find("a", href=re.compile(re.escape(nid)))
        node = a
        for _ in range(10):
            if node is None:
                break
            txt = node.get_text("\n", strip=True)
            lm = re.search(r"^[^\n]*Košice[^\n]*okres[^\n]*$", txt, re.M)
            if lm:
                loc = lm.group(0)
                break
            node = node.parent
        imgs = it.get("image") or []
        price = (it.get("priceSpecification") or {}).get("price")
        try:
            price = int(float(price)) if price is not None else None
        except (TypeError, ValueError):
            price = None
        area = (it.get("floorSize") or {}).get("value")
        title = it.get("name", "").strip(" |")
        desc = it.get("description", "") or ""
        out.append({
            "id": "nehn:" + nid, "source": "Nehnuteľnosti", "title": title, "desc": desc[:300],
            "price": price, "rooms": detect_rooms(it.get("category", ""), title, desc[:400]),
            "area": float(area) if area else parse_area(title),
            "location": loc, "url": url_d, "image": imgs[0] if imgs else None,
            "posted": (it.get("validFrom") or "")[:10] or None,
            "text_for_filter": title + " " + desc[:300], "energy_text": desc,
        })
    return out


def scrape_nehnutelnosti() -> list[dict]:
    out: list[dict] = []
    for cat, pages in (("2-izbove-byty", 4), ("1-izbove-byty", 3), ("3-izbove-byty", 3)):
        for p in range(1, pages + 1):
            url = (f"https://www.nehnutelnosti.sk/vysledky/{cat}/kosice/prenajom?order=NEWEST&priceTo={SOURCE_MAX}"
                   + (f"&page={p}" if p > 1 else ""))
            try:
                page = _nehnutelnosti_page(url)
            except NotFound:
                if p > 1:
                    break  # koniec stránkovania
                raise
            if not page:
                break
            if cat == "1-izbove-byty":
                page = [x for x in page if RE_15.search(x["title"] + " " + x["desc"])]
                for x in page:
                    x["rooms"] = "1,5"
            out.extend(x for x in page if all(y["id"] != x["id"] for y in out))
            time.sleep(1.5)
    return out


def scrape_topreality() -> list[dict]:
    out = []
    # 102 1-izb., 103 2-izb., 104 3-izb., 108 dvojgarsónka, 107 iný byt, 109 mezonet, 110 apartmán
    types = "".join(f"&type%5B%5D={t}" for t in (102, 103, 104, 108, 107, 109, 110))
    for page in range(1, 7):
        url = ("https://www.topreality.sk/vyhladavanie-nehnutelnosti.html?form=3" + types
               + f"&obec=&q=Ko%C5%A1ice&cena_do={SOURCE_MAX}&searchType=string"
               + (f"&page={page}" if page > 1 else ""))
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except NotFound:
            if page > 1:
                break
            raise
        cards = soup.select("div.row.estate")
        added = 0
        for c in cards:
            a = c.select_one("h2 a") or c.select_one("a[href*='-r']")
            if not a:
                continue
            href = urljoin("https://www.topreality.sk/", a.get("href", ""))
            m = re.search(r"-r(\d+)\.html", href)
            if not m:
                continue
            lid = "topr:" + m.group(1)
            if any(x["id"] == lid for x in out):
                continue
            added += 1
            title = a.get_text(" ", strip=True)
            price_el = c.select_one(".price")
            price = parse_price(price_el.get_text(" ", strip=True) if price_el else "")
            city = c.select_one(".location-city")
            dist = c.select_one(".location-district")
            loc = " ".join(x.get_text(" ", strip=True) for x in (city, dist) if x)
            img = c.select_one("img")
            img_url = None
            if img:
                img_url = img.get("data-src") or img.get("src")
                if img_url and img_url.startswith("data:"):
                    img_url = img.get("data-src")
            text = c.get_text(" ", strip=True)
            out.append({
                "id": lid, "source": "TopReality", "title": title, "desc": "",
                "price": price, "rooms": detect_rooms(title, text), "area": parse_area(text),
                "location": loc, "url": href, "image": urljoin("https://www.topreality.sk/", img_url) if img_url else None,
                "posted": None, "text_for_filter": title,
            })
        if added == 0:
            break
        time.sleep(1.5)
    return out


BEZREALITKY_QUERY = """
query($offset:Int){ listAdverts(offerType:[PRONAJEM], estateType:[BYT], limit:60, offset:$offset,
  order:TIMEORDER_DESC, currency:EUR,
  boundaryPoints:[{lat:48.80,lng:21.10},{lat:48.80,lng:21.40},{lat:48.62,lng:21.40},{lat:48.62,lng:21.10},{lat:48.80,lng:21.10}]){
  totalCount list { id uri disposition surface price charges currency address(locale:SK) mainImage{ url(filter:RECORD_THUMB) } } } }
"""


def scrape_bezrealitky() -> list[dict]:
    """Bezrealitky.sk – priamo od majiteľov (verejné GraphQL API, oblasť Košíc)."""
    r = S.post("https://api.bezrealitky.cz/graphql/", json={"query": BEZREALITKY_QUERY, "variables": {"offset": 0}},
               headers={"Origin": "https://www.bezrealitky.sk", "Referer": "https://www.bezrealitky.sk/"}, timeout=30)
    r.raise_for_status()
    data = (r.json().get("data") or {}).get("listAdverts") or {}
    out = []
    for a in data.get("list", []):
        disp = (a.get("disposition") or "").upper()
        rooms = ("1,5" if re.search(r"1_5|1_1", disp) else "2" if re.search(r"DISP_2", disp)
                 else "3" if re.search(r"DISP_3", disp) else (disp or "iné"))
        address = a.get("address") or ""
        surface = a.get("surface")
        title = f"{disp.replace('DISP_', '').replace('_IZB', '-izbový').replace('_', ',')} byt, {surface or '?'} m² – {address.split(',')[0]}"
        price = a.get("price")
        out.append({
            "id": "bezr:" + str(a["id"]), "source": "Bezrealitky", "title": title,
            "desc": f"Poplatky: {a.get('charges')} €" if a.get("charges") else "",
            "price": int(price) if price else None, "rooms": rooms, "area": float(surface) if surface else None,
            "energy": int(a["charges"]) if a.get("charges") else None,
            "location": address, "url": f"https://www.bezrealitky.sk/nehnutelnosti-byty-domy/{a.get('uri')}",
            "image": ((a.get("mainImage") or {}).get("url")), "posted": None,
            "text_for_filter": title + " " + address,
        })
    return out


def scrape_bazar() -> list[dict]:
    """Bazar.sk – najnovšie prenájmy 1- a 2-izbových bytov z celého SK, filtrované na Košice."""
    out = []
    for cat in ("2-izbove-byty", "1-izbove-byty"):
        soup = BeautifulSoup(get(f"https://reality.bazar.sk/byty/{cat}/prenajom/").text, "html.parser")
        for card in soup.select(".hmr-card"):
            loc_el = card.select_one(".location")
            loc = loc_el.get_text(" ", strip=True) if loc_el else ""
            if "košice" not in loc.lower():
                continue
            a = card.select_one("h2.title")
            a = a.find_parent("a") if a else None
            link = next((x.get("href") for x in card.select("a") if re.search(r"/\d{5,}-", x.get("href", ""))), None)
            if not link:
                continue
            m = re.search(r"/(\d{5,})-", link)
            title_el = card.select_one("h2.title")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            sub = card.select_one(".subtitle")
            desc = sub.get_text(" ", strip=True) if sub else ""
            price_el = card.select_one(".price")
            img = card.select_one("img.ad-photo")
            img_url = (img.get("data-src") or img.get("src")) if img else None
            if img_url and img_url.startswith("data:"):
                img_url = None
            rooms = detect_rooms(title, desc)
            if cat == "1-izbove-byty":
                if not RE_15.search(title + " " + desc):
                    continue
                rooms = "1,5"
            out.append({
                "id": "bazar:" + m.group(1), "source": "Bazar.sk", "title": title, "desc": desc[:300],
                "price": parse_price(price_el.get_text(" ", strip=True) if price_el else ""),
                "rooms": rooms or ("2" if cat == "2-izbove-byty" else None), "area": parse_area(title + " " + desc),
                "location": loc, "url": urljoin("https://reality.bazar.sk/", link),
                "image": urljoin("https://reality.bazar.sk/", img_url) if img_url else None,
                "posted": None, "text_for_filter": title + " " + desc + " " + loc,
            })
        time.sleep(1.5)
    return out


SOURCES = {
    "Bazoš": scrape_bazos,
    "Nehnuteľnosti": scrape_nehnutelnosti,
    "TopReality": scrape_topreality,
    "Bezrealitky": scrape_bezrealitky,
    # "Bazar.sk": scrape_bazar,  # vypnuté 2026-11-21: Bazar.sk blokuje GitHub Actions (RemoteDisconnected)
}
if facebook.enabled():
    SOURCES["Facebook"] = facebook.fetch   # volá sa so stavom: fetch(state, log)
INCREMENTAL = {"Facebook"}  # vracia len nové príspevky – neoznačovať ostatné ako zmiznuté


# ---------------------------------------------------------------- filter
def keep(x: dict) -> bool:
    price = x.get("price")
    if price is None or price < MIN_PRICE:
        return False
    t = strip_accents(x["title"].lower())
    full = strip_accents((x["title"] + " " + (x.get("desc") or "")).lower())
    full_e = strip_accents((x["title"] + " " + (x.get("energy_text") or x.get("desc") or "")).lower())
    all_in = bool(RE_ALLIN.search(full_e))
    if all_in:
        energy = 0
    elif x.get("energy") is not None:
        energy = x["energy"]
    else:
        energy = parse_energy(full_e, price)
    total = price + (energy if energy is not None else ENERGY_ASSUMED)
    if total > MAX_PRICE:  # rozpočet je vrátane energií
        return False
    x.update(all_in=all_in, energy=energy, total=total)
    rooms = x.get("rooms")
    if rooms is None:
        # počet izieb neuvedený: ponechať, ak to nevyzerá na izbu, garsónku či dopyt
        if (x.get("area") and x["area"] < 35) or RE_ROOM_ONLY.search(t) or re.search(r"\b(izba|izbu|izby|izieb)\b", t) \
                or RE_SEEK.search(full[:200]):
            return False
        x["rooms"] = "?"
    elif rooms not in ROOMS_OK and rooms != "?":
        return False
    if RE_SEEK.search(t):
        return False
    if RE_ROOM_ONLY.search(t) and not has_rooms(t):
        return False
    if per_person_price(full):
        return False
    # miesto v izbe / spolubývanie aj keď je v názve „2-izbový byt“
    if re.search(r"miesto v izbe|miesto v byte|volne miesto|miesta v izbe|spolubyva|"
                 r"(prenajom|prenajmem|podnajom|ponukam|volna) (jednej |samostatnej |nepriechodnej |zariadenej |vacsej |mensej )*izb[yu]\b|"
                 r"\bizb[ua] v (\d|rodinnom|dome|zdielan)|nepriechodnej izby|samostatnej izby|"
                 r"room for rent|\broom in (a|an|the)\b|flatmate|roommate|shared (flat|apartment)", full):
        return False
    # samostatná izba: v názve je „izba/izby/izbu“ bez počtu izieb bytu
    if re.search(r"\b(izba|izbu|izby|izieb)\b", t) and not has_rooms(t):
        return False
    # Nehnuteľnosti (URL /kosice/) a Bezrealitky (oblasť Košíc) sú už lokalitou obmedzené
    if "kosice" not in strip_accents((x.get("location", "") + " " + x["text_for_filter"]).lower()) \
            and x["source"] not in ("Bazoš", "Nehnuteľnosti", "Bezrealitky"):
        return False
    return True


# slová, ktoré má skoro každý názov – podľa nich sa dva rôzne byty nesmú zlúčiť
GENERIC_WORDS = {"prenajom", "prenajmem", "prenajmu", "ponukam", "ponuka", "izbovy", "izboveho", "izbovom", "izbove",
                 "kosice", "kosiciach", "kosic", "zariadeny", "zariadeneho", "zariadenom", "ciastocne", "novostavbe",
                 "novostavba", "kompletne", "pekny", "krasny", "moderny", "rekonstrukcii", "zrekonstruovany",
                 "balkonom", "loggiou", "lodziou", "sidlisko", "ulica", "ulici", "apartment", "apartman", "rent"}


def title_words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", strip_accents(t.lower())) if len(w) >= 5 and w not in GENERIC_WORDS}


def find_duplicate(x: dict, existing: dict) -> str | None:
    """Ten istý byt na inom portáli: rovnaká cena + (rovnaký názov alebo podobný názov a rozloha)."""
    nt, wx = norm_title(x["title"]), title_words(x["title"])
    for eid, e in existing.items():
        if e["source"] == x["source"] or e.get("dup_of") or e.get("price") != x.get("price"):
            continue
        rx, re_ = x.get("rooms"), e.get("rooms")
        if rx and re_ and "?" not in (rx, re_) and rx != re_:
            continue  # iný počet izieb = iný byt
        if x.get("area") and e.get("area") and abs(x["area"] - e["area"]) > 1.5:
            continue  # iná rozloha = iný byt
        if nt and nt == norm_title(e["title"]):
            return eid
        we = title_words(e["title"])
        sim = len(wx & we) / max(1, len(wx | we))
        same_area = x.get("area") and e.get("area") and abs(x["area"] - e["area"]) < 0.6
        if same_area and sim >= 0.3:
            return eid
        if sim >= 0.6 and min(len(wx), len(we)) >= 2:  # zhoda len na 1 slove je náhoda
            return eid
    return None


# ---------------------------------------------------------------- Telegram
def tg(method: str, **params):
    if not TG_TOKEN:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=params, timeout=30)
        j = r.json()
        if not j.get("ok"):
            log("Telegram chyba:", method, j.get("description"))
        return j
    except Exception as e:  # noqa: BLE001
        log("Telegram výnimka:", method, type(e).__name__)
        return None


def tg_handle_updates():
    """Odpovie každému, kto botovi napíše, jeho chat ID (potrebné do TELEGRAM_CHAT_ID)."""
    if not TG_TOKEN:
        log("Telegram: TELEGRAM_TOKEN je prázdny")
        return
    me = tg("getMe")
    log("Telegram bot:", (me or {}).get("result", {}).get("username") if me and me.get("ok") else "NEPLATNÝ TOKEN",
        "| chatov v TELEGRAM_CHAT_ID:", len(TG_CHATS))
    wh = tg("getWebhookInfo")
    if wh and wh.get("ok") and wh["result"].get("url"):
        log("Telegram: správy spracúva chat bot (webhook) – getUpdates preskakujem")
        return
    j = tg("getUpdates", timeout=0)
    if not j or not j.get("ok"):
        return
    updates = j.get("result", [])
    log("Telegram: nových správ pre bota:", len(updates))
    if not updates:
        return
    seen = set()
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        if str(cid) in TG_CHATS:
            text = "✅ Upozornenia na byty sú pre tento chat zapnuté."
        else:
            text = (f"👋 Ahoj! Tvoje chat ID je:\n<code>{cid}</code>\n\n"
                    "Vlož ho do GitHubu ako secret <b>TELEGRAM_CHAT_ID</b> "
                    "(viac ID oddeľ čiarkou) a začnú ti chodiť nové byty.")
        tg("sendMessage", chat_id=cid, text=text, parse_mode="HTML")
    tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)


def fmt_listing(x: dict) -> str:
    if not x.get("price"):
        price = "cena ?"
    elif x.get("all_in"):
        price = f"{x['price']} € vrátane energií"
    elif x.get("energy") is not None:
        price = f"{x['price']} € + {x['energy']} € energie = {x['price'] + x['energy']} €"
    else:
        price = f"{x['price']} € + energie neuvedené"
    total = x.get("total") or x.get("price")
    good = " 🔥" if total and total <= GOOD_PRICE else ""
    rooms = "izby neuvedené" if x.get("rooms") == "?" else f"{x['rooms']} izb."
    bits = [f"<b>{price}{good}</b> · {rooms}"]
    if x.get("area"):
        bits.append(f"{x['area']:g} m²")
    lines = [" · ".join(bits), html.escape(x["title"][:150])]
    if x.get("location"):
        lines.append("📍 " + html.escape(x["location"][:80]))
    if x.get("available"):
        lines.append("📅 Voľný od: " + html.escape(x["available"][:40]))
    if x.get("contact"):
        lines.append("📞 " + html.escape(x["contact"][:60]))
    if x.get("warning"):
        lines.append("⚠️ " + html.escape(x["warning"][:120]))
    src = x["source"] + (f" · {x['advertiser']}" if x.get("advertiser") else "")
    lines.append(f"🔎 {html.escape(src)}")
    lines.append(f'<a href="{html.escape(x["url"])}">Otvoriť inzerát</a>')
    return "\n".join(lines)


def notify_new(items: list[dict]):
    if not TG_TOKEN or not TG_CHATS:
        log(f"Telegram nie je nastavený – {len(items)} nových bez upozornenia")
        return
    for x in items[:25]:
        caption = fmt_listing(x)
        for cid in TG_CHATS:
            ok = False
            if x.get("image"):
                j = tg("sendPhoto", chat_id=cid, photo=x["image"], caption=caption, parse_mode="HTML")
                ok = bool(j and j.get("ok"))
            if not ok:
                tg("sendMessage", chat_id=cid, text=caption, parse_mode="HTML", disable_web_page_preview="true")
        time.sleep(1)
    if len(items) > 25:
        more = f"…a ďalších {len(items) - 25} nových ponúk." + (f"\n{SITE_URL}" if SITE_URL else "")
        for cid in TG_CHATS:
            tg("sendMessage", chat_id=cid, text=more)


def tg_broadcast(text: str):
    for cid in TG_CHATS:
        tg("sendMessage", chat_id=cid, text=text, parse_mode="HTML", disable_web_page_preview="true")


# ---------------------------------------------------------------- mail
def send_mail(subject: str, body_html: str) -> bool:
    user, pw = os.getenv("GMAIL_USER", ""), os.getenv("GMAIL_APP_PASSWORD", "")
    to = os.getenv("MAIL_TO", user)
    if not (user and pw and to):
        log("Mail nie je nastavený – preskakujem")
        return False
    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())
    return True


def digest():
    listings = load_listings() or []
    since = NOW - timedelta(hours=24)
    new = [x for x in listings if not x.get("dup_of") and x.get("active", True)
           and datetime.fromisoformat(x["first_seen"]) >= since]
    new.sort(key=lambda x: x.get("price") or 9999)
    if not new:
        log("Žiadne nové za 24 h – mail neposielam")
        return
    rows = "".join(
        f'<tr><td style="padding:6px 10px"><b>{x["price"]} €</b></td><td>{x["rooms"]} izb.</td>'
        f'<td>{x.get("area") or ""}{" m²" if x.get("area") else ""}</td>'
        f'<td><a href="{html.escape(x["url"])}">{html.escape(x["title"][:90])}</a><br>'
        f'<small>{html.escape(x.get("location") or "")} · {x["source"]}</small></td></tr>'
        for x in new)
    body = (f"<h2>Nové byty za posledných 24 h ({len(new)})</h2>"
            f"<table style='border-collapse:collapse;font-family:sans-serif'>{rows}</table>"
            + (f'<p><a href="{SITE_URL}">Otvoriť prehľad</a></p>' if SITE_URL else ""))
    send_mail(f"🏠 {len(new)} nových bytov v Košiciach", body)


# ---------------------------------------------------------------- hlavný beh
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stored = load_listings()
    first_run = stored is None
    old = {x["id"]: x for x in (stored or [])}
    old_json = json.dumps(stored, ensure_ascii=False, sort_keys=True) if stored is not None else None
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {"fails": {}}
    state.setdefault("initialized", sorted({x["source"] for x in old.values()}))

    try:
        tg_handle_updates()
    except Exception as e:  # noqa: BLE001
        log("getUpdates zlyhal:", e)

    status = {"checked_at": NOW.isoformat(timespec="seconds"), "sources": {}}
    scraped: dict[str, dict] = {}
    ok_sources = set()
    for name, fn in SOURCES.items():
        t0 = time.time()
        try:
            raw = fn(state, log) if name in INCREMENTAL else fn()
            kept = [x for x in raw if keep(x)]
            for x in kept:
                scraped[x["id"]] = x
            ok_sources.add(name)
            prev_fails = state["fails"].get(name, 0)
            state["fails"][name] = 0
            status["sources"][name] = {"ok": True, "raw": len(raw), "kept": len(kept)}
            log(f"{name}: {len(raw)} nájdených, {len(kept)} vyhovuje ({time.time() - t0:.1f}s)")
            if prev_fails >= FAIL_ALERT_AFTER:
                tg_broadcast(f"✅ Zdroj {name} opäť funguje.")
        except Exception as e:  # noqa: BLE001
            n = state["fails"].get(name, 0) + 1
            state["fails"][name] = n
            status["sources"][name] = {"ok": False, "error": str(e)[:200]}
            log(f"{name}: CHYBA {e}")
            if n == FAIL_ALERT_AFTER:
                tg_broadcast(f"⚠️ Zdroj {name} už {n}× za sebou nefunguje ({str(e)[:120]}).")

    merged = dict(old)
    new_items = []
    for lid, x in scraped.items():
        x.pop("text_for_filter", None)
        x.pop("energy_text", None)
        if lid in merged:
            e = merged[lid]
            old_price = e.get("price")
            price_drop = old_price and x.get("price") and x["price"] < old_price
            for k in ("title", "desc", "price", "rooms", "area", "location", "url", "image", "posted", "total", "all_in"):
                if x.get(k) is not None:
                    e[k] = x[k]
            e["energy"] = x.get("energy")
            if price_drop:
                e["price_drop_from"] = old_price
                if not e.get("dup_of") and not first_run:
                    new_items.append({**e, "title": f"📉 Zlacnené z {old_price} €: " + e["title"]})
            e["active"] = True
            e.pop("gone_since", None)
        else:
            silent = x.pop("silent", False)
            x["first_seen"] = NOW.isoformat(timespec="seconds")
            x["active"] = True
            # ten istý inzerát zmazaný a pridaný nanovo (nové ID na tom istom portáli) = nie je nový
            repost = next((eid for eid, e in merged.items()
                           if eid not in scraped and e["source"] == x["source"] and e.get("price") == x.get("price")
                           and norm_title(e["title"]) == norm_title(x["title"])), None)
            if repost:
                x["repost_of"] = repost
                x["first_seen"] = merged[repost]["first_seen"]
            dup = find_duplicate(x, {k: v for k, v in merged.items() if v.get("active", True)})
            if dup:
                x["dup_of"] = dup
                alts = merged[dup].setdefault("also", [])
                if all(a["url"] != x["url"] for a in alts):
                    alts.append({"source": x["source"], "url": x["url"]})
            merged[lid] = x
            if not dup and not repost and not silent:
                new_items.append(x)
            elif repost:
                log("Znovu pridaný inzerát – neposielam")

    # zmiznuté inzeráty (len pri zdrojoch, ktoré dnes fungovali)
    for lid, e in list(merged.items()):
        if e["source"] in INCREMENTAL:  # príspevky zo skupín: aktívne ACTIVE_DAYS od pridania
            if e.get("active", True) and datetime.fromisoformat(e["first_seen"]) < NOW - timedelta(days=facebook.ACTIVE_DAYS):
                e["active"] = False
                e["gone_since"] = NOW.isoformat(timespec="seconds")
            elif e.get("gone_since") and datetime.fromisoformat(e["gone_since"]) < NOW - timedelta(days=KEEP_GONE_DAYS):
                del merged[lid]
            continue
        if lid in scraped or e["source"] not in ok_sources:
            continue
        if e.get("active", True):
            e["active"] = False
            e["gone_since"] = NOW.isoformat(timespec="seconds")
        elif e.get("gone_since") and datetime.fromisoformat(e["gone_since"]) < NOW - timedelta(days=KEEP_GONE_DAYS):
            del merged[lid]

    listings = sorted(merged.values(), key=lambda x: x["first_seen"], reverse=True)
    prev_initialized = set(state["initialized"])
    state["initialized"] = sorted(prev_initialized | ok_sources)
    old_max = state.get("max_price", 680)
    state["max_price"] = MAX_PRICE
    filters_changed = state.get("filter_v") != FILTER_VERSION
    state["filter_v"] = FILTER_VERSION
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    status["active"] = sum(1 for x in listings if x.get("active") and not x.get("dup_of"))
    changed = json.dumps(listings, ensure_ascii=False, sort_keys=True) != old_json
    save_data(listings, status, changed)
    save_ai_data(listings, status)

    if first_run:
        log(f"Prvý beh – uložených {status['active']} ponúk, upozornenia neposielam")
        tg_broadcast(f"🏠 Sledovanie bytov spustené. Aktuálne {status['active']} ponúk do {MAX_PRICE} €."
                     + (f"\n{SITE_URL}" if SITE_URL else "") + "\nNové ponuky ti budem posielať sem.")
    else:
        # zdroj, ktorý práve funguje prvýkrát, by inak poslal všetky svoje inzeráty naraz
        fresh_sources = ok_sources - prev_initialized
        if fresh_sources:
            log("Prvý úspešný beh zdrojov (bez upozornení):", ", ".join(sorted(fresh_sources)))
        new_items = [x for x in new_items if x["source"] not in fresh_sources]
        # po zvýšení cenového limitu neposielať naraz všetky staršie drahšie ponuky
        if MAX_PRICE > old_max:
            skipped = [x for x in new_items if (x.get("price") or 0) > old_max]
            if skipped:
                log(f"Nový limit {MAX_PRICE} €: {len(skipped)} existujúcich ponúk nad {old_max} € bez upozornenia")
            new_items = [x for x in new_items if (x.get("price") or 0) <= old_max]
        new_items.sort(key=lambda x: x.get("price") or 9999)
        log(f"Nové/zlacnené: {len(new_items)}")
        if filters_changed and new_items:
            # rozšírené filtre by naraz poslali všetky staršie ponuky, ktoré doteraz neprešli
            tg_broadcast(f"🔧 Filtre zmenené (limit {MAX_PRICE} € vrátane energií) – "
                         f"pribudlo {len(new_items)} ponúk, nájdeš ich na webe." + (f"\n{SITE_URL}" if SITE_URL else ""))
            new_items = []
        notify_new(new_items)

    # /sync z Telegramu – potvrdenie žiadateľovi
    sync_chat = os.getenv("SYNC_CHAT", "").strip()
    if sync_chat and sync_chat in TG_CHATS:
        n_new = 0 if first_run else len(new_items)
        bad = [n for n, v in status["sources"].items() if not v.get("ok")]
        tg("sendMessage", chat_id=sync_chat, text=(
            f"✅ Kontrola hotová – {'žiadne nové ponuky' if not n_new else f'{n_new} nových/zlacnených (poslal som ich vyššie)'}."
            f"\nAktívnych ponúk: {status['active']}" + (f"\n⚠️ Nefungovalo: {', '.join(bad)}" if bad else "")))

    # ručné spustenie = testovacia správa s najlacnejšou aktuálnou ponukou
    if os.getenv("TEST_NOTIFY", "").lower() == "true" and TG_CHATS:
        best = sorted((x for x in listings if x.get("active") and not x.get("dup_of")),
                      key=lambda x: x.get("price") or 9999)[:1]
        tg_broadcast(f"🧪 Test – upozornenia fungujú. Sledujem {status['active']} ponúk."
                     + (f"\n{SITE_URL}" if SITE_URL else "") + "\nNajlacnejšia aktuálna ponuka:")
        notify_new(best)

    if not ok_sources:
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true")
    args = ap.parse_args()
    digest() if args.digest else main()
