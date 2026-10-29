#!/usr/bin/env python3
"""
patch_identity_fixes.py: fixes the printing-identity problems in mtg_singles_bot.py.

What it changes (each one matches an item in the review issue):
  1. World Championship / Gold Border / Collectors' Edition listings no longer join the ordinary set group.
  2. Finishes and treatments written in a variant's options (for example "NM / Etched Foil") are kept, and a
     bracketed word that no set explains (for example "Shattered Glass") keeps a listing out of the plain
     printing's group.
  3. Set code and collector number are read from variant options ("MOC 120", "VOW 164 Foil", full set names).
     "Special Guests" is no longer read as the code "guests". Clues that name different sets are marked
     conflicting and are compared by hand only, never as an exact match. Alias learning is guarded.
  4. Title layouts: "Magic Single - Card", "Card | Set | 6", "Card [Foil] - Set", "Card [Set] - Foil" and
     "Knight Token" are read correctly. Prefixes are only removed when they are a real set.
  5. Event tickets, figures, other games and similar products are skipped, so they no longer count as singles
     or move a store's newest-listing date.
  Coverage now counts verified sets only. The Store check table gets a "Not a single card" column.

Safe to run in production? Yes. It only edits mtg_singles_bot.py on this computer, makes no web requests, saves a
copy as mtg_singles_bot.py.bak first, and stops without changing anything if any text it expects is missing or
found twice.

Usage (run it from the repo folder):
    python patch_identity_fixes.py
    python patch_identity_fixes.py --bot path/to/mtg_singles_bot.py
"""
import argparse
import re
import shutil
import sys
from pathlib import Path


def load(path):
    raw = Path(path).read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def save(path, text, crlf):
    Path(path).write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def swap(text, old, new, label, count=1):
    found = text.count(old)
    if found != count:
        raise SystemExit(f"STOPPED, nothing was changed: '{label}' expected {count} match(es) but found {found}.")
    return text.replace(old, new)


def swap_line(text, pattern, new, label):
    """Replaces one whole line that starts with `pattern` (a regular expression)."""
    text2, n = re.subn(r"^" + pattern + r".*$", lambda m: new, text, count=0, flags=re.M)
    if n != 1:
        raise SystemExit(f"STOPPED, nothing was changed: '{label}' expected 1 match but found {n}.")
    return text2


def swap_func(text, start, end, new, label):
    """Replaces everything from the line starting `start` up to (not including) the line starting `end`."""
    a = text.count("\n" + start)
    b = text.count("\n" + end)
    if a != 1 or b != 1:
        raise SystemExit(f"STOPPED, nothing was changed: '{label}' markers found {a} and {b} time(s), expected 1 each.")
    i = text.index("\n" + start) + 1
    j = text.index("\n" + end, i) + 1
    return text[:i] + new.rstrip("\n") + "\n" + text[j:]


# ---------------------------------------------------------------------------------------------------
# New code
# ---------------------------------------------------------------------------------------------------
PREFIX_LINE = (r'_PREFIX_RE = re.compile(r"^\s*(?:(?:magic|mtg)(?:\s*:?\s*the\s*gathering)?\s+singles?\s*[-:|]\s+|'
               r'magic\s*:?\s*the\s*gathering\s*[-:|]?\s*|(?:magic|mtg)\s*[-:|]\s+)", re.I)')

RARITY_LINE = (r'_RARITY_CODE_RE = re.compile(r"^(?i:common|uncommon|rare|mythic(?:\s+rare)?|special|bonus)\s+'
               r'((?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,6})$")  # the code must be capitals, so "Special Guests" is not a code')

HELPERS = r'''# ---------------------------------------------------------------------------
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
def clean_name(s):'''

STRIP_PREFIX = r'''def strip_set_prefix(name, loose_texts, book=None):
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
'''

PARSE_TITLE = r'''def parse_title(title, loose_texts=(), book=None):
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
'''

RECORDS = r'''def looks_like_mtg(p, parsed, tags, loose, book):
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
'''

RESOLVE = r'''def resolve_identity(rec, book):
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
'''

SET_LABEL = r'''def set_label(set_id, rec, book):
    base, _, ed = set_id.partition("~")
    if base.startswith("name:"):
        label = rec["set_texts"][0] if rec["set_texts"] else base[5:]
    else:
        label = book.name_by_code.get(base) or base.upper()
    if ed:
        label += " (" + ", ".join(EDITION_LABELS.get(k, k) for k in ed.split("+")) + ")"
    return label
'''

CARD_KEY = r'''def card_key(r):
    return (r["name_key"], r["foil"], r["finishes"], r.get("edition", frozenset()), r.get("qual", frozenset()))
'''


def patch_bot(text):
    if "STRICT_QUALIFIERS" in text:
        raise SystemExit("mtg_singles_bot.py already has the identity fixes, so it was left alone.")
    text = swap_line(text, r"_PREFIX_RE = ", PREFIX_LINE, "_PREFIX_RE")
    text = swap_line(text, r"_RARITY_CODE_RE = ", RARITY_LINE, "_RARITY_CODE_RE")
    text = swap(text, "def clean_name(s):", HELPERS, "def clean_name")
    text = swap_func(text, "def strip_set_prefix(", "def _group_texts(", STRIP_PREFIX, "strip_set_prefix")
    text = swap_func(text, "def parse_title(", "def loose_candidates(", PARSE_TITLE, "parse_title")
    text = swap(text, '"nonmtg": 0, "kept": 0', '"nonmtg": 0, "nonsingle": 0, "kept": 0', "new_stat")
    text = swap_func(text, "def looks_like_mtg(", "def read_store(", RECORDS, "looks_like_mtg and records_from_product")
    text = swap_func(text, "def resolve_set(", "def set_label(", RESOLVE, "resolve_set and prepare")
    text = swap_func(text, "def set_label(", "def finish_label(", SET_LABEL, "set_label")
    text = swap_func(text, "def card_key(", "def loose_compare(", CARD_KEY, "card_key")
    text = swap(text, 'groups[(r["name_key"], r["set_id"], r["foil"], r["finishes"])]',
                'groups[(r["name_key"], r["set_id"], r["foil"], r["finishes"], r["qual"])]', "group key")
    text = swap(text, 'k = (r["store"], r["name_key"], r["foil"], r["finishes"], r["frames"], r["number"])',
                'k = (r["store"], r["name_key"], r["foil"], r["finishes"], r["frames"], r["number"],\n'
                '                     r.get("edition"), r.get("qual"))', "unset key")
    text, n = re.subn(r'if r\.get\("set_id"\):(\s+)st\["with_set"\] \+= 1',
                      lambda m: 'if r.get("set_verified"):' + m.group(1) + 'st["with_set"] += 1', text)
    if n != 1:
        raise SystemExit(f"STOPPED, nothing was changed: 'with_set count' expected 1 match but found {n}.")
    text = swap(text, "<th>Kept (NM, English)</th><th>With a set</th>",
                "<th>Kept (NM, English)</th><th>Not a single card</th><th>With a verified set</th>", "store check header")
    text = swap(text, "<td>{s['kept']}</td><td>{s['with_set']}</td>",
                "<td>{s['kept']}</td><td>{s['junk'] + s.get('nonsingle', 0) + s['nonmtg']}</td><td>{s['with_set']}</td>",
                "store check row")
    text = swap(text, "added: {fmt_added(r.get('added'))}\")",
                "added: {fmt_added(r.get('added'))}; identity: {r.get('id_status') or '-'}\")", "parse sample")
    return text


def main():
    ap = argparse.ArgumentParser(description="Fix the printing-identity problems in the MTG singles bot.")
    ap.add_argument("--bot", default="mtg_singles_bot.py")
    args = ap.parse_args()
    if not Path(args.bot).is_file():
        raise SystemExit(f"{args.bot} was not found. Run this from the repo folder or use --bot.")
    text, crlf = load(args.bot)
    new = patch_bot(text)
    compile(new, args.bot, "exec")  # a syntax error here means nothing is written
    shutil.copy2(args.bot, args.bot + ".bak")
    save(args.bot, new, crlf)
    print(f"Patched {args.bot}. The original is saved as {args.bot}.bak.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
