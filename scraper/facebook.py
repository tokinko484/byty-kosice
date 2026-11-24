"""Facebook skupiny – nové príspevky cez Bright Data (verejné skupiny, bez prihlásenia) + extrakcia cez Gemini.

Beh je asynchrónny, aby nebrzdil 15-minútovú kontrolu portálov:
  1. ak čaká snapshot z minulého behu a je hotový → stiahne ho a spracuje,
  2. ak uplynul interval → spustí nový snapshot (a chvíľu počká, či nebude hneď hotový).
Stav (snapshot, videné príspevky, mesačná spotreba záznamov) sa drží v state["fb"].
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

BD_TOKEN = os.getenv("BRIGHTDATA_TOKEN", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
BD_DATASET = os.getenv("FB_DATASET_ID", "gd_lz11l67o2cb3r0lkj3")  # Facebook – Posts by group URL
FB_GROUPS = [g.strip() for g in os.getenv(
    "FB_GROUPS", "https://www.facebook.com/groups/rentflatkosice/").split(",") if g.strip()]
MONTHLY_BUDGET = int(os.getenv("FB_MONTHLY_BUDGET", "4500"))  # free tier je 5 000 záznamov / mesiac
MIN_INTERVAL_MIN = int(os.getenv("FB_MIN_INTERVAL_MIN", "60"))
WAIT_READY_S = int(os.getenv("FB_WAIT_READY_S", "150"))
NUM_POSTS = int(os.getenv("FB_NUM_POSTS", "15"))  # koľko najnovších príspevkov na skupinu (ak to API prijme)
ACTIVE_DAYS = 21  # príspevok zo skupiny „zmizne“ z webu po 3 týždňoch
BD = "https://api.brightdata.com/datasets/v3"
GEMINI_MODELS = ("gemini-flash-lite-latest", "gemini-flash-latest")

NOW = datetime.now(timezone.utc)


def enabled() -> bool:
    return bool(BD_TOKEN and GEMINI_KEY and FB_GROUPS)


# ---------------------------------------------------------------- Bright Data
def _bd(method: str, path: str, **kw) -> requests.Response:
    r = requests.request(method, BD + path, headers={"Authorization": f"Bearer {BD_TOKEN}"}, timeout=60, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Bright Data {path.split('?')[0]} -> HTTP {r.status_code} {r.text[:150]}")
    return r


def bd_trigger(fb: dict, log) -> str:
    path = f"/trigger?dataset_id={BD_DATASET}&include_errors=true"
    r = None
    if NUM_POSTS and not fb.get("no_num_posts"):
        try:
            r = _bd("POST", path, json=[{"url": g, "num_of_posts": NUM_POSTS} for g in FB_GROUPS])
        except RuntimeError as e:
            if "HTTP 4" not in str(e):
                raise
            log(f"Facebook: num_of_posts neprijaté ({str(e)[:120]}) – skúšam bez neho")
            fb["no_num_posts"] = True
    if r is None:
        r = _bd("POST", path, json=[{"url": g} for g in FB_GROUPS])
    sid = (r.json() or {}).get("snapshot_id")
    if not sid:
        raise RuntimeError(f"Bright Data trigger bez snapshot_id: {r.text[:150]}")
    return sid


def bd_status(sid: str) -> str:
    return ((_bd("GET", f"/progress/{sid}").json() or {}).get("status") or "").lower()


def bd_download(sid: str) -> list[dict]:
    r = _bd("GET", f"/snapshot/{sid}?format=json")
    data = r.json()
    if isinstance(data, dict):  # chybová odpoveď alebo obal
        data = data.get("data") or data.get("records") or []
    return [d for d in data if isinstance(d, dict)]


# ---------------------------------------------------------------- Gemini
SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "is_offer": {"type": "BOOLEAN"},
            "in_kosice": {"type": "BOOLEAN"},
            "rooms": {"type": "STRING", "enum": ["1", "1,5", "2", "2,5", "3", "4+", "?"]},
            "rent_eur": {"type": "INTEGER", "nullable": True},
            "energy_eur": {"type": "INTEGER", "nullable": True},
            "all_inclusive": {"type": "BOOLEAN"},
            "area_m2": {"type": "NUMBER", "nullable": True},
            "location": {"type": "STRING"},
            "available_from": {"type": "STRING"},
            "contact": {"type": "STRING"},
            "advertiser": {"type": "STRING", "enum": ["súkromník", "RK", "?"]},
            "title": {"type": "STRING"},
            "warning": {"type": "STRING"},
        },
        "required": ["id", "is_offer", "in_kosice", "rooms", "rent_eur", "all_inclusive", "location",
                     "available_from", "contact", "advertiser", "title", "warning"],
    },
}

PROMPT = """Si parser realitných príspevkov z Facebook skupín o prenájme bytov v Košiciach (texty po slovensky, česky aj anglicky).
Pre KAŽDÝ príspevok vráť jeden objekt s rovnakým "id".

is_offer = true LEN ak ide o PONUKU na dlhodobý prenájom CELÉHO bytu (alebo celého poschodia/domu na bývanie).
is_offer = false pre: dopyt („hľadám“, „looking for“, „kúpim“), izbu, spolubývanie, miesto v izbe, lôžko,
cenu za osobu, predaj, krátkodobé/denné ubytovanie, kanceláriu, garáž, reklamu či iný obsah.
in_kosice = true ak je byt v Košiciach (vrátane mestských častí), inak false.

rooms (počet izieb podľa slovenského zvyku, kuchyňa sa nepočíta):
garsónka / studio / 1-izbový / 1+1 / 1+kk = "1"; 1,5-izbový = "1,5";
2-izbový / dvojizbový / 2+1 / 2+kk / one-bedroom (obývačka + spálňa) = "2"; 2,5-izbový = "2,5";
3-izbový / 3+1 / 3+kk / two-bedroom = "3"; viac = "4+"; nedá sa určiť = "?".

rent_eur = mesačný nájom v € bez energií (celé číslo), null ak chýba. energy_eur = mesačné energie/zálohy v €, ak sú uvedené.
all_inclusive = true ak je cena vrátane energií (vtedy rent_eur = celková mesačná suma).
area_m2 = rozloha v m², ak je uvedená. location = mestská časť / ulica (napr. „Terasa, Popradská“), "" ak chýba.
available_from = od kedy je voľný (text, napr. „1.12.“ alebo „ihneď“), "" ak chýba.
contact = telefón alebo e-mail z textu, "" ak chýba. advertiser = „RK“ ak inzeruje realitná kancelária/maklér, inak „súkromník“, „?“ ak sa nedá určiť.
title = stručný slovenský názov do 80 znakov v tvare „2-izbový byt, Terasa, 52 m²“ (pri izbe/inom obsahu vystihni obsah).
warning = krátke upozornenie, ak príspevok vyzerá podozrivo (záloha vopred bez obhliadky, majiteľ v zahraničí, kontakt len cez WhatsApp, nereálne nízka cena), inak "".

Príspevky (JSON):
"""


def gemini_extract(posts: list[dict]) -> dict[str, dict]:
    payload = [{"id": p["id"], "text": p["text"][:1800]} for p in posts]
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT + json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": SCHEMA},
    }
    last = None
    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
                if r.status_code != 200:
                    last = f"{model} HTTP {r.status_code}"
                    if r.status_code in (429, 500, 503):
                        time.sleep(8 * (attempt + 1))
                        continue
                    break
                parts = (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])
                items = json.loads(parts[0].get("text") or "[]")
                return {str(i.get("id")): i for i in items if isinstance(i, dict)}
            except (requests.RequestException, ValueError) as e:  # noqa: PERF203
                last = f"{model} {type(e).__name__}"
                time.sleep(5)
    raise RuntimeError(f"Gemini zlyhal ({last})")


# ---------------------------------------------------------------- spracovanie
def _post_id(rec: dict) -> str | None:
    pid = rec.get("post_id") or rec.get("id")
    if not pid:
        m = re.search(r"/posts/(\d+)|permalink/(\d+)|story_fbid=(\d+)", rec.get("url") or "")
        pid = next((g for g in (m.groups() if m else ()) if g), None)
    return str(pid) if pid else None


def _image(rec: dict) -> str | None:
    for a in rec.get("attachments") or []:
        if isinstance(a, dict):
            u = a.get("url") or a.get("image_url") or a.get("attachment_url")
            if u and str(u).startswith("http"):
                return u
    return None


def to_listing(rec: dict, ex: dict) -> dict | None:
    if not ex.get("is_offer") or not ex.get("in_kosice"):
        return None
    rooms = {"2,5": "2", "4+": "4", "?": None}.get(ex.get("rooms"), ex.get("rooms"))
    price = ex.get("rent_eur")
    text = (rec.get("content") or "").strip()
    extra = []
    if ex.get("all_inclusive"):
        extra.append("Cena vrátane energií.")
    elif ex.get("energy_eur"):
        extra.append(f"Energie: {ex['energy_eur']} €.")
    loc = (ex.get("location") or "").strip()
    title = (ex.get("title") or text[:80]).strip()
    return {
        "id": "fb:" + _post_id(rec), "source": "Facebook", "title": title,
        "desc": (" ".join(extra) + " " + text)[:300].strip(),
        "price": int(price) if price else None, "rooms": rooms,
        "area": float(ex["area_m2"]) if ex.get("area_m2") else None,
        "location": (loc + ", Košice") if loc else "Košice",
        "url": rec.get("url"), "image": _image(rec),
        "posted": (rec.get("date_posted") or "")[:10] or None,
        "available": (ex.get("available_from") or "").strip() or None,
        "contact": (ex.get("contact") or "").strip() or None,
        "advertiser": ex.get("advertiser") if ex.get("advertiser") in ("súkromník", "RK") else None,
        "warning": (ex.get("warning") or "").strip() or None,
        "group": rec.get("group_name"),
        "text_for_filter": f"{title} {loc} kosice",
    }


def process(records: list[dict], fb: dict, log) -> list[dict]:
    seen = set(fb.get("seen", []))
    posts = []
    for rec in records:
        pid = _post_id(rec)
        text = (rec.get("content") or "").strip()
        if not pid or pid in seen or rec.get("error") or len(text) < 15:
            continue
        posts.append({"id": pid, "text": text, "rec": rec})
    out = []
    first = not fb.get("primed")
    for i in range(0, len(posts), 20):  # max 20 príspevkov na jedno volanie Gemini
        chunk = posts[i:i + 20]
        ex = gemini_extract(chunk)
        for p in chunk:
            x = to_listing(p["rec"], ex.get(p["id"]) or {})
            if x:
                out.append(x)
        seen.update(p["id"] for p in chunk)  # až po úspešnej extrakcii
        fb["seen"] = sorted(seen, key=lambda s: (len(s), s))[-1500:]
    fb["primed"] = True
    if first:  # prvá dávka = história skupiny → len na web, bez upozornení
        for x in out:
            x["silent"] = True
    log(f"Facebook: {len(records)} záznamov, {len(posts)} nových príspevkov, {len(out)} ponúk bytov"
        + (" (prvá dávka bez upozornení)" if first else ""))
    return out


def fetch(state: dict, log) -> list[dict]:
    """Volá sa z hlavného behu ako zdroj. Vracia len NOVÉ ponuky (inkrementálny zdroj)."""
    if not enabled():
        raise RuntimeError("chýba BRIGHTDATA_TOKEN alebo GEMINI_API_KEY")
    fb = state.setdefault("fb", {})
    month = NOW.strftime("%Y-%m")
    usage = fb.setdefault("usage", {})
    for k in [k for k in usage if k != month]:
        usage.pop(k)
    out: list[dict] = []

    def collect(sid: str) -> bool:
        st = bd_status(sid)
        if st == "ready":
            records = bd_download(sid)
            usage[month] = usage.get(month, 0) + len(records)
            fb["last_count"] = len(records)
            out.extend(process(records, fb, log))
            fb.pop("snapshot", None)
            return True
        if st in ("failed", "error", "canceled", "cancelled"):
            fb.pop("snapshot", None)
            raise RuntimeError(f"Bright Data snapshot {st}")
        return False

    # 1) dokončiť snapshot z minulého behu
    if fb.get("snapshot"):
        if not collect(fb["snapshot"]):
            started = datetime.fromisoformat(fb.get("snapshot_at", NOW.isoformat()))
            if NOW - started > timedelta(hours=2):
                log("Facebook: snapshot visí > 2 h, zahadzujem")
                fb.pop("snapshot", None)
            else:
                log("Facebook: snapshot ešte nie je hotový")
            return out

    # 2) nový snapshot, ak uplynul interval a je rozpočet
    per_run = max(1, fb.get("last_count") or NUM_POSTS * len(FB_GROUPS) or 15)
    interval = max(MIN_INTERVAL_MIN, math.ceil(30 * 24 * 60 * per_run / MONTHLY_BUDGET))
    last = fb.get("last_trigger")
    due = not last or NOW - datetime.fromisoformat(last) >= timedelta(minutes=interval - 2)
    if not due:
        return out
    if usage.get(month, 0) + per_run > MONTHLY_BUDGET:
        log(f"Facebook: mesačný limit {MONTHLY_BUDGET} záznamov vyčerpaný ({usage.get(month, 0)}) – čakám na ďalší mesiac")
        return out
    sid = bd_trigger(fb, log)
    fb.update(snapshot=sid, snapshot_at=NOW.isoformat(timespec="seconds"), last_trigger=NOW.isoformat(timespec="seconds"),
              interval_min=interval)
    log(f"Facebook: spustený snapshot (interval {interval} min, tento mesiac {usage.get(month, 0)} záznamov)")
    t0 = time.time()
    while time.time() - t0 < WAIT_READY_S:
        time.sleep(15)
        if collect(sid):
            break
    return out
