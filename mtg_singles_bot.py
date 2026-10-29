#!/usr/bin/env python3
"""
MTG singles bot.

Reads single-card listings from Australian stores and works out which listings are the same
card (same name, set, foil type, treatment and collector number). Because most bargains are
mispriced new additions, it looks only at each store's latest additions and compares each one
with the cheapest price at every other store. It reports:
  1. New listings priced well below the other stores.
  2. New listings whose SET IS UNKNOWN but which look cheap against the same card in other sets
     or stores. These need a manual set check, but they are not skipped.
  3. Wide price spreads that involve a new listing.
Only English, Near Mint, in-stock listings are compared. The report also shows how well each
store was read, so you can see which stores need fixing.

Safe to run in production? It only makes read-only HTTP GET requests to public web pages
(plus one request to Scryfall for set names) and sends one email. It does not change any
system. It is a personal project, so do not run it from a work server or a work tenant
unless that is allowed.

Email: if the environment variable SMTP_HOST is set, the report is sent through that SMTP
server. Otherwise it is sent through Microsoft Graph.

Stores are read in parallel (one worker per store, one request per second per store).

Usage:
    python mtg_singles_bot.py --dry-run                  # builds report.html, sends nothing
    python mtg_singles_bot.py --dry-run --max-pages 2    # quick test, 2 pages per collection
    python mtg_singles_bot.py --dry-run --new-days 7     # widen the window to 7 days
    python mtg_singles_bot.py --dry-run --new-mode calendar   # measure the window back from today instead
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape, unescape

import requests

# ---------------------------------------------------------------------------
# 1. Settings you are expected to edit
# ---------------------------------------------------------------------------

# Shopify stores: (store name, site address, [collection handles], mode).
# mode is optional. "all" (the default) reads every handle in the list. "first" stops at the first
# handle that works, so the rest are only fallbacks.
# A handle is the last part of a collection address, for example
#   https://thecardcave.com.au/collections/magic-singles  ->  "magic-singles"
# The handle "*" means the store's whole product feed (<site>/products.json). It is used when the
# collection name is not known. Whole-store feeds are filtered so that only Magic cards are kept.
# "seen" = the collection address came from a live page. "guess" = probably Shopify, but the feed
# has not been seen. The Store check table in the report shows which stores actually work.
# Adelaide stores come first.
SHOPIFY_STORES = [
    ("Shuffled", "https://shuffled.com.au", ["mtg-singles-instock"]),                          # Adelaide, seen
    ("Kastle Cards and Games", "https://kastlecardsandgames.com", ["*"]),                      # Adelaide, Shopify
    ("Good Games Adelaide", "https://ggadelaide.com.au", ["mtg-singles-instock"]),             # Adelaide, seen
    ("The Cardhub Australia", "https://thecardhubaustralia.com.au", ["mtg-singles"]),          # Adelaide, seen
    ("Moon MTG", "https://moonmtg.com", ["cards-in-stock"]),                                   # Adelaide, seen
    ("Card Traders Australia", "https://cardtradersaustralia.com.au", ["*"]),                  # Adelaide, guess
    ("Good Games TCG", "https://tcg.goodgames.com.au",
     ["mtg-singles-instock", "mtg-singles-all-products"], "first"),                            # seen
    ("Pro Gamers", "https://progamers.com.au", ["mtg-singles-instock"]),                       # seen
    ("Cherry Collectables", "https://www.cherrycollectables.com.au", ["magic-the-gathering-singles"]),  # seen
    ("Gameology", "https://www.gameology.com.au", ["magic-the-gathering-singles"]),            # seen
    ("The Card Cave", "https://thecardcave.com.au", ["magic-singles"]),                        # seen
    ("Mint Collectables", "https://mintcollectables.com.au", ["magic-the-gathering-singles"]),  # seen
    ("Ozzie Collectables", "https://www.ozziecollectables.com", ["mtg-singles"]),              # seen
    ("Mana Market", "https://manamarket.com.au", ["magic-the-gathering-singles"]),             # seen
    ("Mana Masters", "https://www.manamasters.com.au", ["tcg-singles"]),                       # seen
    ("Urban Empire", "https://urbanempirecollectables.com", ["magic-the-gathering-singles"]),  # seen
    ("MTG Singles Australia", "https://www.mtgsinglesaustralia.com", ["*"]),                   # guess
    ("Arcane Alfred", "https://www.arcanealfred.com.au", ["*"]),                               # guess
    ("Jenes MTG", "https://jenesmtg.com.au", ["*"]),                                           # guess
    ("ManaCat", "https://manacat.com.au", ["*"]),                                              # guess
    ("Gumnut TCG", "https://tcg.gumnut.com.au", ["*"]),                                        # Shopify
    ("That Game Store", "https://thatgamestore.com.au", ["*"]),                                # Shopify
]

# A listing counts as new if its product was created or published, or its variant was created,
# inside the window below. Set NEW_WITHIN_DAYS to 0 to turn the restriction off.
#   NEW_MODE "store":    the window is measured back from EACH STORE'S OWN newest listing. A store whose
#                        newest listing is 20 days old still contributes its latest batch.
#   NEW_MODE "calendar": the window is measured back from today, the same for every store.
NEW_WITHIN_DAYS = 2
NEW_MODE = "store"
# In "store" mode, ignore a store entirely if its newest listing is older than this many days
# (0 = never ignore). Stock that old is probably no longer a fresh mispricing.
MAX_STORE_AGE_DAYS = 0
# What counts as a wide spread that involves a new listing (all must be true).
MIN_STORES_SPREAD = 2        # stores that have the card in stock
MIN_SPREAD_RATIO = 1.5       # highest price is at least 1.5 times the lowest
MIN_SPREAD_ABS = 8.00        # and at least this many dollars higher
# What counts as a cheap new listing (all must be true). It must be the cheapest listing of the
# card across all stores, and a new listing. The same rules judge listings whose set is unknown.
MIN_OTHERS_NEW = 1           # at least this many other stores must also stock the card
MIN_CHEAP_GAP = 0.25         # 25% or more below the median of the other stores
MIN_CHEAP_GAP_ONE_OTHER = 0.35  # a stricter gap when only one other store can be compared
MIN_CHEAP_SAVING = 5.00      # and saves at least this many dollars against that median
CHECK_PRINTING_RATIO = 4.0   # flag spreads this wide as "check it is the same printing"
MAX_LISTING_AGE_DAYS = 5     # drop anything added more than this many days ago (Adelaide dates)
MAX_ROWS = 40                # rows per table in the email
COMPARE_CONDITIONS = {"NM"}  # only these conditions are compared

MAX_PAGES = 140              # pages of 250 products per collection
REQUEST_DELAY = 1.0          # seconds to wait between requests, to be polite to small shops
TIMEOUT_SECONDS = 30
MAX_WORKERS = 6              # stores read at the same time
STALE_NEWEST_DAYS = 45       # warn if a store's newest listing is older than this
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
    """True if the listing was added within `days` days of now. With days = 0 everything counts as new."""
    if days <= 0:
        return True
    added = rec.get("added")
    return bool(added) and added >= now - timedelta(days=days)


def store_anchors(records, now):
    """Each store's newest listing date, never later than now (a bad date in the future is clamped)."""
    newest = {}
    for r in records:
        a = r.get("added")
        if a and (r["store"] not in newest or a > newest[r["store"]]):
            newest[r["store"]] = a
    return {s: min(a, now) for s, a in newest.items()}


def make_is_new(records, now, days, mode=None):
    """Returns a function that says whether a listing counts as new, under the chosen mode."""
    mode = mode or NEW_MODE
    anchors = store_anchors(records, now) if mode == "store" else {}

    def check(r):
        if days <= 0:
            return True
        added = r.get("added")
        if not added:
            return False
        end = now
        if mode == "store":
            end = anchors.get(r["store"])
            if end is None:
                return False
            if MAX_STORE_AGE_DAYS > 0 and end < now - timedelta(days=MAX_STORE_AGE_DAYS):
                return False
        return added >= end - timedelta(days=days)

    return check


def age_days(dt, now=None):
    """Whole calendar days (Adelaide dates) between the day a listing was added and today. None if no date."""
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    return max((now.astimezone(TZ).date() - dt.astimezone(TZ).date()).days, 0)
def is_today(dt, now=None):
    return age_days(dt, now) == 0
def is_recent(dt, now=None):
    """True if the listing was added within MAX_LISTING_AGE_DAYS days. 5d ago is kept, 6d ago is not."""
    d = age_days(dt, now)
    return d is not None and d <= MAX_LISTING_AGE_DAYS
def fmt_age(dt, now=None):
    d = age_days(dt, now)
    if d is None:
        return ""
    return "today" if d == 0 else f"{d}d ago"
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

_PREFIX_RE = re.compile(r"^\s*(?:(?:magic|mtg)(?:\s*:?\s*the\s*gathering)?\s+singles?\s*[-:|]\s+|magic\s*:?\s*the\s*gathering\s*[-:|]?\s*|(?:magic|mtg)\s*[-:|]\s+)", re.I)
_SUFFIX_RE = re.compile(r"\s+[-|]\s*(?:magic\s*:?\s*the\s*gathering|mtg)\s*$", re.I)
_FOIL_PREFIX_RE = re.compile(r"^\s*(?:\[\s*foil\s*\]|foil)\s+", re.I)
_GROUP_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_LETTER = r"(?=[A-Za-z0-9]*[A-Za-z])"          # the code must contain at least one letter
_SETNUM_RE = re.compile(r"^(" + _LETTER + r"[A-Za-z0-9]{2,6})\s*[-/#]\s*(\d+[A-Za-z\u2605]*)$")
_SETNUM_FIND_RE = re.compile(r"[\(\[]\s*(" + _LETTER + r"[A-Za-z0-9]{2,6})\s*[-/#]\s*(\d+[A-Za-z\u2605]*)\s*[\)\]]")
_CODE_SPACE_NUM_RE = re.compile(r"^(" + _LETTER + r"[A-Z0-9]{2,6})\s+(\d{1,4}[A-Za-z\u2605]?)$")
_TRAIL_CODE_SLASH_RE = re.compile(r"\s+(" + _LETTER + r"[A-Z0-9]{2,6})\s*/\s*(\d{1,4}[A-Za-z\u2605]?)\s*$")
_RARITY_CODE_RE = re.compile(r"^(?i:common|uncommon|rare|mythic(?:\s+rare)?|special|bonus)\s+((?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,6})$")  # the code must be capitals, so "Special Guests" is not a code
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
    "concept": r"\bconcept(?:\s+praetors?)?\b",
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
    "facet": r"\bfacet\b",
    "chocobo": r"\bchocobo(?:[\s-]*track)?\b",
    "scroll": r"\bsilver[\s-]*scroll\b",
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


# ---------------------------------------------------------------------------
# Printing identity helpers (added by patch_identity_fixes.py)
# ---------------------------------------------------------------------------
# A bracketed word that no set explains (for example "Shattered Glass") keeps a listing out of the plain
# printing's group, because it is probably a different printing. Set this to False to turn that off.
STRICT_QUALIFIERS = True
_EDITION_RES = {
    "worldchamp": re.compile(r"\bworld\s+championships?\b|\bwcd\b|\bwc\s*\d{2}\b", re.I),
    "goldborder": re.compile(r"\bgold[\s-]*border(?:ed)?\b", re.I),
    "collectors": re.compile(r"\b(?:international\s+|intl\.?\s+)?collector[s\u2019']*\s+edition\b", re.I),
}
EDITION_LABELS = {"worldchamp": "World Championship", "goldborder": "Gold Border", "collectors": "Collectors' Edition"}
_QUAL_NOISE_RE = re.compile(r"^(?:english|en|eng|nm|near mint|magic|mtg|regular|normal|standard|default|common|"
                            r"uncommon|rare|mythic|mythic rare|special|bonus)$")
_GENERIC_REMAINDER = {"token", "tokens", "emblem", "emblems", "card", "cards"}
_OTHER_GAME_RE = re.compile(r"\b(?:pok[e\u00e9]mon|yu-?gi-?oh|lorcana|digimon|one\s+piece|dragon\s*ball|"
                            r"flesh\s+and\s+blood|star\s+wars\s+unlimited)\b", re.I)
_NONCARD_TYPE_RE = re.compile(r"\b(?:events?|tickets?|figures?|figurines?|toys?|plush|apparel|clothing|merch(?:andise)?|"
                              r"gift\s*cards?|vouchers?|accessor(?:y|ies)|supplies|sleeves|playmats?|sealed)\b", re.I)
_NONCARD_TAG_RE = re.compile(r"^(?:events?|event\s+tickets?|tickets?|figures?|action\s+figures?|toys?|plush|apparel|"
                             r"merch(?:andise)?|gift\s*cards?|accessories|supplies)$", re.I)
_NONCARD_TITLE_RE = re.compile(r"\b(?:event\s+tickets?|entry\s+tickets?|gift\s+cards?|action\s+figures?|figurines?|"
                               r"t-?shirts?|hoodies?)\b|\bticket\s*$", re.I)
_SINGLE_META_RE = re.compile(r"\b(?:singles?|cards?)\b", re.I)
_VAR_CODE_NUM_RE = re.compile(r"^([A-Za-z0-9]{2,6})\s+(\d{1,4}[A-Za-z\u2605]?)(?:\s+(?:non[\s-]*)?foil)?$", re.I)
def edition_keys(text):
    """Special editions named in a text: World Championship, Gold Border, Collectors' Edition."""
    return {k for k, rx in _EDITION_RES.items() if rx.search(unescape(text or ""))}
def _is_meta_text(t):
    """True if the text holds only finish, treatment, foil, condition, number or code words, and names no set."""
    t = (t or "").strip(" :,-")
    if not t:
        return True
    if t.isdigit() or _SETNUM_RE.match(t) or _CODE_SPACE_NUM_RE.match(t) or _RARITY_CODE_RE.match(t):
        return True
    c = cond_from_text(t) if len(t) <= 20 else None
    if c and re.fullmatch(r"[A-Za-z/\-\s]+", t) and len(t.split()) <= 3:
        return True
    return bool(scan_traits(t)[3])
def not_a_single(p, title, ptype, tags):
    """True if the product's own details say it is not a single Magic card (a ticket, figure, other game...)."""
    meta = " ".join([ptype] + [str(t) for t in tags])
    if _OTHER_GAME_RE.search(f"{title} {p.get('vendor', '') or ''} {meta}"):
        return True
    if "single" not in ptype.lower() and _NONCARD_TYPE_RE.search(ptype):
        return True
    if any(_NONCARD_TAG_RE.match(str(t).strip()) for t in tags):
        return True
    return bool(_NONCARD_TITLE_RE.search(title))
def variant_identity(v, book):
    """Reads a set code and collector number ("MOC 120", "VOW 164 Foil") or a full set name out of a variant's
    options. Only sets the Scryfall list knows are accepted, so nothing is guessed. Returns (code, number)."""
    if book is None or not book.name_by_code:
        return None, None
    code, number = None, None
    for part in (v.get("title"), v.get("option1"), v.get("option2"), v.get("option3")):
        for piece in re.split(r"\s+/\s+", (part or "").strip()):
            piece = piece.strip()
            if not piece or piece.lower() == "default title":
                continue
            m = _VAR_CODE_NUM_RE.match(piece)
            if m and m.group(1).lower() in book.name_by_code:
                code = code or m.group(1).lower()
                number = number or norm_num(m.group(2))
                continue
            if len(piece) >= 4 and not cond_from_text(piece):
                found = book.lookup_name(piece)
                if found:
                    code = code or found
    return code, number
def clean_name(s):
    return re.sub(r"\s+", " ", s).strip(" -:,|")


def strip_set_prefix(name, loose_texts, book=None):
    """If the title starts with the set name given in vendor or tags, drop that prefix.
    Only a set the Scryfall list confirms is removed, so a tag such as 'Knight' can no longer turn
    'Knight Token' into 'Token'. With no set list loaded nothing is removed."""
    if book is None or not book.name_by_code:
        return name
    words = name.split()
    for text in loose_texts:
        target = norm_set(text)
        if not target or not (book.lookup_name(text) or book.lookup_code(text)):
            continue
        for k in range(1, len(words)):
            if norm_set(" ".join(words[:k])) == target:
                rest = " ".join(words[k:])
                return name if norm(rest) in _GENERIC_REMAINDER else rest
    return name
def _group_texts(chunk):
    return [a or b for a, b in _GROUP_RE.findall(chunk)]


def parse_title(title, loose_texts=(), book=None):
    """
    Splits a listing title into card name, set clues, collector number, foil and treatments.
    Handles these shapes (and combinations of them):
      Name (Borderless) (SET-123) - Set Name          code and number in brackets
      Name (Borderless) [SET - 123]                   same, with spaces
      Name (Showcase) [Set Name]                      set in square brackets
      Name [Foil] - Set Name                          brackets that hold only foil or treatment words
      Name [Set Name] - Foil                          a finish after the set
      Name - SET 123 Foil                             code and number after a dash
      Name SET/123                                    code and number joined by a slash
      Name (SET #123)                                 hash number
      Name 123 (Set Name) - Foil                      number after the name
      Name | Set Name | 6                             pipe layout
      Magic Single - Name                             store prefix
    """
    raw_title = unescape(title or "").strip()
    s = _PREFIX_RE.sub("", raw_title, count=1)
    s = _SUFFIX_RE.sub("", s)
    if " | " in s:  # "Name | Set | 6": rewrite it into the dash layout that is read below
        pieces = [x.strip() for x in s.split(" | ") if x.strip()]
        if 2 <= len(pieces) <= 4 and not _GROUP_RE.search(pieces[0]):
            nums = [x for x in pieces[1:] if re.fullmatch(r"\d{1,4}[A-Za-z\u2605]?", x)]
            rest = [x for x in pieces[1:] if x not in nums]
            s = pieces[0] + (f" #{nums[0]}" if nums else "") + "".join(f" - {x}" for x in rest)
    foil, cond = None, None
    frames, finishes, set_texts, bare_nums, group_texts = set(), set(), [], [], []
    set_code, number = None, None
    m = _FOIL_PREFIX_RE.match(s)
    if m:
        foil, s = True, s[m.end():]
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
                bare_nums.append(t)
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
        m3 = _CODE_SPACE_NUM_RE.match(t.strip())
        if m3:
            set_code = set_code or m3.group(1).lower()
            number = number or norm_num(m3.group(2))
            return
        cleaned = clean_name(t)
        set_texts.append(cleaned)
        if is_group:
            group_texts.append(cleaned)
    mc = _SETNUM_FIND_RE.search(s)
    if mc:
        # A code and number in brackets marks the end of the name, so a dash inside the name is kept.
        set_code, number = mc.group(1).lower(), norm_num(mc.group(2))
        head, tail = s[:mc.start()], s[mc.end():]
        for g in _group_texts(head):
            handle_text(g, True)
        name_part = _GROUP_RE.sub(" ", head)
        for g in _group_texts(tail):
            handle_text(g, True)
        extra_parts = [p for p in re.split(r"\s+-\s+", _GROUP_RE.sub(" ", tail)) if p.strip()]
    else:
        for g in _group_texts(s):
            handle_text(g, True)
        s2 = _GROUP_RE.sub(" ", s)
        if any(not _is_meta_text(b) for b in re.findall(r"\[([^\[\]]+)\]", s)):
            # The set is in square brackets, so a dash left in the title may belong to the card name.
            # A trailing "- Foil" style part is still a finish, not part of the name.
            parts = [p for p in re.split(r"\s+-\s+", s2) if p.strip()]
            if not parts:
                return None
            trailing = []
            while len(parts) > 1 and _is_meta_text(parts[-1]):
                trailing.insert(0, parts.pop())
            name_part, extra_parts = " - ".join(parts), trailing
        else:
            parts = [p for p in re.split(r"\s+-\s+", s2) if p.strip()]
            if not parts:
                return None
            name_part, extra_parts = parts[0], parts[1:]
    if not name_part.strip():
        return None
    for p in extra_parts:
        handle_text(p, False)
    ms = _TRAIL_CODE_SLASH_RE.search(name_part)
    if ms and not set_code:
        set_code, number = ms.group(1).lower(), norm_num(ms.group(2))
        name_part = name_part[:ms.start()]
    mh = _HASHNUM_RE.search(name_part)
    if mh:
        number = number or norm_num(mh.group(1))
        name_part = _HASHNUM_RE.sub(" ", name_part)
    mt = _TRAILNUM_RE.search(name_part)
    if mt:
        number = number or norm_num(mt.group(1))
        name_part = name_part[:mt.start()]
    if not number and bare_nums:
        number = norm_num(bare_nums[0])
    name = clean_name(strip_set_prefix(clean_name(name_part), loose_texts, book))
    front = name.split("//")[0].strip()
    if len(norm(front)) < 2:
        return None
    return {"name": front, "name_key": norm(front), "set_code": set_code, "set_texts": [t for t in set_texts if t],
            "group_texts": group_texts, "edition": frozenset(edition_keys(raw_title)),
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
    return {"pages": 0, "ok": 0, "tried": 0, "products": 0, "variants": 0, "in_stock": 0, "junk": 0,
            "unparsed": 0, "foreign": 0, "not_nm": 0, "nonmtg": 0, "nonsingle": 0, "kept": 0, "with_set": 0,
            "with_number": 0, "dated": 0, "new": 0, "newest": None, "truncated": False, "errors": []}


_MTG_WORD_RE = re.compile(r"\b(?:magic|mtg)\b", re.I)


def looks_like_mtg(p, parsed, tags, loose, book):
    """Used for whole-store feeds, which also hold Pokemon, accessories and other games.
    A listing is accepted only with positive Magic evidence, and never when it names another game."""
    ptype = p.get("product_type", "") or ""
    meta = " ".join([p.get("vendor", "") or "", ptype] + [str(t) for t in tags])
    text = " ".join([p.get("title", "") or "", meta])
    if _OTHER_GAME_RE.search(text):
        return False
    if _MTG_WORD_RE.search(text):
        return True
    if book is None or not book.name_by_code:
        return False
    code_ok = bool(parsed["set_code"] and parsed["set_code"].lower() in book.name_by_code)
    text_ok = any(book.lookup_name(t) or book.lookup_code(t) for t in list(parsed["set_texts"]) + list(loose))
    # Set evidence alone is not enough, because set codes are shared with other games. It must come with a
    # collector number or with the store calling the product a single or a card.
    single_word = bool(_SINGLE_META_RE.search(" ".join([ptype] + [str(t) for t in tags])))
    if code_ok and parsed["number"]:
        return True
    return bool((code_ok or text_ok) and single_word)
def records_from_product(store, base, p, st, samples, need_mtg=False, book=None):
    st["products"] += 1
    title = p.get("title", "") or ""
    ptype = p.get("product_type", "") or ""
    if _JUNK_RE.search(title) and "single" not in ptype.lower():
        st["junk"] += 1
        return []
    tags = p.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not_a_single(p, title, ptype, tags):
        st["nonsingle"] = st.get("nonsingle", 0) + 1
        return []
    loose = loose_candidates(p.get("vendor", ""), ptype, tags)
    parsed = parse_title(title, loose, book)
    if not parsed:
        st["unparsed"] += 1
        return []
    if need_mtg and not looks_like_mtg(p, parsed, tags, loose, book):
        st["nonmtg"] += 1
        return []
    loose_edition = frozenset(edition_keys(" ".join(loose)))
    out = []
    variants = p.get("variants", [])
    for v in variants:
        st["variants"] += 1
        if not v.get("available"):
            continue
        st["in_stock"] += 1
        vtext = variant_text(v)
        if _LANG_RE.search(f"{title} {vtext} {' '.join(str(t) for t in tags)}"):
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
        # finishes and treatments can be written in the variant instead of the title ("NM / Etched Foil")
        v_frames, v_finishes, _vfoil, _pure = scan_traits(vtext)
        finishes = parsed["finishes"] | frozenset(v_finishes)
        frames = parsed["frames"] | frozenset(v_frames)
        if _NONFOIL_RE.search(vtext):
            foil = False
        elif _FOIL_RE.search(vtext):
            foil = True
        else:
            foil = bool(parsed["foil"])
        if finishes:
            foil = True
        v_code, v_number = variant_identity(v, book)
        number = parsed["number"] or v_number
        num_conflict = bool(parsed["number"] and v_number and parsed["number"] != v_number)
        edition = parsed["edition"] | loose_edition | frozenset(edition_keys(vtext))
        url = f"{base}/products/{p.get('handle', '')}"
        if len(variants) > 1 and v.get("id"):
            url += f"?variant={v['id']}"
        added = latest_dt(p.get("created_at"), p.get("published_at"), v.get("created_at"))
        rec = {"store": store, "title": title, "url": url, "price": price, "foil": foil, "cond": cond,
               "added": added,
               "name": parsed["name"], "name_key": parsed["name_key"], "set_code": parsed["set_code"],
               "set_texts": parsed["set_texts"], "group_texts": parsed["group_texts"], "loose": loose,
               "number": number, "frames": frames, "finishes": finishes,
               "edition": edition, "vset": v_code, "num_conflict": num_conflict,
               "sku": v.get("sku") or "", "vtext": vtext,
               "uid": (store, p.get("handle", ""), v.get("id"))}
        out.append(rec)
        st["kept"] += 1
        if len(samples[store]) < 4:
            samples[store].append(rec)
    return out
def read_store(entry, cap, book):
    """Reads one store. Returns (store name, records, stat, samples). Safe to run in a worker thread."""
    name, base, handles, *rest = entry
    mode = rest[0] if rest else "all"
    st = new_stat()
    samples = defaultdict(list)
    records, seen = [], set()
    for handle in handles:
        if out_of_time():
            st["errors"].append("skipped: time budget used up")
            break
        whole = handle == "*"
        url = f"{base}/products.json" if whole else f"{base}/collections/{handle}/products.json"
        st["tried"] += 1
        got_any, hit_cap = False, True
        for page in range(1, cap + 1):
            if out_of_time():
                st["errors"].append("skipped: time budget used up")
                hit_cap = False
                break
            params = {"limit": 250, "page": page}
            if not whole:
                params["sort_by"] = "created-descending"
            try:
                products = http_get(url, params).json().get("products", [])
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
            got_any = True
            for p in products:
                for rec in records_from_product(name, base, p, st, samples, need_mtg=whole, book=book):
                    if rec["uid"] not in seen:
                        seen.add(rec["uid"])
                        records.append(rec)
            if len(products) < 250:
                hit_cap = False
                break
        if hit_cap:
            st["truncated"] = True
        if mode == "first" and got_any:
            break
    return name, records, st, samples


def fetch_all(max_pages=None, book=None, workers=None):
    """Reads every store in parallel. Returns (records, stats, samples) in store-list order."""
    cap = max_pages or MAX_PAGES
    entries = list(SHOPIFY_STORES)
    results = {}
    with ThreadPoolExecutor(max_workers=workers or MAX_WORKERS) as pool:
        futures = {pool.submit(read_store, e, cap, book): e[0] for e in entries}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:  # one broken store must not stop the others
                st = new_stat()
                st["errors"].append(f"unexpected error: {exc}")
                results[name] = (name, [], st, defaultdict(list))
    records, stats, samples = [], {}, {}
    for e in entries:
        name, recs, st, smp = results[e[0]]
        records.extend(recs)
        stats[name] = st
        samples[name] = smp.get(name, [])
    return records, stats, samples


# ---------------------------------------------------------------------------
# 6. Resolving sets and grouping the same card
# ---------------------------------------------------------------------------


def resolve_identity(rec, book):
    """Works out which printing a listing is. Returns (set id, status, qualifiers).
    status is
      verified     a set the Scryfall list knows
      unverified   a set code that nothing could confirm
      text         set words that match no known set (kept as a label only, never counted as a verified set)
      conflicting  the clues name different sets, or the title and variant give different collector numbers
      unknown      no clue at all
    A conflicting or unknown listing gets the id "" so it is never compared as an exact match.
    A special edition (World Championship, Gold Border, Collectors' Edition) is part of the id, so it never joins
    the ordinary set's group. qualifiers are bracketed words that no set explains."""
    edition = rec.get("edition") or frozenset()
    known = book.name_by_code
    cands = []
    def consider(code):
        code = (code or "").lower()
        if code and code in known and code not in cands:
            cands.append(code)
    consider(rec["set_code"])
    consider(rec.get("vset"))
    for t in rec["set_texts"]:
        consider(book.lookup_name(t) or book.lookup_code(t))
    if edition:  # a code that is itself the special edition set wins over a reference to the original set
        own = [c for c in cands if edition_keys(known.get(c, "")) & edition]
        if own:
            cands = own[:1]
    if rec.get("num_conflict") or len(cands) > 1:
        return "", "conflicting", frozenset()
    code = (rec["set_code"] or "").lower()
    if cands:
        sid, status = cands[0], "verified"
    elif code:
        sid, status = code, "unverified"
    elif rec["set_texts"]:
        sid, status = "name:" + norm_set(rec["set_texts"][0]), "text"
    else:
        sid, status = "", "unknown"
        for t in rec["loose"]:
            c = book.lookup_name(t) or book.lookup_code(t)
            if c:
                sid, status = c, "verified"
                break
    if sid and edition:
        own_name = known.get(sid, "") if status == "verified" else ""
        extra = edition - edition_keys(own_name)
        if extra:
            sid = sid + "~" + "+".join(sorted(extra))
    qual = set()
    if STRICT_QUALIFIERS and status in ("verified", "unverified"):
        for t in rec.get("group_texts", []):
            key = norm_set(t)
            if (not key or len(key) < 2 or _QUAL_NOISE_RE.match(key) or edition_keys(t)
                    or book.lookup_name(t) or book.lookup_code(t)):
                continue
            qual.add(key)
    return sid, status, frozenset(qual)
def resolve_set(rec, book):
    return resolve_identity(rec, book)[0]
def prepare(records, book):
    had_catalogue = bool(book.name_by_code)
    for r in records:  # learn name-to-code pairs from titles that show both
        if not r["set_code"] or r.get("edition"):
            continue
        # Bracketed words may be a treatment, not a set, so only a dash-separated set name is learned. A name is
        # never learned for a code the Scryfall list does not know, or for a name that already means another set.
        texts = [t for t in r["set_texts"] if t not in r.get("group_texts", [])]
        code = r["set_code"].lower()
        if texts and (not had_catalogue or code in book.name_by_code) and book.lookup_name(texts[0]) in (None, code):
            book.add(texts[0], code)
    for r in records:
        r["set_id"], r["id_status"], r["qual"] = resolve_identity(r, book)
        r["set_verified"] = r["id_status"] == "verified"
def set_label(set_id, rec, book):
    base, _, ed = set_id.partition("~")
    if base.startswith("name:"):
        label = rec["set_texts"][0] if rec["set_texts"] else base[5:]
    else:
        label = book.name_by_code.get(base) or base.upper()
    if ed:
        label += " (" + ", ".join(EDITION_LABELS.get(k, k) for k in ed.split("+")) + ")"
    return label
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


def review_row(r, kind, why, **extra):
    """A review row for a new listing that could not be compared at all."""
    return {"kind": kind, "why": why, "store": r["store"], "name": r["name"],
            "set": r.get("set_id") or "", "price": r["price"], "added": r.get("added"),
            "url": r["url"], "gap": None, "saving": None, "median": None, "n_others": 0, **extra}


def judge_new_listing(row, ranked, i):
    """Says whether the new listing at ranked[i] would be flagged, using the same rules as the report."""
    r = ranked[i]
    others = [x["price"] for j, x in enumerate(ranked) if j != i]
    med = statistics.median(others)
    gap = 1 - r["price"] / med if med > 0 else 0.0
    saving = med - r["price"]
    need = MIN_CHEAP_GAP if len(others) >= 2 else MIN_CHEAP_GAP_ONE_OTHER
    base = {"store": r["store"], "name": row["name"], "set": row["set"], "price": r["price"],
            "added": r.get("added"), "url": r["url"], "gap": gap, "saving": saving, "median": med,
            "n_others": len(others), "need": need, "number": row["number"]}
    if i != 0:
        return {**base, "kind": "not_cheapest",
                "why": f"not the cheapest: {ranked[0]['store']} has it for {money(ranked[0]['price'])}"}
    if len(others) < MIN_OTHERS_NEW:
        return {**base, "kind": "alone", "why": "no other store has this exact card in stock"}
    if gap < need:
        return {**base, "kind": "gap",
                "why": f"only {gap:.0%} below the median of {len(others)} other store(s), needs {need:.0%}"}
    if saving < MIN_CHEAP_SAVING:
        return {**base, "kind": "saving",
                "why": f"saves only {money(saving)} against the median, needs {money(MIN_CHEAP_SAVING)}"}
    return {**base, "kind": "flagged", "why": "flagged"}


def card_key(r):
    return (r["name_key"], r["foil"], r["finishes"], r.get("edition", frozenset()), r.get("qual", frozenset()))
def loose_compare(L, pool, book, only_unset=False):
    """
    Compares listing L with other stores' listings of the same card when the set cannot be matched
    exactly. Candidates must share the card name, foil type, finish and treatment words, and the
    collector number when both listings show one. Candidates are grouped by set, because the
    listing could belong to any of them. L is judged against each group with the normal rules and
    the group that gives the biggest saving is reported, together with every set it could be.
    Returns a row, or None if there is nothing to compare with.
    only_unset=True limits the candidates to other stores' listings that have no set either.
    """
    cands = [c for c in pool.get(card_key(L), [])
             if c["store"] != L["store"] and c["frames"] == L["frames"]
             and not (L["number"] and c["number"] and L["number"] != c["number"])]
    if only_unset:
        cands = [c for c in cands if not c["set_id"]]
    by_set = defaultdict(list)
    for c in cands:
        by_set[c["set_id"] or ""].append(c)
    options = []
    for sid, cs in by_set.items():
        best = best_per_store(cs)
        if len(best) < MIN_OTHERS_NEW:
            continue
        ranked = sorted(best.values(), key=lambda x: x["price"])
        prices = [x["price"] for x in ranked]
        med = statistics.median(prices)
        gap = 1 - L["price"] / med if med > 0 else 0.0
        saving = med - L["price"]
        need = MIN_CHEAP_GAP if len(prices) >= 2 else MIN_CHEAP_GAP_ONE_OTHER
        cheapest = L["price"] <= prices[0]
        options.append({
            "label": "set unknown" if not sid else set_label(sid, ranked[0], book),
            "n": len(prices), "median": med, "lowest": prices[0], "gap": gap, "saving": saving,
            "need": need, "cheapest": cheapest,
            "ok": cheapest and gap >= need and saving >= MIN_CHEAP_SAVING,
            "entries": [(x["store"], x["price"], x["url"]) for x in ranked]})
    if not options:
        return None
    passing = [o for o in options if o["ok"]]
    pick = max(passing, key=lambda o: o["saving"]) if passing else max(options, key=lambda o: o["gap"])
    if passing:
        why = "possible discount, check the set by hand"
    elif not pick["cheapest"]:
        why = f"not the cheapest: {pick['entries'][0][0]} has a matching card for {money(pick['lowest'])}"
    elif pick["gap"] < pick["need"]:
        why = f"only {pick['gap']:.0%} below the median of {pick['n']} other store(s), needs {pick['need']:.0%}"
    else:
        why = f"saves only {money(pick['saving'])} against the median, needs {money(MIN_CHEAP_SAVING)}"
    return {
        "kind": "loose_flagged" if passing else "loose_other", "why": why,
        "store": L["store"], "name": L["name"], "number": L["number"] or "",
        "set": set_label(L["set_id"], L, book) if L["set_id"] else "set unknown",
        "known": bool(L["set_id"]), "price": L["price"], "added": L.get("added"), "url": L["url"],
        "finish": finish_label(L["foil"], L["finishes"]), "frames": ", ".join(sorted(L["frames"])),
        "gap": pick["gap"], "saving": pick["saving"], "median": pick["median"], "n_others": pick["n"],
        "need": pick["need"], "best_set": pick["label"], "n_sets": len(options),
        "sets": [(o["label"], o["median"], o["n"])
                 for o in sorted(options, key=lambda o: o["median"], reverse=True)[:5]],
        "entries": pick["entries"]}


def analyse(records, book, now=None, days=None, review=None, loose=None):
    """Returns (spreads, cheaps). Both only include cards with at least one new listing.
    If `review` is a list, one row per new listing is appended to it, saying how it was judged.
    If `loose` is a list, new listings whose set is unknown (or whose only match has no set) are
    compared by name, finish and treatment, and the possible discounts are appended to it."""
    now = now or datetime.now(timezone.utc)
    days = NEW_WITHIN_DAYS if days is None else days
    newf = make_is_new(records, now, days)
    prepare(records, book)
    pool = defaultdict(list)
    for r in records:
        pool[card_key(r)].append(r)

    def run_loose(L, only_unset, fallback_kind, fallback_why):
        row = loose_compare(L, pool, book, only_unset=only_unset)
        if row is None:
            if review is not None:
                review.append(review_row(L, fallback_kind, fallback_why))
            return
        if review is not None:
            review.append(row)
        if loose is not None and row["kind"] == "loose_flagged":
            loose.append(row)

    groups = defaultdict(list)
    unset_best = {}
    for r in records:
        if not r["set_id"]:
            if newf(r):  # keep the cheapest listing per store and card, then compare it by name
                k = (r["store"], r["name_key"], r["foil"], r["finishes"], r["frames"], r["number"],
                     r.get("edition"), r.get("qual"))
                if k not in unset_best or r["price"] < unset_best[k]["price"]:
                    unset_best[k] = r
            continue
        groups[(r["name_key"], r["set_id"], r["foil"], r["finishes"], r["qual"])].append(r)

    spreads, cheaps = [], []
    for key, recs in groups.items():
        # A collector number identifies one printing, so numbered listings are compared by number alone
        # (stores word the treatment differently). Listings with no number are compared by the treatment
        # words they carry, and join a numbered printing only when exactly one printing could be meant.
        numbered, unnumbered = defaultdict(list), defaultdict(list)
        for r in recs:
            if r["number"]:
                numbered[r["number"]].append(r)
            else:
                unnumbered[r["frames"]].append(r)
        subs = {n: list(rs) for n, rs in numbered.items()}
        loose_groups = []
        for frames, urs in unnumbered.items():
            if frames:
                owners = [n for n, rs in numbered.items() if any(x["frames"] == frames for x in rs)]
            else:
                owners = [n for n, rs in numbered.items() if all(not x["frames"] for x in rs)]
            if len(owners) == 1:
                subs[owners[0]].extend(urs)
            elif not owners:
                loose_groups.append(urs)
            elif review is not None:
                # several printings could be meant: skipped rather than guessed
                for r in urs:
                    if newf(r):
                        review.append(review_row(r, "ambiguous", "no collector number and several printings exist, "
                                                                  "so it cannot be matched safely"))
        for sub in list(subs.values()) + loose_groups:
            best = best_per_store(sub)  # cheapest in-stock listing at each store
            if len(best) < 2:
                for r in best.values():
                    if newf(r):
                        # nobody else has this exact printing, but another store may list it with no set
                        run_loose(r, True, "alone", "no other store has this exact card in stock")
                continue
            ranked = sorted(best.values(), key=lambda r: r["price"])
            flags = [newf(r) for r in ranked]
            if not any(flags):
                continue  # nothing new about this card, so it is not what we are looking for
            prices = [r["price"] for r in ranked]
            lo, hi = prices[0], prices[-1]
            first = ranked[0]
            row = {
                "name": first["name"], "set": set_label(first["set_id"], first, book),
                "finish": finish_label(first["foil"], first["finishes"]),
                "frames": ", ".join(sorted(set().union(*[r["frames"] for r in sub]))),
                "number": next((r["number"] for r in sub if r["number"]), ""),
                "entries": [(r["store"], r["price"], r["url"], f) for r, f in zip(ranked, flags)],
                "lo": lo, "hi": hi, "ratio": hi / lo, "spread": hi - lo, "added": first.get("added"),
                "cv": statistics.pstdev(prices) / statistics.mean(prices),
                "new_added": max((r["added"] for r, f in zip(ranked, flags) if f and r.get("added")),
                                 default=None),
            }
            if len(best) >= MIN_STORES_SPREAD and row["ratio"] >= MIN_SPREAD_RATIO and row["spread"] >= MIN_SPREAD_ABS:
                spreads.append(row)
            if review is not None:
                for i, (r, is_n) in enumerate(zip(ranked, flags)):
                    if is_n:
                        review.append(judge_new_listing(row, ranked, i))
            others = prices[1:]
            if flags[0] and len(others) >= MIN_OTHERS_NEW:
                med = statistics.median(others)
                gap = 1 - lo / med
                need = MIN_CHEAP_GAP if len(others) >= 2 else MIN_CHEAP_GAP_ONE_OTHER
                if gap >= need and (med - lo) >= MIN_CHEAP_SAVING:
                    cheaps.append({**row, "median_others": med, "gap": gap, "saving": med - lo,
                                   "n_others": len(others)})

    for L in unset_best.values():
        run_loose(L, False, "noset_alone",
                  "set unknown and no other store has a card with this name, finish and treatment")
    spreads.sort(key=lambda r: r["spread"], reverse=True)
    cheaps.sort(key=lambda r: r["saving"], reverse=True)
    if loose is not None:
        loose.sort(key=lambda r: r["saving"], reverse=True)
    return spreads, cheaps


def count_stats(records, stats, now, days):
    """Fills in the per-store counts used by the Store check table. Call after analyse()."""
    newf = make_is_new(records, now, days)
    for r in records:
        st = stats[r["store"]]
        if r.get("set_verified"):
            st["with_set"] += 1
        if r["number"]:
            st["with_number"] += 1
        if r.get("added"):
            st["dated"] += 1
            if st["newest"] is None or r["added"] > st["newest"]:
                st["newest"] = r["added"]
        if newf(r):
            st["new"] += 1


# ---------------------------------------------------------------------------
# 7. Building and sending the email
# ---------------------------------------------------------------------------


TODAY_BG = "#c8f7dc"
def row_open(dt, now):
    """Opens a table row. The row is highlighted when the listing was added today."""
    return f'<tr style="background:{TODAY_BG}">' if is_today(dt, now) else "<tr>"
def today_badge(dt, now):
    if not is_today(dt, now):
        return ""
    return (' <span style="background:#1b8f4a;color:#fff;padding:1px 7px;border-radius:9px;'
            'font-size:11px;font-weight:bold">TODAY</span>')
def keep_recent(rows, now, key="added"):
    """Returns (rows added within the limit, how many rows were left out)."""
    kept = [r for r in rows if is_recent(r.get(key), now)]
    return kept, len(rows) - len(kept)
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
            extra = (f" {s['nonmtg']} products were skipped as not Magic (the set list from Scryfall may be missing)."
                     if s["nonmtg"] else "")
            problems.append(f"{name}: readable, but no in-stock English NM singles were kept.{extra} "
                            f"Check the parse samples in a dry run.")
        else:
            if s["truncated"]:
                problems.append(f"{name}: stopped at the page limit, so some listings were not read")
            if any("time budget" in e for e in s["errors"]):
                problems.append(f"{name}: stopped early because the time budget ran out")
            if s["with_set"] < 0.5 * s["kept"]:
                problems.append(f"{name}: a set could be worked out for under half of its listings, so most of "
                                f"its new cards are compared by name only and need a manual set check")
            if NEW_WITHIN_DAYS > 0:
                newest = s.get("newest")
                if newest is not None and newest < datetime.now(timezone.utc) - timedelta(days=STALE_NEWEST_DAYS):
                    extra = (" Its latest batch is still compared, but it may be old stock."
                             if NEW_MODE == "store" and MAX_STORE_AGE_DAYS <= 0 else "")
                    problems.append(f"{name}: its newest listing is over {STALE_NEWEST_DAYS} days old, so its added "
                                    f"dates may not be updating.{extra}")
                if NEW_MODE == "store" and MAX_STORE_AGE_DAYS > 0 and newest is not None and \
                        newest < datetime.now(timezone.utc) - timedelta(days=MAX_STORE_AGE_DAYS):
                    problems.append(f"{name}: left out of the new-listing check because its newest listing is over "
                                    f"{MAX_STORE_AGE_DAYS} days old")
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


def build_html(spreads, cheaps, stats, notes, book, samples=None, now=None, review=None, loose=None,
               noset_samples=None, hidden=0):
    now = now or datetime.now(timezone.utc)
    days = NEW_WITHIN_DAYS
    working, total, problems = store_summary(stats)
    kept = sum(s["kept"] for s in stats.values())
    new_total = sum(s["new"] for s in stats.values())
    when = now.astimezone(TZ).strftime("%a %d %b %Y")
    if days > 0 and NEW_MODE == "store":
        parts = [f"<p>{new_total} of {kept} in-stock English NM listings are each store's latest additions "
                 f"(added within {days} day(s) of that store's own newest listing). "
                 f"Each one was compared with the cheapest price at every other store. "
                 f"{working} of {total} stores were read on {when}.</p>"]
    elif days > 0:
        parts = [f"<p>{new_total} of {kept} in-stock English NM listings were added in the last {days} day(s). "
                 f"Each one was compared with the cheapest price at every other store. "
                 f"{working} of {total} stores were read on {when}.</p>"]
    else:
        parts = [f"<p>The new-listing restriction is turned off, so all {kept} in-stock English NM listings "
                 f"were compared. {working} of {total} stores were read on {when}.</p>"]
    parts.append(f"<p>Only listings added in the last {MAX_LISTING_AGE_DAYS} days are shown ({hidden} older row(s) left out). Rows with a listing added today are highlighted.</p>")
    tbl = '<table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse;font-size:13px">'

    parts.append("<h3>New listings priced well below the other stores</h3>")
    parts.append(f"<p>A new listing is flagged when it is the cheapest price for that card at every store that has "
                 f"it, at least {MIN_CHEAP_GAP:.0%} below the median of the other stores "
                 f"({MIN_CHEAP_GAP_ONE_OTHER:.0%} if only one other store has it), and saves at least "
                 f"{money(MIN_CHEAP_SAVING)} against that median.</p>")
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
                f"{row_open(r['added'], now)}<td>{card_cell(r)}</td><td>{escape(r['set'])}, {escape(r['finish'])}</td>"
                f"<td>{money(r['lo'])}<br>{escape(first[0])}<br>added {fmt_added(r['added'])} ({fmt_age(r['added'], now)}){today_badge(r['added'], now)}</td>"
                f"<td>{money(r['median_others'])}<br>({r['n_others']} other store{plural}){weak}</td>"
                f"<td>{money(r['saving'])} ({r['gap']:.0%})</td><td>{prices_cell(r['entries'])}</td></tr>")
        parts.append("</table>")
        if len(cheaps) > MAX_ROWS:
            parts.append(f"<p>Showing {MAX_ROWS} of {len(cheaps)}.</p>")
    else:
        parts.append("<p>None today.</p>")

    if loose is not None:
        parts.append("<h3>Possible discounts where the set is unknown (check the set by hand)</h3>")
        parts.append("<p>These new listings could not be matched to an exact set. They were compared by card name, "
                     "foil type, finish, treatment and collector number (when both show one) against the same card "
                     "in other stores. The set is unknown, so each one is judged against every set it could be and "
                     "flagged if it would be a discount for at least one of them. Before buying, open the listing "
                     "and confirm which printing it is. The 'Sets it could be' column shows what each possible set "
                     "costs elsewhere.</p>")
        if loose:
            parts.append(tbl + "<tr><th>Card</th><th>New listing</th><th>Best match</th><th>Saving</th>"
                         "<th>Sets it could be (median price, stores)</th><th>Compare</th></tr>")
            for x in loose[:MAX_ROWS]:
                plural = "" if x["n_others"] == 1 else "s"
                shown = x["set"] if x["known"] else "set unknown"
                sets_txt = "<br>".join(f"{escape(l)}: {money(m)} ({n})" for l, m, n in x["sets"])
                if x["n_sets"] > len(x["sets"]):
                    sets_txt += f"<br>and {x['n_sets'] - len(x['sets'])} more"
                weak = ""
                if x["n_others"] == 1:
                    weak = '<br><span style="color:#b00">only one other store</span>'
                who = ("The other store's listing has no set." if x["known"]
                       else "This listing has no set.")
                parts.append(
                    f"{row_open(x['added'], now)}<td>{card_cell(x)}<br><span style=\"color:#666\">{escape(x['finish'])}</span></td>"
                    f"<td><a href=\"{escape(x['url'])}\">{money(x['price'])}</a><br>{escape(x['store'])}"
                    f"<br>{escape(shown)}<br>added {fmt_added(x['added'])} ({fmt_age(x['added'], now)}){today_badge(x['added'], now)}</td>"
                    f"<td>{escape(x['best_set'])}: {money(x['median'])}<br>({x['n_others']} other store{plural})"
                    f"{weak}<br><span style=\"color:#666\">{who}</span></td>"
                    f"<td>{money(x['saving'])} ({x['gap']:.0%})</td><td>{sets_txt}</td>"
                    f"<td>" + "<br>".join(f"<a href=\"{escape(u)}\">{escape(s)}</a> {money(p)}"
                                          for s, p, u in x["entries"][:5]) + "</td></tr>")
            parts.append("</table>")
            if len(loose) > MAX_ROWS:
                parts.append(f"<p>Showing {MAX_ROWS} of {len(loose)}.</p>")
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
                f"{row_open(r['new_added'], now)}<td>{card_cell(r)}{flag}{today_badge(r['new_added'], now)}</td><td>{escape(r['set'])}, {escape(r['finish'])}</td>"
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

    if review is not None:
        parts.append("<h3>How each new listing was judged</h3>")
        labels = [("flagged", "flagged (shown in the first table)"),
                  ("loose_flagged", "set unknown, possible discount (shown in the set-unknown table)"),
                  ("loose_other", "set unknown, compared by name but not cheap enough"),
                  ("noset_alone", "set unknown and no other store has a card with that name and finish"),
                  ("alone", "no other store has the exact card in stock"),
                  ("ambiguous", "no collector number, several printings"),
                  ("not_cheapest", "another store is cheaper"), ("gap", "gap below the threshold"),
                  ("saving", "saving below the dollar minimum")]
        counts = defaultdict(int)
        for x in review:
            counts[x["kind"]] += 1
        if review:
            parts.append("<ul>" + "".join(f"<li>{counts[k]}: {escape(t)}</li>" for k, t in labels if counts[k]) + "</ul>")
            near = sorted([x for x in review if x["kind"] in ("not_cheapest", "gap", "saving", "loose_other")],
                          key=lambda x: x["gap"], reverse=True)
            if near:
                parts.append("<p>Closest to being flagged (the gap is measured against the median of the other stores):</p>")
                parts.append(tbl + "<tr><th>Card</th><th>New listing</th><th>Median of others</th><th>Gap</th>"
                             "<th>Saving</th><th>Why it was not flagged</th></tr>")
                for x in near[:25]:
                    plural = "" if x["n_others"] == 1 else "s"
                    parts.append(
                        f"{row_open(x['added'], now)}<td><a href=\"{escape(x['url'])}\">{escape(x['name'])}</a> "
                        f"<span style=\"color:#666\">({escape(str(x['set']))}"
                        f"{', #' + escape(str(x['number'])) if x.get('number') else ''})</span></td>"
                        f"<td>{money(x['price'])}<br>{escape(x['store'])}<br>added {fmt_added(x['added'])} ({fmt_age(x['added'], now)}){today_badge(x['added'], now)}</td>"
                        f"<td>{money(x['median'])}<br>({x['n_others']} other store{plural})</td>"
                        f"<td>{x['gap']:.0%}</td><td>{money(x['saving'])}</td><td>{escape(x['why'])}</td></tr>")
                parts.append("</table>")
        else:
            parts.append("<p>There were no new listings to judge.</p>")

    parts.append("<h3>Store check</h3>")
    parts.append(tbl + "<tr><th>Store</th><th>Pages</th><th>Products</th><th>In-stock variants</th>"
                 f"<th>Kept (NM, English)</th><th>Not a single card</th><th>With a verified set</th><th>With a number</th>"
                 f"<th>With an added date</th><th>{'Latest batch' if NEW_MODE == 'store' else 'New'} ({days}d)</th>"
                 f"<th>Newest listing</th></tr>")
    for name, s in stats.items():
        trunc = " (hit page limit)" if s["truncated"] else ""
        parts.append(f"<tr><td>{escape(name)}</td><td>{s['pages']}{trunc}</td><td>{s['products']}</td>"
                     f"<td>{s['in_stock']}</td><td>{s['kept']}</td><td>{s['junk'] + s.get('nonsingle', 0) + s['nonmtg']}</td><td>{s['with_set']}</td>"
                     f"<td>{s['with_number']}</td><td>{s['dated']}</td><td>{s['new']}</td>"
                     f"<td>{fmt_added(s.get('newest'))} ({fmt_age(s.get('newest'), now)})</td></tr>")
    parts.append("</table>")

    if problems:
        parts.append("<h3>Problems</h3><ul>" + "".join(f"<li>{escape(p)}</li>" for p in problems) + "</ul>")
    parts.append("<h3>Notes</h3><ul>" + "".join(f"<li>{escape(n)}</li>" for n in notes) + "</ul>")

    if samples:
        parts.append("<h3>Parse samples (dry run only)</h3>")
        parts.append("<p>The first few listings kept from each store that were added recently. This only shows whether "
                     "names, sets and numbers are being read correctly. It does not affect the results.</p>")
        parts.append(tbl + "<tr><th>Store</th><th>Listing title</th><th>Read as</th></tr>")
        for store, recs in samples.items():
            for r in recs:
                read = (f"name: {r['name']}; set: {r.get('set_id') or 'unknown'}; number: {r['number'] or '-'}; "
                        f"{finish_label(r['foil'], r['finishes'])}; frames: {', '.join(sorted(r['frames'])) or '-'}; "
                        f"cond: {r['cond']}; price: {money(r['price'])}; added: {fmt_added(r.get('added'))}; identity: {r.get('id_status') or '-'}")
                parts.append(f"{row_open(r.get('added'), now)}<td>{escape(store)}</td><td>{escape(r['title'])}</td><td>{escape(read)}</td></tr>")
        parts.append("</table>")
    if samples is not None and noset_samples:
        parts.append("<h3>Listings with no set (dry run only)</h3>")
        parts.append("<p>Up to three per store, with every field the store gives that might hold the set. "
                     "If you can see the set in one of these fields, tell me which and I can read it.</p>")
        parts.append(tbl + "<tr><th>Store</th><th>Listing title</th><th>Vendor, type and tags</th>"
                     "<th>SKU</th><th>Variant options</th></tr>")
        for store, recs in noset_samples.items():
            for r in recs:
                parts.append(f"<tr><td>{escape(store)}</td><td>{escape(r['title'])}</td>"
                             f"<td>{escape(' | '.join(r['loose'][:8]) or '-')}</td>"
                             f"<td>{escape(r.get('sku') or '-')}</td><td>{escape(r.get('vtext') or '-')}</td></tr>")
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
    global NEW_WITHIN_DAYS, NEW_MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="write report.html instead of emailing")
    parser.add_argument("--max-pages", type=int, default=None, help="pages per collection (quick test)")
    parser.add_argument("--new-days", type=int, default=None,
                        help="size of the new-listing window in days (0 turns the restriction off)")
    parser.add_argument("--new-mode", choices=["store", "calendar"], default=None,
                        help="store: window ends at each store's newest listing. calendar: window ends today")
    args = parser.parse_args()
    if args.new_days is not None:
        NEW_WITHIN_DAYS = max(0, args.new_days)
    if args.new_mode:
        NEW_MODE = args.new_mode
    now = datetime.now(timezone.utc)

    book = SetBook()
    loaded, err = (load_scryfall_sets(book) if USE_SCRYFALL else (0, "turned off"))
    records, stats, samples = fetch_all(args.max_pages, book)
    review, loose = [], []
    spreads, cheaps = analyse(records, book, now, review=review, loose=loose)
    count_stats(records, stats, now, NEW_WITHIN_DAYS)
    # Only listings added in the last MAX_LISTING_AGE_DAYS days are shown (Adelaide dates).
    cheaps, h_cheap = keep_recent(cheaps, now)
    loose, h_loose = keep_recent(loose, now)
    spreads, h_spread = keep_recent(spreads, now, "new_added")
    review, _ = keep_recent(review, now)
    hidden = h_cheap + h_loose + h_spread
    samples = {s: [r for r in recs if is_recent(r.get("added"), now)] for s, recs in samples.items()}
    samples = {s: recs for s, recs in samples.items() if recs}

    noset_samples = defaultdict(list)
    for r in records:
        if not r.get("set_id") and is_recent(r.get("added"), now) and len(noset_samples[r["store"]]) < 3:
            noset_samples[r["store"]].append(r)

    notes = [
        "Only in-stock, English, Near Mint listings are compared. If a store does not state a condition, NM is assumed.",
        ("A listing counts as new when the store's feed says its product was created or published, or its variant "
         "was created, within the window. In per-store mode the window is measured back from each store's own "
         "newest listing, so a store that has not added stock lately still contributes its latest batch, and the "
         "age shown next to each listing tells you how old that batch is. Stores can reset these dates when they "
         "re-import stock, so watch the Latest batch column in the Store check table.") if NEW_MODE == "store" else
        ("A listing counts as new when the store's feed says its product was created or published, or its variant "
         "was created, inside the window. Stores can reset these dates when they re-import stock, so watch the "
         "New column in the Store check table."),
        "Each new listing is compared with the cheapest in-stock price at every other store, new or old. "
        "A new listing that no other store carries cannot be judged, so it does not appear.",
        "A new listing with no set is compared by card name, foil type, finish and treatment instead of being "
        "skipped. It is judged against every set the card exists in, so some flags will be the wrong printing. "
        "Treat the set-unknown table as a list to check by hand.",
        "Stores marked as whole-store feeds are read from their full product list and filtered to Magic cards "
        "using the Scryfall set list, so a missing set list means those stores return nothing.",
        "Prices are the cheapest in-stock listing per store, in AUD, and do not include shipping. "
        "For cards under about $15, shipping can cancel out the saving.",
        "Two listings count as the same card only when name, set, foil type, treatment and collector number agree, "
        "except for the set-unknown comparison described above.",
        (f"Set names loaded from Scryfall: {loaded}." if loaded
         else f"Scryfall set list not loaded ({err}). Set matching relied on codes shown in listing titles."),
    ]
    working, total, _ = store_summary(stats)
    new_total = sum(s["new"] for s in stats.values())
    html = build_html(spreads, cheaps, stats, notes, book, samples if args.dry_run else None, now, review, loose,
                      noset_samples if args.dry_run else None, hidden)
    subject = (f"MTG new singles: {len(cheaps)} cheap new listing(s), {len(loose)} set-unknown to check, "
               f"{len(spreads)} spread(s), {new_total} new scanned, {working}/{total} stores OK")
    if args.dry_run:
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(subject, "-> report.html written")
    else:
        send_mail(subject, html)
        print(subject, "-> email sent")
    return 0

import store_config
store_config.load_into(globals(), "stores-singles.txt", kind="singles")
if __name__ == "__main__":
    sys.exit(main())
