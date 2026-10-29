#!/usr/bin/env python3
"""
patch_recent_only.py: makes the MTG singles page show only listings added in the last 5 days,
and highlights rows that have a listing added today.

Safe to run in production? Yes. It only edits two local files (mtg_singles_bot.py and build_site.py),
makes no web requests and touches nothing else. It saves a copy of each file as <name>.bak first, and it
stops without changing anything if any of the text it expects to find is missing or found twice.

Usage (run it from the repo folder):
    python patch_recent_only.py
    python patch_recent_only.py --bot path/to/mtg_singles_bot.py --site path/to/build_site.py
"""
import argparse
import re
import shutil
import sys
from pathlib import Path


def load(path):
    raw = Path(path).read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), crlf


def save(path, text, crlf):
    if crlf:
        text = text.replace("\n", "\r\n")
    Path(path).write_bytes(text.encode("utf-8"))


def swap(text, old, new, label, count=1):
    """Replaces old with new. Stops if old is not found exactly `count` time(s)."""
    found = text.count(old)
    if found != count:
        raise SystemExit(f"STOPPED, nothing was changed: '{label}' expected {count} match(es) but found {found}.")
    return text.replace(old, new)


# ---------------------------------------------------------------------------------------------------
# Edits to mtg_singles_bot.py
# ---------------------------------------------------------------------------------------------------
AGE_HELPERS = '''def age_days(dt, now=None):
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
    return "today" if d == 0 else f"{d}d ago"'''

ROW_HELPERS = '''TODAY_BG = "#c8f7dc"
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
def money(x):'''

MAIN_FILTER = '''count_stats(records, stats, now, NEW_WITHIN_DAYS)
    # Only listings added in the last MAX_LISTING_AGE_DAYS days are shown (Adelaide dates).
    cheaps, h_cheap = keep_recent(cheaps, now)
    loose, h_loose = keep_recent(loose, now)
    spreads, h_spread = keep_recent(spreads, now, "new_added")
    review, _ = keep_recent(review, now)
    hidden = h_cheap + h_loose + h_spread
    samples = {s: [r for r in recs if is_recent(r.get("added"), now)] for s, recs in samples.items()}
    samples = {s: recs for s, recs in samples.items() if recs}'''


def patch_bot(text):
    if "MAX_LISTING_AGE_DAYS" in text:
        raise SystemExit("mtg_singles_bot.py already has the 5 day filter, so it was left alone.")

    # 1. the setting
    text = swap(text, "MAX_ROWS = 40",
                "MAX_LISTING_AGE_DAYS = 5     # drop anything added more than this many days ago (Adelaide dates)\n"
                "MAX_ROWS = 40", "MAX_ROWS setting")

    # 2. age helpers (replaces the old fmt_age, which counted 24 hour blocks instead of calendar days)
    text, n = re.subn(r"def fmt_age\(dt, now=None\):.*?(?=\ndef fmt_added)", lambda m: AGE_HELPERS,
                      text, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("STOPPED, nothing was changed: could not find fmt_age().")

    # 3. row helpers, placed just before money()
    text = swap(text, "def money(x):", ROW_HELPERS, "def money")

    # 4. the spread rows remember the date of their newest NEW listing
    text = swap(text, '"cv": statistics.pstdev(prices) / statistics.mean(prices),',
                '"cv": statistics.pstdev(prices) / statistics.mean(prices),\n'
                '                "new_added": max((r["added"] for r, f in zip(ranked, flags) if f and r.get("added")),\n'
                '                                 default=None),', "spread row")

    # 5. build_html takes the count of hidden rows
    text = swap(text, "noset_samples=None):", "noset_samples=None, hidden=0):", "build_html signature")

    # 6. a line under the summary
    tbl = "tbl = '<table border=\"1\" cellpadding=\"5\" cellspacing=\"0\" style=\"border-collapse:collapse;font-size:13px\">'"
    text = swap(text, tbl,
                'parts.append(f"<p>Only listings added in the last {MAX_LISTING_AGE_DAYS} days are shown '
                '({hidden} older row(s) left out). Rows with a listing added today are highlighted.</p>")\n    ' + tbl,
                "table style line")

    # 7. highlighted rows and TODAY badges
    text = swap(text, 'f"<tr><td>{card_cell(r)}</td>',
                'f"{row_open(r[\'added\'], now)}<td>{card_cell(r)}</td>', "cheap row")
    text = swap(text, "({fmt_age(r['added'], now)})</td>\"",
                "({fmt_age(r['added'], now)}){today_badge(r['added'], now)}</td>\"", "cheap age cell")
    text = swap(text, 'f"<tr><td>{card_cell(x)}',
                'f"{row_open(x[\'added\'], now)}<td>{card_cell(x)}', "set-unknown row")
    text = swap(text, "({fmt_age(x['added'], now)})</td>\"",
                "({fmt_age(x['added'], now)}){today_badge(x['added'], now)}</td>\"", "age cell (2 tables)", count=2)
    text = swap(text, 'f"<tr><td>{card_cell(r)}{flag}</td>',
                'f"{row_open(r[\'new_added\'], now)}<td>{card_cell(r)}{flag}{today_badge(r[\'new_added\'], now)}</td>',
                "spread row")
    text = swap(text, 'f"<tr><td><a href=\\"{escape(x[\'url\'])}\\">',
                'f"{row_open(x[\'added\'], now)}<td><a href=\\"{escape(x[\'url\'])}\\">', "closest-to-flagged row")
    text = swap(text, "f\"<tr><td>{escape(store)}</td><td>{escape(r['title'])}</td><td>{escape(read)}</td></tr>\"",
                "f\"{row_open(r.get('added'), now)}<td>{escape(store)}</td><td>{escape(r['title'])}</td>"
                "<td>{escape(read)}</td></tr>\"", "parse sample row")

    # 8. the parse samples table is skipped when nothing recent is left in it
    text = swap(text, "if samples is not None:", "if samples:", "samples heading")
    text = swap(text, ", whatever their date.", " that were added recently.", "samples wording")

    # 9. main(): filter everything, then pass the count of hidden rows on
    text = swap(text, "count_stats(records, stats, now, NEW_WITHIN_DAYS)", MAIN_FILTER, "main filter")
    text = swap(text, 'if not r.get("set_id") and len(noset_samples[r["store"]]) < 3:',
                'if not r.get("set_id") and is_recent(r.get("added"), now) and len(noset_samples[r["store"]]) < 3:',
                "no-set samples")
    text = swap(text, "noset_samples if args.dry_run else None)", "noset_samples if args.dry_run else None, hidden)",
                "build_html call")
    return text


# ---------------------------------------------------------------------------------------------------
# Edit to build_site.py: the highlight colour for the dark theme
# ---------------------------------------------------------------------------------------------------
SITE_CSS = '''/* rows with a listing added today (singles page) */
tr[style*="#c8f7dc"]{background:transparent !important}
tr[style*="#c8f7dc"] > td{background:rgba(94,234,150,.14)}
tr[style*="#c8f7dc"] > td:first-child{box-shadow:inset 3px 0 0 #5eea96}
/* the archive list */'''


def patch_site(text):
    if "#c8f7dc" in text:
        raise SystemExit("build_site.py already has the today highlight, so it was left alone.")
    return swap(text, "/* the archive list */", SITE_CSS, "archive list comment")


def main():
    ap = argparse.ArgumentParser(description="Show only listings from the last 5 days and highlight today's.")
    ap.add_argument("--bot", default="mtg_singles_bot.py")
    ap.add_argument("--site", default="build_site.py")
    args = ap.parse_args()
    for p in (args.bot, args.site):
        if not Path(p).is_file():
            raise SystemExit(f"{p} was not found. Run this from the repo folder or use --bot and --site.")
    bot, bot_crlf = load(args.bot)
    site, site_crlf = load(args.site)
    new_bot, new_site = patch_bot(bot), patch_site(site)  # both are checked before either file is written
    for p in (args.bot, args.site):
        shutil.copy2(p, p + ".bak")
    save(args.bot, new_bot, bot_crlf)
    save(args.site, new_site, site_crlf)
    print(f"Patched {args.bot} and {args.site}. Originals saved as .bak files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
