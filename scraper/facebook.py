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
# verejné skupiny → počiatočný počet najnovších príspevkov na jeden snapshot (ďalej sa ladí automaticky)
DEFAULT_GROUPS = {
    "https://www.facebook.com/groups/rentflatkosice/": 4,            # Flat for rent Kosice, Byt na Prenajom
    "https://www.facebook.com/groups/byvanieaprenajomKOSICE/": 3,     # Bývanie a prenájom Košice (~2/deň)
    "https://www.facebook.com/groups/575297962860739/": 4,            # Nehnuteľnosti predaj-podnájom-prenájom KE (10+/deň)
    "https://www.facebook.com/groups/449027048994430/": 7,            # Bývanie Košice – byty, domy, pozemky (20+/deň)
}
_env = [g.strip() for g in os.getenv("FB_GROUPS", "").split(",") if g.strip()]  # "url|n,url|n"
GROUPS = {g.split("|")[0]: int(g.split("|")[1]) if "|" in g else 5 for g in _env} or DEFAULT_GROUPS
FB_GROUPS = list(GROUPS)
N_MIN, N_MAX = 3, 25
MONTHLY_BUDGET = int(os.getenv("FB_MONTHLY_BUDGET", "4500"))  # free tier je 5 000 záznamov / mesiac
MIN_INTERVAL_MIN = int(os.getenv("FB_MIN_INTERVAL_MIN", "60"))
WAIT_READY_S = int(os.getenv("FB_WAIT_READY_S", "150"))
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
    if not fb.get("no_num_posts"):
        n = fb.setdefault("n", {})
        try:
            r = _bd("POST", path, json=[{"url": g, "num_of_posts": n.setdefault(g, GROUPS[g])} for g in FB_GROUPS])
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


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
RE_BAD_MODEL = re.compile(r"image|tts|audio|live|embedding|vision|8b")


def _more_models(known: list[str]) -> list[str]:
    """Ďalšie dostupné Flash modely z API (záloha, keď sú hlavné preťažené)."""
    try:
        r = requests.get(GEMINI_URL + "?pageSize=200", headers={"x-goog-api-key": GEMINI_KEY}, timeout=20)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception:  # noqa: BLE001
        return []
    flash = [n for n in names if "flash" in n and not RE_BAD_MODEL.search(n) and n not in known]
    flash.sort(key=lambda n: ("preview" in n or "exp" in n, "lite" not in n, n), reverse=False)
    return flash[:4]


def gemini_extract(posts: list[dict]) -> dict[str, dict]:
    payload = [{"id": p["id"], "text": p["text"][:1800]} for p in posts]
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT + json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": SCHEMA},
    }
    deadline = time.time() + int(os.getenv("GEMINI_MAX_S", "240"))
    models, tried, errors = list(GEMINI_MODELS), set(), []
    while models and time.time() < deadline:
        model = models.pop(0)
        tried.add(model)
        for attempt in range(3):
            if time.time() > deadline:
                break
            try:
                r = requests.post(f"{GEMINI_URL}/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
                if r.status_code == 200:
                    parts = (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])
                    items = json.loads(parts[0].get("text") or "[]")
                    return {str(i.get("id")): i for i in items if isinstance(i, dict)}
                errors.append(f"{model} HTTP {r.status_code}")
                if r.status_code not in (429, 500, 502, 503, 504):
                    break  # chyba modelu/požiadavky → skúsiť iný model
            except (requests.RequestException, ValueError) as e:
                errors.append(f"{model} {type(e).__name__}")
            time.sleep(10 * (attempt + 1))  # preťaženie býva krátke
        if not models and not tried - set(GEMINI_MODELS):
            models = _more_models(list(tried))
    raise RuntimeError(f"Gemini zlyhal ({', '.join(dict.fromkeys(errors[-4:])) or 'časový limit'})")


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
        "energy": None if ex.get("all_inclusive") else ex.get("energy_eur"),
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


def group_key(url: str) -> str:
    return url.rstrip("/").split("/groups/")[-1].split("/")[0].lower()


def rec_group(rec: dict) -> str | None:
    txt = " ".join(str(v) for v in ((rec.get("input") or {}).get("url"), rec.get("group_url"), rec.get("url"),
                                    rec.get("group_id"), rec.get("group_name")) if v).lower()
    for g in FB_GROUPS:
        if f"/groups/{group_key(g)}/" in txt or f"/groups/{group_key(g)}?" in txt or txt.split() and group_key(g) in txt.split():
            return g
    return None


def tune(stats: dict, fb: dict, log):
    """Ak boli všetky stiahnuté príspevky skupiny nové, mohli niektoré uniknúť → stiahnuť viac.
    Ak je skupina pomalá, počet pomaly znižovať (šetrí free limit)."""
    n, rate, primed = fb.setdefault("n", {}), fb.setdefault("rate", {}), fb.setdefault("primed_groups", [])
    for g, (ret, new) in stats.items():
        if g is None:
            continue
        if g not in primed:  # prvá dávka skupiny je vždy „celá nová“
            primed.append(g)
            continue
        cur = n.get(g, GROUPS.get(g, 5))
        rate[g] = round(0.7 * rate.get(g, new) + 0.3 * new, 2)
        if ret >= cur and new >= cur:
            n[g] = min(N_MAX, cur + max(2, cur // 2))
            log(f"Facebook: {group_key(g)} – všetkých {cur} príspevkov bolo nových, zvyšujem na {n[g]} (niečo mohlo uniknúť)")
        elif max(N_MIN, math.ceil(rate[g] * 2) + 2) < cur:
            n[g] = cur - 1


def process(records: list[dict], fb: dict, log) -> list[dict]:
    seen = set(fb.get("seen", []))
    if fb.get("primed") and not fb.get("primed_groups"):  # prvá skupina už bola spracovaná pred touto verziou
        fb["primed_groups"] = [FB_GROUPS[0]]
    primed = set(fb.get("primed_groups", []))
    posts, stats = [], {}
    for rec in records:
        pid = _post_id(rec)
        text = (rec.get("content") or "").strip()
        if rec.get("error") or not pid:
            continue
        g = rec_group(rec)
        st = stats.setdefault(g, [0, 0])
        st[0] += 1
        if pid in seen:
            continue
        st[1] += 1
        if len(text) < 15:
            seen.add(pid)
            continue
        posts.append({"id": pid, "text": text, "rec": rec, "group": g})
    out = []
    for i in range(0, len(posts), 20):  # max 20 príspevkov na jedno volanie Gemini
        chunk = posts[i:i + 20]
        ex = gemini_extract(chunk)
        for p in chunk:
            x = to_listing(p["rec"], ex.get(p["id"]) or {})
            if x:
                if p["group"] is not None and p["group"] not in primed:  # história novej skupiny → ohlási sa v súhrne
                    x["silent"] = True
                out.append(x)
        seen.update(p["id"] for p in chunk)  # až po úspešnej extrakcii
        fb["seen"] = sorted(seen, key=lambda s: (len(s), s))[-1500:]
    fb["seen"] = sorted(seen | set(fb.get("seen", [])), key=lambda s: (len(s), s))[-1500:]
    fb["primed"] = True
    tune(stats, fb, log)
    per_group = ", ".join(f"{group_key(g) if g else '?'} {r}/{n}" for g, (r, n) in stats.items())
    log(f"Facebook: {len(records)} záznamov, {len(posts)} nových príspevkov, {len(out)} ponúk bytov"
        f" (skupina: stiahnuté/nové – {per_group})")
    return out


def fetch(state: dict, log) -> list[dict]:
    """Volá sa z hlavného behu ako zdroj. Vracia len NOVÉ ponuky (inkrementálny zdroj).
    Hotové snapshoty, ktoré sa nepodarilo spracovať (napr. Gemini preťažené), čakajú vo fronte
    a skúšajú sa pri každom behu – zber nových snapshotov medzitým pokračuje, nič sa nestratí."""
    if not enabled():
        raise RuntimeError("chýba BRIGHTDATA_TOKEN alebo GEMINI_API_KEY")
    fb = state.setdefault("fb", {})
    month = NOW.strftime("%Y-%m")
    usage = fb.setdefault("usage", {})
    for k in [k for k in usage if k != month]:
        usage.pop(k)
    if month == "2026-11" and not fb.get("usage_fix_1"):  # opakované pokusy omylom násobili spotrebu
        usage[month] = 33
        fb["usage_fix_1"] = True
    queue: list[str] = fb.setdefault("queue", [])        # hotové, ešte nespracované snapshoty
    counted: list[str] = fb.setdefault("counted", [])    # snapshoty už započítané do limitu
    out: list[dict] = []
    errors: list[str] = []

    def handle(sid: str) -> bool:
        """Stiahne a spracuje hotový snapshot. True = hotovo (alebo zahodený), False = skúsiť neskôr."""
        try:
            records = bd_download(sid)
        except RuntimeError as e:
            if "HTTP 404" in str(e) or "HTTP 410" in str(e):
                log(f"Facebook: snapshot {sid} už neexistuje, zahadzujem")
                return True
            errors.append(str(e))
            return False
        if sid not in counted:
            usage[month] = usage.get(month, 0) + len(records)
            counted.append(sid)
            del counted[:-30]
            fb["last_count"] = len(records)
        try:
            out.extend(process(records, fb, log))
            return True
        except RuntimeError as e:
            errors.append(str(e))
            log(f"Facebook: spracovanie odložené ({e}) – skúsim pri ďalšom behu")
            return False

    # 1) najprv fronta nespracovaných snapshotov (najstarší prvý)
    for sid in list(queue):
        if handle(sid):
            queue.remove(sid)
        else:
            break  # Gemini stále nejde – ostatné nechať na neskôr

    # 2) bežiaci snapshot z minulého behu
    if fb.get("snapshot"):
        sid = fb["snapshot"]
        st = bd_status(sid)
        if st == "ready":
            fb.pop("snapshot")
            if queue or not handle(sid):
                queue.append(sid) if sid not in queue else None
        elif st in ("failed", "error", "canceled", "cancelled"):
            fb.pop("snapshot")
            errors.append(f"Bright Data snapshot {st}")
        elif NOW - datetime.fromisoformat(fb.get("snapshot_at", NOW.isoformat())) > timedelta(hours=2):
            log("Facebook: snapshot visí > 2 h, zahadzujem")
            fb.pop("snapshot")
        else:
            log("Facebook: snapshot ešte nie je hotový")

    # 3) nový snapshot, ak uplynul interval a je rozpočet (aj keď fronta čaká – zber nesmie stáť)
    n = fb.get("n", {})
    planned = sum(n.get(g, GROUPS[g]) for g in FB_GROUPS)
    per_run = max(1, planned if not fb.get("no_num_posts") else (fb.get("last_count") or 15 * len(FB_GROUPS)))
    interval = max(MIN_INTERVAL_MIN, math.ceil(30 * 24 * 60 * per_run / MONTHLY_BUDGET))
    last = fb.get("last_trigger")
    due = not fb.get("snapshot") and (not last or NOW - datetime.fromisoformat(last) >= timedelta(minutes=interval - 2))
    if due and usage.get(month, 0) + per_run * (1 + len(queue)) > MONTHLY_BUDGET:
        log(f"Facebook: mesačný limit {MONTHLY_BUDGET} záznamov vyčerpaný ({usage.get(month, 0)}) – čakám na ďalší mesiac")
    elif due:
        sid = bd_trigger(fb, log)
        fb.update(snapshot=sid, snapshot_at=NOW.isoformat(timespec="seconds"),
                  last_trigger=NOW.isoformat(timespec="seconds"), interval_min=interval)
        log(f"Facebook: spustený snapshot (interval {interval} min, tento mesiac {usage.get(month, 0)} záznamov)")
        t0 = time.time()
        while time.time() - t0 < WAIT_READY_S:
            time.sleep(15)
            st = bd_status(sid)
            if st == "ready":
                fb.pop("snapshot")
                if queue or not handle(sid):
                    queue.append(sid) if sid not in queue else None
                break
            if st in ("failed", "error", "canceled", "cancelled"):
                fb.pop("snapshot")
                errors.append(f"Bright Data snapshot {st}")
                break

    if queue:
        log(f"Facebook: {len(queue)} snapshot(y) čaká na spracovanie (príspevky sú uložené v Bright Data)")
    if errors and not out:
        raise RuntimeError(errors[-1])
    return out
