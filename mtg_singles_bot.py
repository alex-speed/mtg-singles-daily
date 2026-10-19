#!/usr/bin/env python3
"""
MTG singles bot.

Reads single-card listings from Australian stores and works out which listings are the same
card (same name, set, foil type, treatment and collector number). Because most bargains are
mispriced new additions, it looks only at listings ADDED in the last few days (default 2) and
compares each one with the cheapest price at every other store. It reports:
  1. New listings priced well below the other stores.
  2. Wide price spreads that involve a new listing.
Only English, Near Mint, in-stock listings are compared. The report also shows how well each
store was read, so you can see which stores need fixing.

Safe to run in production? It only makes read-only HTTP GET requests to public web pages
(plus one request to Scryfall for set names) and sends one email. It does not change any
system. It is a personal project, so do not run it from a work server or a work tenant
unless that is allowed.

Email: if the environment variable SMTP_HOST is set, the report is sent through that SMTP
server. Otherwise it is sent through Microsoft Graph.

Usage:
    python mtg_singles_bot.py --dry-run                  # builds report.html, sends nothing
    python mtg_singles_bot.py --dry-run --max-pages 2    # quick test, 2 pages per collection
    python mtg_singles_bot.py --dry-run --new-days 7     # treat the last 7 days as new
    python mtg_singles_bot.py                            # builds the report and emails it
"""
import argparse
import os
import re
import smtplib
import ssl
import statistics
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape, unescape

import requests

# ---------------------------------------------------------------------------
# 1. Settings you are expected to edit
# ---------------------------------------------------------------------------

# Shopify stores: (store name, site address, [collection handles to try]).
# A handle is the last part of a collection URL, for example
#   https://thecardcave.com.au/collections/magic-singles  ->  "magic-singles"
# All handles below were taken from live pages seen in searches. Check that
# <site>/collections/<handle>/products.json opens in a browser before adding more.
SHOPIFY_STORES = [
    ("Good Games TCG", "https://tcg.goodgames.com.au", ["mtg-singles-all-products"]),
    ("Gameology", "https://www.gameology.com.au", ["magic-the-gathering-singles"]),
    ("Cherry Collectables", "https://www.cherrycollectables.com.au", ["magic-the-gathering-singles"]),
    ("The Card Cave", "https://thecardcave.com.au", ["magic-singles"]),
    ("Mint Collectables", "https://mintcollectables.com.au", ["magic-the-gathering-singles"]),
    ("Ozzie Collectables", "https://www.ozziecollectables.com", ["mtg-singles"]),
]

# A listing counts as new if its product was created or published, or its variant was created,
# within this many days. Set to 0 to turn the restriction off and look at every listing.
NEW_WITHIN_DAYS = 2
# What counts as a wide spread that involves a new listing (all must be true).
MIN_STORES_SPREAD = 2        # stores that have the card in stock
MIN_SPREAD_RATIO = 1.5       # highest price is at least 1.5 times the lowest
MIN_SPREAD_ABS = 8.00        # and at least this many dollars higher
# What counts as a cheap new listing (all must be true). It must be the cheapest listing of the
# card across all stores, and a new listing.
MIN_OTHERS_NEW = 1           # at least this many other stores must also stock the card
MIN_CHEAP_GAP = 0.25         # 25% or more below the median of the other stores
MIN_CHEAP_GAP_ONE_OTHER = 0.35  # a stricter gap when only one other store can be compared
MIN_CHEAP_SAVING = 5.00      # and saves at least this many dollars against that median
CHECK_PRINTING_RATIO = 4.0   # flag spreads this wide as "check it is the same printing"
MAX_ROWS = 40                # rows per table in the email
COMPARE_CONDITIONS = {"NM"}  # only these conditions are compared

MAX_PAGES = 140              # pages of 250 products per collection
REQUEST_DELAY = 1.0          # seconds to wait between requests, to be polite to small shops
TIMEOUT_SECONDS = 30
TIME_BUDGET_SECONDS = 1500   # stop fetching after 25 minutes so the job always finishes
USE_SCRYFALL = True          # one request for the list of set names and codes
USER_AGENT = "Mozilla/5.0 (personal price checker)"
SCRYFALL_HEADERS = {"User-Agent": "MTGSinglesBot/1.0 (personal price checker)", "Accept": "application/json"}

# ---------------------------------------------------------------------------
# 2. Small helpers
# ---------------------------------------------------------------------------

_START = time.monotonic()
RETRY_CODES = {429, 500, 502, 503, 504}

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Australia/Adelaide")
except Exception:  # no time zone database available: show UTC dates instead
    TZ = timezone.utc


def parse_dt(value):
    """Reads an ISO date from a store feed. A date with no time zone is taken as UTC."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def latest_dt(*values):
    got = [d for d in (parse_dt(v) for v in values) if d]
    return max(got) if got else None


def is_new(rec, now, days):
    """True if the listing was added within `days` days. With days = 0 everything counts as new."""
    if days <= 0:
        return True
    added = rec.get("added")
    return bool(added) and added >= now - timedelta(days=days)


def fmt_added(dt):
    return dt.astimezone(TZ).strftime("%a %d %b") if dt else "-"


def out_of_time():
    return time.monotonic() - _START > TIME_BUDGET_SECONDS


def retry_wait(resp, attempt):
    try:
        base = int(resp.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        base = 5
    return min(base, 60) * (attempt + 1)


def http_get(url, params=None, headers=None, tries=3):
    """GET with a polite delay and a few retries for rate limits and server errors."""
    for attempt in range(tries):
        time.sleep(REQUEST_DELAY)
        try:
            resp = requests.get(url, params=params, headers=headers or {"User-Agent": USER_AGENT},
                                timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        if resp.status_code in RETRY_CODES and attempt < tries - 1:
            time.sleep(retry_wait(resp, attempt))
            continue
        resp.raise_for_status()
        return resp


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s):
    """Lowercase, no accents, no apostrophes, other punctuation becomes a space."""
    s = strip_accents(unescape(s or "")).lower().replace("&", " and ")
    s = re.sub(r"['\u2019`]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_ROMAN = {"ii": "2", "iii": "3", "iv": "4"}
_SET_NOISE_RE = re.compile(r"\b(?:magic the gathering|mtg|singles?|cards?)\b")


def norm_set(s):
    n = norm(s)
    n = _SET_NOISE_RE.sub(" ", n)
    toks = [_ROMAN.get(t, t) for t in n.split()]
    n = " ".join(toks)
    return re.sub(r"^the ", "", n).strip()


def norm_num(s):
    """'0100' -> '100', '347/285' -> '347', '12a' stays '12a'."""
    s = (s or "").strip().lower().split("/")[0].strip()
    m = re.match(r"^0*(\d+)(.*)$", s)
    return (m.group(1) + m.group(2)) if m else s


# ---------------------------------------------------------------------------
# 3. Set names and codes
# ---------------------------------------------------------------------------


class SetBook:
    """Maps set names to set codes, so 'Modern Horizons 2' and 'MH2' count as the same set."""

    def __init__(self):
        self.code_by_name = {}
        self.name_by_code = {}

    def add(self, name, code, seeded=False):
        code = (code or "").lower().strip()
        key = norm_set(name)
        if not key or not code:
            return
        if seeded or key not in self.code_by_name:
            self.code_by_name[key] = code
        self.name_by_code.setdefault(code, name)

    def lookup_name(self, text):
        n = norm_set(text)
        if not n:
            return None
        if n in self.code_by_name:
            return self.code_by_name[n]
        m = re.match(r"^commander (.+)$", n)  # 'Commander: Kaldheim' -> 'Kaldheim Commander'
        if m and (m.group(1) + " commander") in self.code_by_name:
            return self.code_by_name[m.group(1) + " commander"]
        return None

    def lookup_code(self, text):
        t = (text or "").strip().lower()
        return t if t in self.name_by_code else None


def load_scryfall_sets(book):
    """Returns (number of sets loaded, error text or None). Failure is not fatal."""
    try:
        data = http_get("https://api.scryfall.com/sets", headers=SCRYFALL_HEADERS).json().get("data", [])
    except Exception as exc:
        return 0, str(exc)
    for s in data:
        book.add(s.get("name", ""), s.get("code", ""), seeded=True)
    return len(data), None


# ---------------------------------------------------------------------------
# 4. Reading a listing title
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^\s*(?:magic\s*:?\s*the\s*gathering|magic|mtg)\s*[-:|]\s+", re.I)
_SUFFIX_RE = re.compile(r"\s+[-|]\s*(?:magic\s*:?\s*the\s*gathering|mtg)\s*$", re.I)
_FOIL_PREFIX_RE = re.compile(r"^\s*(?:\[\s*foil\s*\]|foil)\s+", re.I)
_GROUP_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_SETNUM_RE = re.compile(r"^([A-Za-z0-9]{2,6})-(\d+[A-Za-z\u2605]*)$")
_RARITY_CODE_RE = re.compile(r"^(?:common|uncommon|rare|mythic(?:\s+rare)?|special|bonus)\s+([A-Za-z0-9]{2,6})$", re.I)
_HASHNUM_RE = re.compile(r"\s*#\s*(\d+[A-Za-z\u2605]*)\b")
_TRAILNUM_RE = re.compile(r"\s+(\d{1,4}[A-Za-z\u2605]?)(?:\s*/\s*\d{1,4})?\s*$")
_TRAILFOIL_RE = re.compile(r"\s+foil\s*$", re.I)

_FRAMES = {
    "borderless": r"\bborderless\b",
    "showcase": r"\bshowcase\b",
    "extended": r"\bextended(?:\s+art)?\b",
    "retro": r"\bretro(?:\s+frame)?\b",
    "fullart": r"\bfull[\s-]*art\b",
    "promo": r"\bpromo\b",
    "prerelease": r"\bpre[\s-]*release\b",
    "altart": r"\b(?:alternate|alt)[\s-]*art\b",
    "anime": r"\banime\b",
    "stamped": r"\b(?:gold[\s-]*)?stamped\b|\bsignature\b",
    "textless": r"\btextless\b",
    "serialized": r"\bserialized\b",
}
_FINISHES = {
    "etched": r"\betched\b",
    "surge": r"\bsurge\b",
    "galaxy": r"\bgalaxy\b",
    "textured": r"\btextured\b",
    "rainbow": r"\b(?:double[\s-]*)?rainbow\b",
    "compleat": r"\b(?:step[\s-]*and[\s-]*)?compleat\b",
    "raised": r"\braised\b",
    "halo": r"\bhalo\b",
    "ripple": r"\bripple\b",
    "confetti": r"\bconfetti\b",
    "oilslick": r"\boil[\s-]*slick\b",
    "gilded": r"\bgilded\b",
    "neonink": r"\bneon[\s-]*ink\b",
    "fracture": r"\bfracture\b",
}
_NONFOIL_RE = re.compile(r"\bnon[\s-]*foil\b|\bnormal\b|\bregular\b", re.I)
_FOIL_RE = re.compile(r"\bfoils?\b", re.I)
_FRAME_RES = {k: re.compile(v, re.I) for k, v in _FRAMES.items()}
_FINISH_RES = {k: re.compile(v, re.I) for k, v in _FINISHES.items()}

_COND_PATTERNS = [
    ("NM", re.compile(r"\b(?:nm|near\s+mint|nm/m|nm-m|mint)\b", re.I)),
    ("DMG", re.compile(r"\b(?:dmg|damaged|poor)\b", re.I)),
    ("HP", re.compile(r"\b(?:hp|heavily\s+played|heavy\s+play(?:ed)?)\b", re.I)),
    ("MP", re.compile(r"\b(?:mp|moderately\s+played|moderate\s+play(?:ed)?)\b", re.I)),
    ("LP", re.compile(r"\b(?:lp|lightly\s+played|slightly\s+played|sp)\b", re.I)),
]
_LANG_RE = re.compile(r"\b(?:japanese|korean|chinese|russian|german|french|italian|spanish|portuguese|"
                      r"non[\s-]*english|jpn|jp)\b", re.I)
_JUNK_RE = re.compile(r"\b(?:booster(?:s)?(?:\s+(?:pack|box|display))?|bundle|playmat|play-mat|sleeves?|binder|"
                      r"deck\s+box|starter\s+(?:kit|deck)|draft\s+night|commander\s+deck|theme\s+deck|scene\s+box|"
                      r"prerelease\s+pack|dice|life\s+counter|storage\s+box|token\s+set|card\s+box)\b", re.I)


def cond_from_text(text):
    for code, rx in _COND_PATTERNS:
        if rx.search(text or ""):
            return code
    return None


def scan_traits(text):
    """Returns (frames, finishes, foil flag or None, is_pure). is_pure means nothing but trait words."""
    low = text.lower()
    frames = {k for k, rx in _FRAME_RES.items() if rx.search(low)}
    finishes = {k for k, rx in _FINISH_RES.items() if rx.search(low)}
    foil = None
    if _NONFOIL_RE.search(low):
        foil = False
    if _FOIL_RE.search(_NONFOIL_RE.sub(" ", low)):
        foil = True
    rest = low
    for rx in list(_FRAME_RES.values()) + list(_FINISH_RES.values()) + [_NONFOIL_RE, _FOIL_RE]:
        rest = rx.sub(" ", rest)
    rest = re.sub(r"[^a-z0-9]+", "", rest)
    matched = bool(frames or finishes or foil is not None)
    return frames, finishes, foil, (matched and rest == "")


def clean_name(s):
    return re.sub(r"\s+", " ", s).strip(" -:,|")


def strip_set_prefix(name, loose_texts):
    """If the title starts with the set name given in vendor or tags, drop that prefix."""
    words = name.split()
    for text in loose_texts:
        target = norm_set(text)
        if not target:
            continue
        for k in range(1, len(words)):
            if norm_set(" ".join(words[:k])) == target:
                return " ".join(words[k:])
    return name


def parse_title(title, loose_texts=()):
    """Splits a listing title into card name, set clues, collector number, foil and treatments."""
    s = unescape(title or "").strip()
    s = _PREFIX_RE.sub("", s)
    s = _SUFFIX_RE.sub("", s)
    foil, cond = None, None
    frames, finishes, set_texts = set(), set(), []
    set_code, number = None, None

    m = _FOIL_PREFIX_RE.match(s)
    if m:
        foil, s = True, s[m.end():]

    groups = [a or b for a, b in _GROUP_RE.findall(s)]
    s = _GROUP_RE.sub(" ", s)
    parts = [p for p in re.split(r"\s+-\s+", s) if p.strip()]
    if not parts:
        return None
    name_part = parts[0]
    extra_parts = parts[1:]

    def handle_text(text, is_group):
        nonlocal foil, cond, set_code, number
        t = text.strip(" :,-")
        if not t:
            return
        if is_group:
            m1 = _SETNUM_RE.match(t)
            if m1:
                set_code, number = m1.group(1).lower(), norm_num(m1.group(2))
                return
            if t.isdigit():
                return
        else:
            if re.fullmatch(r"magic(?:\s*:?\s*the\s*gathering)?|mtg", t, re.I):
                return
            m2 = _RARITY_CODE_RE.match(t)
            if m2:
                set_code = set_code or m2.group(1).lower()
                return
        c = cond_from_text(t) if len(t) <= 20 else None
        if c and re.fullmatch(r"[A-Za-z/\-\s]+", t) and len(t.split()) <= 3:
            cond = c
            return
        fr, fi, fo, pure = scan_traits(t)
        if pure:
            frames.update(fr)
            finishes.update(fi)
            if fo is not None:
                foil = fo if foil is None else (foil or fo)
            return
        if _TRAILFOIL_RE.search(t):  # a set name followed by the word Foil
            foil = True
            t = _TRAILFOIL_RE.sub("", t)
        set_texts.append(clean_name(t))

    for g in groups:
        handle_text(g, True)
    for p in extra_parts:
        handle_text(p, False)

    mh = _HASHNUM_RE.search(name_part)
    if mh:
        number = number or norm_num(mh.group(1))
        name_part = _HASHNUM_RE.sub(" ", name_part)
    mt = _TRAILNUM_RE.search(name_part)
    if mt:
        number = number or norm_num(mt.group(1))
        name_part = name_part[:mt.start()]

    name = clean_name(strip_set_prefix(clean_name(name_part), loose_texts))
    front = name.split("//")[0].strip()
    if len(norm(front)) < 2:
        return None
    return {"name": front, "name_key": norm(front), "set_code": set_code, "set_texts": [t for t in set_texts if t],
            "number": number, "foil": foil, "frames": frozenset(frames), "finishes": frozenset(finishes),
            "cond_title": cond}


def loose_candidates(vendor, ptype, tags):
    out = []
    for raw in [vendor, ptype] + list(tags or []):
        t = re.sub(r"^\s*(?:set|expansion|edition)\s*[:_\-]\s*", "", raw or "", flags=re.I).strip()
        if t and t not in out:
            out.append(t)
    return out


def variant_text(v):
    parts = [v.get("title") or "", v.get("option1") or "", v.get("option2") or "", v.get("option3") or ""]
    return " ".join(p for p in parts if p and p.lower() != "default title")


# ---------------------------------------------------------------------------
# 5. Fetching and turning products into records
# ---------------------------------------------------------------------------


def new_stat():
    return {"pages": 0, "ok": 0, "products": 0, "variants": 0, "in_stock": 0, "junk": 0, "unparsed": 0,
            "foreign": 0, "not_nm": 0, "kept": 0, "with_set": 0, "with_number": 0, "dated": 0, "new": 0,
            "truncated": False,
            "errors": []}


def records_from_product(store, base, p, st, samples):
    st["products"] += 1
    title = p.get("title", "") or ""
    ptype = p.get("product_type", "") or ""
    if _JUNK_RE.search(title) and "single" not in ptype.lower():
        st["junk"] += 1
        return []
    tags = p.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    loose = loose_candidates(p.get("vendor", ""), ptype, tags)
    parsed = parse_title(title, loose)
    if not parsed:
        st["unparsed"] += 1
        return []
    out = []
    variants = p.get("variants", [])
    for v in variants:
        st["variants"] += 1
        if not v.get("available"):
            continue
        st["in_stock"] += 1
        vtext = variant_text(v)
        if _LANG_RE.search(f"{title} {vtext} {' '.join(tags)}"):
            st["foreign"] += 1
            continue
        try:
            price = float(v.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        cond = cond_from_text(vtext) or parsed["cond_title"] or "NM"
        if cond not in COMPARE_CONDITIONS:
            st["not_nm"] += 1
            continue
        if _NONFOIL_RE.search(vtext):
            foil = False
        elif _FOIL_RE.search(vtext):
            foil = True
        else:
            foil = bool(parsed["foil"])
        if parsed["finishes"]:
            foil = True
        url = f"{base}/products/{p.get('handle', '')}"
        if len(variants) > 1 and v.get("id"):
            url += f"?variant={v['id']}"
        added = latest_dt(p.get("created_at"), p.get("published_at"), v.get("created_at"))
        rec = {"store": store, "title": title, "url": url, "price": price, "foil": foil, "cond": cond,
               "added": added,
               "name": parsed["name"], "name_key": parsed["name_key"], "set_code": parsed["set_code"],
               "set_texts": parsed["set_texts"], "loose": loose, "number": parsed["number"],
               "frames": parsed["frames"], "finishes": parsed["finishes"],
               "uid": (store, p.get("handle", ""), v.get("id"))}
        out.append(rec)
        st["kept"] += 1
        if len(samples[store]) < 4:
            samples[store].append(rec)
    return out


def fetch_all(max_pages=None):
    """Returns (records, stats, samples)."""
    cap = max_pages or MAX_PAGES
    records, seen = [], set()
    stats, samples = {}, defaultdict(list)
    for store, base, handles in SHOPIFY_STORES:
        st = stats[store] = new_stat()
        st["tried"] = len(handles)
        for handle in handles:
            hit_cap = True
            for page in range(1, cap + 1):
                if out_of_time():
                    st["errors"].append("skipped: time budget used up")
                    hit_cap = False
                    break
                try:
                    products = http_get(f"{base}/collections/{handle}/products.json",
                                        {"limit": 250, "page": page, "sort_by": "created-descending"}).json().get("products", [])
                except Exception as exc:
                    st["errors"].append(f"{handle} page {page}: {exc}")
                    hit_cap = False
                    break
                if page == 1:
                    st["ok"] += 1
                st["pages"] += 1
                if not products:
                    hit_cap = False
                    break
                for p in products:
                    for rec in records_from_product(store, base, p, st, samples):
                        if rec["uid"] not in seen:
                            seen.add(rec["uid"])
                            records.append(rec)
                if len(products) < 250:
                    hit_cap = False
                    break
            if hit_cap:
                st["truncated"] = True
    return records, stats, samples


# ---------------------------------------------------------------------------
# 6. Resolving sets and grouping the same card
# ---------------------------------------------------------------------------


def resolve_set(rec, book):
    if rec["set_code"]:
        return rec["set_code"].lower()
    for t in rec["set_texts"]:
        code = book.lookup_name(t)
        if code:
            return code
    if rec["set_texts"]:
        return "name:" + norm_set(rec["set_texts"][0])
    for t in rec["loose"]:
        code = book.lookup_name(t) or book.lookup_code(t)
        if code:
            return code
    return ""


def prepare(records, book):
    for r in records:  # learn name-to-code pairs from titles that show both
        if r["set_code"] and r["set_texts"]:
            book.add(r["set_texts"][0], r["set_code"])
    for r in records:
        r["set_id"] = resolve_set(r, book)


def set_label(set_id, rec, book):
    if set_id.startswith("name:"):
        return rec["set_texts"][0] if rec["set_texts"] else set_id[5:]
    return book.name_by_code.get(set_id) or set_id.upper()


def finish_label(foil, finishes):
    if not foil:
        return "Non-foil"
    return "Foil" + (" (" + ", ".join(sorted(finishes)) + ")" if finishes else "")


def best_per_store(recs):
    best = {}
    for r in recs:
        cur = best.get(r["store"])
        if cur is None or r["price"] < cur["price"]:
            best[r["store"]] = r
    return best


def analyse(records, book, now=None, days=None):
    """Returns (spreads, cheaps). Both only include cards with at least one new listing."""
    now = now or datetime.now(timezone.utc)
    days = NEW_WITHIN_DAYS if days is None else days
    prepare(records, book)
    groups = defaultdict(list)
    for r in records:
        if not r["set_id"]:
            continue  # without a set, different printings could be mixed up
        groups[(r["name_key"], r["set_id"], r["foil"], r["finishes"], r["frames"])].append(r)

    spreads, cheaps = [], []
    for key, recs in groups.items():
        numbers = {r["number"] for r in recs if r["number"]}
        if len(numbers) > 1:  # same card, several numbered printings: compare like with like
            subs = [[r for r in recs if r["number"] == n] for n in sorted(numbers)]
        else:
            subs = [recs]
        for sub in subs:
            best = best_per_store(sub)  # cheapest in-stock listing at each store
            if len(best) < 2:
                continue
            ranked = sorted(best.values(), key=lambda r: r["price"])
            flags = [is_new(r, now, days) for r in ranked]
            if not any(flags):
                continue  # nothing new about this card, so it is not what we are looking for
            prices = [r["price"] for r in ranked]
            lo, hi = prices[0], prices[-1]
            first = ranked[0]
            row = {
                "name": first["name"], "set": set_label(first["set_id"], first, book),
                "finish": finish_label(first["foil"], first["finishes"]),
                "frames": ", ".join(sorted(first["frames"])), "number": first["number"] or "",
                "entries": [(r["store"], r["price"], r["url"], f) for r, f in zip(ranked, flags)],
                "lo": lo, "hi": hi, "ratio": hi / lo, "spread": hi - lo, "added": first.get("added"),
                "cv": statistics.pstdev(prices) / statistics.mean(prices),
            }
            if len(best) >= MIN_STORES_SPREAD and row["ratio"] >= MIN_SPREAD_RATIO and row["spread"] >= MIN_SPREAD_ABS:
                spreads.append(row)
            others = prices[1:]
            if flags[0] and len(others) >= MIN_OTHERS_NEW:
                med = statistics.median(others)
                gap = 1 - lo / med
                need = MIN_CHEAP_GAP if len(others) >= 2 else MIN_CHEAP_GAP_ONE_OTHER
                if gap >= need and (med - lo) >= MIN_CHEAP_SAVING:
                    cheaps.append({**row, "median_others": med, "gap": gap, "saving": med - lo,
                                   "n_others": len(others)})
    spreads.sort(key=lambda r: r["spread"], reverse=True)
    cheaps.sort(key=lambda r: r["saving"], reverse=True)
    return spreads, cheaps


def count_stats(records, stats, now, days):
    """Fills in the per-store counts used by the Store check table. Call after analyse()."""
    for r in records:
        st = stats[r["store"]]
        if r.get("set_id"):
            st["with_set"] += 1
        if r["number"]:
            st["with_number"] += 1
        if r.get("added"):
            st["dated"] += 1
        if is_new(r, now, days):
            st["new"] += 1


# ---------------------------------------------------------------------------
# 7. Building and sending the email
# ---------------------------------------------------------------------------


def money(x):
    return f"${x:,.2f}"


def store_summary(stats):
    working = sum(1 for s in stats.values() if s["ok"] and s["kept"])
    problems = []
    for name, s in stats.items():
        if s["ok"] == 0:
            first = s["errors"][0] if s["errors"] else "no response"
            problems.append(f"{name}: no collection could be read (first error: {first})")
        elif s["kept"] == 0:
            problems.append(f"{name}: readable, but no in-stock English NM singles were kept. "
                            f"Check the parse samples in a dry run.")
        else:
            if s["truncated"]:
                problems.append(f"{name}: stopped at the page limit, so some listings were not read")
            if any("time budget" in e for e in s["errors"]):
                problems.append(f"{name}: stopped early because the time budget ran out")
            if s["with_set"] < 0.5 * s["kept"]:
                problems.append(f"{name}: a set could be worked out for under half of its listings, "
                                f"so few cards can be compared")
            if NEW_WITHIN_DAYS > 0:
                if s["dated"] == 0:
                    problems.append(f"{name}: its listings carry no added dates, so none can be judged as new")
                elif s["kept"] >= 50 and s["new"] > 0.5 * s["kept"]:
                    problems.append(f"{name}: over half of its listings look new, so its added dates may be "
                                    f"unreliable (a bulk re-import can reset them)")
    return working, len(stats), problems


def card_cell(r):
    bits = escape(r["name"])
    extra = [x for x in [r["frames"], f"#{r['number']}" if r["number"] else ""] if x]
    return bits + (f' <span style="color:#666">({escape(", ".join(extra))})</span>' if extra else "")


def prices_cell(entries):
    out = []
    for store, price, url, new_flag in entries:
        tag = " <b>(new)</b>" if new_flag and NEW_WITHIN_DAYS > 0 else ""
        out.append(f'<a href="{escape(url)}">{escape(store)}</a> {money(price)}{tag}')
    return "<br>".join(out)


def build_html(spreads, cheaps, stats, notes, book, samples=None, now=None):
    now = now or datetime.now(timezone.utc)
    days = NEW_WITHIN_DAYS
    working, total, problems = store_summary(stats)
    kept = sum(s["kept"] for s in stats.values())
    new_total = sum(s["new"] for s in stats.values())
    when = now.astimezone(TZ).strftime("%a %d %b %Y")
    if days > 0:
        parts = [f"<p>{new_total} of {kept} in-stock English NM listings were added in the last {days} day(s). "
                 f"Each one was compared with the cheapest price at every other store. "
                 f"{working} of {total} stores were read on {when}.</p>"]
    else:
        parts = [f"<p>The new-listing restriction is turned off, so all {kept} in-stock English NM listings "
                 f"were compared. {working} of {total} stores were read on {when}.</p>"]
    tbl = '<table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse;font-size:13px">'

    parts.append("<h3>New listings priced well below the other stores</h3>")
    if cheaps:
        parts.append(tbl + "<tr><th>Card</th><th>Set, finish</th><th>New listing</th><th>Median of others</th>"
                     "<th>Saving</th><th>All prices</th></tr>")
        for r in cheaps[:MAX_ROWS]:
            first = r["entries"][0]
            weak = ""
            if r["n_others"] == 1:
                weak = '<br><span style="color:#b00">only one other store</span>'
            plural = "" if r["n_others"] == 1 else "s"
            parts.append(
                f"<tr><td>{card_cell(r)}</td><td>{escape(r['set'])}, {escape(r['finish'])}</td>"
                f"<td>{money(r['lo'])}<br>{escape(first[0])}<br>added {fmt_added(r['added'])}</td>"
                f"<td>{money(r['median_others'])}<br>({r['n_others']} other store{plural}){weak}</td>"
                f"<td>{money(r['saving'])} ({r['gap']:.0%})</td><td>{prices_cell(r['entries'])}</td></tr>")
        parts.append("</table>")
        if len(cheaps) > MAX_ROWS:
            parts.append(f"<p>Showing {MAX_ROWS} of {len(cheaps)}.</p>")
    else:
        parts.append("<p>None today.</p>")

    parts.append("<h3>Wide price spreads that involve a new listing</h3>")
    if spreads:
        parts.append(tbl + "<tr><th>Card</th><th>Set, finish</th><th>Lowest</th><th>Highest</th>"
                     "<th>Spread</th><th>CV</th><th>All prices</th></tr>")
        for r in spreads[:MAX_ROWS]:
            flag = ""
            if r["ratio"] >= CHECK_PRINTING_RATIO:
                flag = '<br><span style="color:#b00">check it is the same printing</span>'
            parts.append(
                f"<tr><td>{card_cell(r)}{flag}</td><td>{escape(r['set'])}, {escape(r['finish'])}</td>"
                f"<td>{money(r['lo'])}</td><td>{money(r['hi'])}</td>"
                f"<td>{money(r['spread'])} ({r['ratio']:.1f}x)</td><td>{r['cv']:.0%}</td>"
                f"<td>{prices_cell(r['entries'])}</td></tr>")
        parts.append("</table>")
        if len(spreads) > MAX_ROWS:
            parts.append(f"<p>Showing {MAX_ROWS} of {len(spreads)}.</p>")
        parts.append("<p>CV is the standard deviation divided by the mean price. Higher means the stores disagree more. "
                     "A new listing at the high end of a spread is a mispricing too, but not a bargain.</p>")
    else:
        parts.append("<p>None today.</p>")

    parts.append("<h3>Store check</h3>")
    parts.append(tbl + "<tr><th>Store</th><th>Pages</th><th>Products</th><th>In-stock variants</th>"
                 f"<th>Kept (NM, English)</th><th>With a set</th><th>With a number</th>"
                 f"<th>With an added date</th><th>New ({days}d)</th></tr>")
    for name, s in stats.items():
        trunc = " (hit page limit)" if s["truncated"] else ""
        parts.append(f"<tr><td>{escape(name)}</td><td>{s['pages']}{trunc}</td><td>{s['products']}</td>"
                     f"<td>{s['in_stock']}</td><td>{s['kept']}</td><td>{s['with_set']}</td>"
                     f"<td>{s['with_number']}</td><td>{s['dated']}</td><td>{s['new']}</td></tr>")
    parts.append("</table>")

    if problems:
        parts.append("<h3>Problems</h3><ul>" + "".join(f"<li>{escape(p)}</li>" for p in problems) + "</ul>")
    parts.append("<h3>Notes</h3><ul>" + "".join(f"<li>{escape(n)}</li>" for n in notes) + "</ul>")

    if samples is not None:
        parts.append("<h3>Parse samples (dry run only)</h3>")
        parts.append(tbl + "<tr><th>Store</th><th>Listing title</th><th>Read as</th></tr>")
        for store, recs in samples.items():
            for r in recs:
                read = (f"name: {r['name']}; set: {r.get('set_id') or 'unknown'}; number: {r['number'] or '-'}; "
                        f"{finish_label(r['foil'], r['finishes'])}; frames: {', '.join(sorted(r['frames'])) or '-'}; "
                        f"cond: {r['cond']}; price: {money(r['price'])}; added: {fmt_added(r.get('added'))}")
                parts.append(f"<tr><td>{escape(store)}</td><td>{escape(r['title'])}</td><td>{escape(read)}</td></tr>")
        parts.append("</table>")
    return "\n".join(parts)


def send_graph(subject, html):
    """Send via Microsoft Graph using an app registration (client credentials)."""
    tenant, client_id = os.environ["GRAPH_TENANT_ID"], os.environ["GRAPH_CLIENT_ID"]
    secret, sender, recipient = os.environ["GRAPH_CLIENT_SECRET"], os.environ["MAIL_FROM"], os.environ["MAIL_TO"]
    token = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"client_id": client_id, "client_secret": secret,
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=TIMEOUT_SECONDS,
    )
    token.raise_for_status()
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        json={"message": {"subject": subject,
                          "body": {"contentType": "HTML", "content": html},
                          "toRecipients": [{"emailAddress": {"address": recipient}}]},
              "saveToSentItems": False},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def send_smtp(subject, html):
    """Send through any SMTP server (for example Gmail with an app password)."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("MAIL_FROM") or user
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, os.environ["MAIL_TO"]
    msg.set_content("This report needs an email app that shows HTML.")
    msg.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=TIMEOUT_SECONDS) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)


def send_mail(subject, html):
    """Uses SMTP if SMTP_HOST is set, otherwise Microsoft Graph."""
    if os.environ.get("SMTP_HOST"):
        send_smtp(subject, html)
    else:
        send_graph(subject, html)


def main():
    global NEW_WITHIN_DAYS
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="write report.html instead of emailing")
    parser.add_argument("--max-pages", type=int, default=None, help="pages per collection (quick test)")
    parser.add_argument("--new-days", type=int, default=None,
                        help="listings added in the last N days count as new (0 turns the restriction off)")
    args = parser.parse_args()
    if args.new_days is not None:
        NEW_WITHIN_DAYS = max(0, args.new_days)
    now = datetime.now(timezone.utc)

    book = SetBook()
    loaded, err = (load_scryfall_sets(book) if USE_SCRYFALL else (0, "turned off"))
    records, stats, samples = fetch_all(args.max_pages)
    spreads, cheaps = analyse(records, book, now)
    count_stats(records, stats, now, NEW_WITHIN_DAYS)

    notes = [
        "Only in-stock, English, Near Mint listings are compared. If a store does not state a condition, NM is assumed.",
        "A listing counts as new when the store's feed says its product was created or published, or its variant "
        "was created, inside the window. Stores can reset these dates when they re-import stock, so watch the "
        "New column in the Store check table.",
        "Each new listing is compared with the cheapest in-stock price at every other store, new or old. "
        "A new listing that no other store carries cannot be judged, so it does not appear.",
        "Prices are the cheapest in-stock listing per store, in AUD, and do not include shipping. "
        "For cards under about $15, shipping can cancel out the saving.",
        "Two listings count as the same card only when name, set, foil type, treatment and collector number agree. "
        "A card with no set information is skipped rather than guessed.",
        (f"Set names loaded from Scryfall: {loaded}." if loaded
         else f"Scryfall set list not loaded ({err}). Set matching relied on codes shown in listing titles."),
    ]
    working, total, _ = store_summary(stats)
    new_total = sum(s["new"] for s in stats.values())
    html = build_html(spreads, cheaps, stats, notes, book, samples if args.dry_run else None, now)
    subject = (f"MTG new singles: {len(cheaps)} cheap new listing(s), {len(spreads)} spread(s), "
               f"{new_total} new scanned, {working}/{total} stores OK")
    if args.dry_run:
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(subject, "-> report.html written")
    else:
        send_mail(subject, html)
        print(subject, "-> email sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
