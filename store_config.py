#!/usr/bin/env python3
"""
store_config.py: keeps the list of stores in a plain text file, so adding a store means editing
that file on GitHub instead of changing the bot's code.

Safe to run in production? Yes. Loading a stores file only reads a local text file. The check and
discover commands make read-only web requests (one request per second per store) and change nothing.

How a bot uses it (two lines, placed directly above the line  if __name__ == "__main__":  ):

    import store_config
    store_config.load_into(globals(), "stores-singles.txt", kind="singles")

kind is "singles" for the singles bot and "sealed" for the sealed product bot. The lines replace the
store lists written in the bot. If the file is missing or has no usable stores, the bot keeps the
lists written in its own code and says so in the report. Mistakes on individual lines are skipped and
listed under "Problems with the stores file" in the report, so one typo never stops the daily run.

The stores file has one store per line, with the parts separated by the | character:

    Name | Website | Collection | Options

Command line:

    python store_config.py check                      check the stores file(s) and test every store
    python store_config.py check stores-singles.txt   check one file
    python store_config.py discover https://shop.example.com.au     list a store's Magic collections
    python store_config.py export mtg_singles_bot.py  print the stores written in a bot as a stores file
"""
import argparse
import importlib.util
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape
from urllib.parse import urlsplit

USER_AGENT = "Mozilla/5.0 (personal price checker)"
TIMEOUT = 20
DELAY = 1.0          # seconds between requests to one store
MAX_WORKERS = 6

_OPTIONS = ("first", "all", "off", "woo", "page")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._~%\-]+(?:/[A-Za-z0-9._~%\-]+)*$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_NOT_COLLECTION = {"pages", "blogs", "cart", "search", "account", "products"}
_KINDS = ("singles", "sealed")
_LIST_NAMES = {"singles": ("SHOPIFY_STORES",), "sealed": ("SHOPIFY_STORES", "WOO_STORES", "ITEMCODE_STORES")}


# ---------------------------------------------------------------------------
# 1. Reading the stores file
# ---------------------------------------------------------------------------


class StoreConfig:
    """The result of reading a stores file."""

    def __init__(self, kind, path):
        self.kind = kind
        self.path = path
        self.found = True
        self.entries = []        # enabled stores, in file order
        self.problems = []       # readable messages, each starting with its line number
        self.problem_lines = []  # the line numbers, in the same order
        self.switched_off = 0

    def add_problem(self, line, text):
        self.problems.append(f"Line {line}: {text}" if line else text)
        self.problem_lines.append(line)

    def lists(self):
        """The store lists in the exact shapes the bots expect."""
        if self.kind == "sealed":
            return {
                "SHOPIFY_STORES": [(e["name"], e["base"], list(e["handles"]))
                                   for e in self.entries if e["type"] == "shopify"],
                "WOO_STORES": [(e["name"], e["base"]) for e in self.entries if e["type"] == "woo"],
                "ITEMCODE_STORES": [(e["name"], e["base"], list(e["handles"]))
                                    for e in self.entries if e["type"] == "page"],
            }
        out = []
        for e in self.entries:
            item = (e["name"], e["base"], list(e["handles"]))
            out.append(item + ("first",) if e["mode"] == "first" else item)
        return {"SHOPIFY_STORES": out}


def _host_key(base):
    host = urlsplit(base).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _split_address(raw):
    """Returns (base, path, query). The base is the site only, for example https://example.com.au."""
    a = (raw or "").strip()
    if not a:
        raise ValueError("the website address is empty")
    if not _SCHEME_RE.match(a):
        a = "https://" + a.lstrip("/")
    parts = urlsplit(a)
    if parts.scheme not in ("http", "https") or not parts.netloc or " " in parts.netloc or "." not in parts.netloc:
        raise ValueError(f"'{raw.strip()}' does not look like a web address")
    return f"{parts.scheme}://{parts.netloc.lower()}", parts.path, parts.query


def _shopify_handle_from_path(path, strict):
    """Turns a path such as /collections/mtg-singles into the collection name 'mtg-singles'."""
    p = path.split("?")[0].split("#")[0].strip().strip("/")
    low = p.lower()
    if strict and not low.startswith("collections/"):
        raise ValueError(f"'{path}' does not contain /collections/, so I cannot tell which collection it is")
    if low.startswith("collections/"):
        p = p[len("collections/"):]
    elif low == "collections":
        raise ValueError("that is the list of all collections, not one collection")
    segments = p.split("/")
    lowered = [x.lower() for x in segments]
    if "products" in lowered:
        cut = lowered.index("products")
        if cut == 0:
            raise ValueError("that is a product page, not a collection")
        p = "/".join(segments[:cut])
    if p.lower().endswith("products.json"):
        p = p[:-len("products.json")].strip("/")
    p = p.strip("/")
    if not p:
        raise ValueError("the collection name is empty")
    if p.split("/")[0].lower() in _NOT_COLLECTION:
        raise ValueError(f"'{p}' is not a collection address")
    if not _HANDLE_RE.match(p):
        raise ValueError(f"'{p}' has characters a collection name cannot have")
    return p


def _shopify_handle(item, base):
    it = item.strip()
    if it == "*":
        return "*"
    if _SCHEME_RE.match(it) or it.startswith("//"):
        parts = urlsplit(it if "://" in it else "https:" + it)
        if _host_key(f"https://{parts.netloc}") != _host_key(base):
            raise ValueError(f"'{it}' is on a different website from {base}")
        return _shopify_handle_from_path(parts.path, strict=True)
    return _shopify_handle_from_path(it, strict=False)


def _page_path(item, base):
    it = item.strip()
    if _SCHEME_RE.match(it):
        parts = urlsplit(it)
        if _host_key(f"https://{parts.netloc}") != _host_key(base):
            raise ValueError(f"'{it}' is on a different website from {base}")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
    else:
        path = it if it.startswith("/") else "/" + it
    if not path.strip("/") or " " in path:
        raise ValueError(f"'{item}' is not a usable page address")
    return path


def _items(cell):
    return [x for x in re.split(r"[,\s]+", (cell or "").strip()) if x]


def parse_text(text, kind, path="the stores file"):
    """Reads the text of a stores file. Never raises for a bad line; the line is skipped and reported."""
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {_KINDS}")
    cfg = StoreConfig(kind, path)
    by_name, by_host = {}, {}
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = re.sub(r"\s+#.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 2:
            cfg.add_problem(number, "expected  Name | Website | Collection | Options  with the parts separated by | characters")
            continue
        if len(cols) > 4:
            cfg.add_problem(number, "too many | characters. There should be at most three of them")
            continue
        name, address = cols[0], cols[1]
        collections = cols[2] if len(cols) > 2 else ""
        options_raw = cols[3] if len(cols) > 3 else ""
        if not name:
            cfg.add_problem(number, "the store name is empty")
            continue
        opts = [o.lower() for o in re.split(r"[,\s]+", options_raw) if o]
        unknown = [o for o in opts if o not in _OPTIONS]
        if unknown:
            cfg.add_problem(number, f"unknown option '{unknown[0]}'. The options are: first, off, woo, page")
            continue
        if "woo" in opts and "page" in opts:
            cfg.add_problem(number, "an entry cannot be both woo and page")
            continue
        stype = "woo" if "woo" in opts else "page" if "page" in opts else "shopify"
        if "off" in opts:
            cfg.switched_off += 1
            continue
        if kind == "singles" and stype != "shopify":
            cfg.add_problem(number, "the singles bot reads Shopify stores only, so woo and page are not available")
            continue
        if "first" in opts and (stype != "shopify" or kind == "sealed"):
            cfg.add_problem(number, "the 'first' option only works for Shopify stores in the singles bot")
            continue
        try:
            base, apath, aquery = _split_address(address)
        except ValueError as exc:
            cfg.add_problem(number, str(exc))
            continue
        handles = []
        try:
            if stype == "shopify":
                if apath.strip("/"):
                    handles.append(_shopify_handle_from_path(apath, strict=True))
                handles += [_shopify_handle(i, base) for i in _items(collections)]
            elif stype == "page":
                if apath.strip("/"):
                    handles.append(apath + (f"?{aquery}" if aquery else ""))
                handles += [_page_path(i, base) for i in _items(collections)]
        except ValueError as exc:
            cfg.add_problem(number, str(exc))
            continue
        if stype in ("shopify", "page") and not handles:
            extra = ", or use * to read the whole store" if kind == "singles" and stype == "shopify" else ""
            cfg.add_problem(number, "no collection given. Put the collection name in the third column, or paste the "
                                    f"full collection address in the second column{extra}")
            continue
        if kind == "sealed" and "*" in handles:
            cfg.add_problem(number, "* (the whole store) is only available in the singles bot. Name a collection instead")
            continue
        handles = list(dict.fromkeys(handles))
        key, host = name.lower(), _host_key(base)
        existing = by_name.get(key)
        if existing:
            if _host_key(existing["base"]) != host:
                cfg.add_problem(number, f"the name '{name}' is already used for {existing['base']} on line {existing['line']}")
                continue
            if existing["type"] != stype:
                cfg.add_problem(number, f"'{name}' is already listed as a {existing['type']} store on line {existing['line']}")
                continue
            existing["handles"] += [h for h in handles if h not in existing["handles"]]
            if "first" in opts:
                existing["mode"] = "first"
            continue
        other = by_host.get(host)
        if other:
            cfg.add_problem(number, f"{base} is already listed as '{other['name']}' on line {other['line']}. "
                                    "Use the same name to add more collections to the same store")
            continue
        entry = {"name": name, "base": base, "type": stype, "handles": handles,
                 "mode": "first" if "first" in opts else "all", "line": number}
        cfg.entries.append(entry)
        by_name[key] = entry
        by_host[host] = entry
    return cfg


def parse_file(path, kind):
    cfg = StoreConfig(kind, path)
    if not os.path.isfile(path):
        cfg.found = False
        return cfg
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    parsed = parse_text(text, kind, path)
    return parsed


# ---------------------------------------------------------------------------
# 2. Plugging the file into a bot
# ---------------------------------------------------------------------------


def _annotate(level, path, line, text):
    """Shows a message in the GitHub Actions run, next to the file and line when possible."""
    if os.environ.get("GITHUB_ACTIONS"):
        where = f" file={path}" + (f",line={line}" if line else "")
        print(f"::{level}{where}::{text}")


def _wrap_build_html(namespace):
    """Adds a 'Problems with the stores file' section to the bot's report. Does nothing twice."""
    original = namespace.get("build_html")
    if original is None:
        print("store_config: the report function was not found, so stores file problems will only show in the log. "
              "Place the two lines directly above  if __name__ == \"__main__\":", flush=True)
        return
    if getattr(original, "_store_config_wrapped", False):
        return

    def build_html(*args, **kwargs):
        html = original(*args, **kwargs)
        problems = namespace.get("STORE_CONFIG_PROBLEMS") or []
        if not problems:
            return html
        block = ("<h3>Problems with the stores file</h3><ul>"
                 + "".join(f"<li>{escape(p)}</li>" for p in problems) + "</ul>")
        marker = "<h3>Store check</h3>"
        if marker in html:
            return html.replace(marker, block + "\n" + marker, 1)
        return html + "\n" + block

    build_html._store_config_wrapped = True
    build_html.__wrapped__ = original
    namespace["build_html"] = build_html


def load_into(namespace, filename, kind="singles", wrap_report=True):
    """
    Replaces the store lists in `namespace` (pass globals() from the bot) with the stores in the file.
    Returns the StoreConfig, or None when the STORE_CONFIG_SKIP environment variable is set.
    """
    if os.environ.get("STORE_CONFIG_SKIP"):
        return None
    base_dir = os.path.dirname(os.path.abspath(namespace.get("__file__") or "."))
    path = filename if os.path.isabs(filename) else os.path.join(base_dir, filename)
    cfg = parse_file(path, kind)
    problems = list(cfg.problems)
    if not cfg.found:
        problems.append(f"{filename} was not found, so the stores written in the script are being used")
        problem_lines = cfg.problem_lines + [0]
    elif not cfg.entries:
        problems.append(f"{filename} has no usable stores, so the stores written in the script are being used")
        problem_lines = cfg.problem_lines + [0]
    else:
        problem_lines = list(cfg.problem_lines)
        for list_name, value in cfg.lists().items():
            namespace[list_name] = value
    namespace["STORE_CONFIG_PROBLEMS"] = problems
    namespace["STORE_CONFIG_FILE"] = filename
    total = sum(len(v) for v in cfg.lists().values()) if cfg.entries else 0
    off = f", {cfg.switched_off} switched off" if cfg.switched_off else ""
    print(f"Stores file {filename}: {total} stores loaded{off}, {len(problems)} problem(s)", flush=True)
    for text, line in zip(problems, problem_lines):
        print(f"  {text}", flush=True)
        _annotate("warning", filename, line, text)
    if wrap_report:
        _wrap_build_html(namespace)
    return cfg


# ---------------------------------------------------------------------------
# 3. Writing a stores file from lists (used to start a file from a bot's own lists)
# ---------------------------------------------------------------------------

HEADER = {
    "singles": """# Stores for the MTG singles bot.
#
# One store per line, with the parts separated by the | character:
#
#   Name | Website | Collection | Options
#
#   Name        any name you like. It shows in the report.
#   Website     the store's web address, for example https://example.com.au
#               You can paste a whole collection address here instead, for example
#               https://example.com.au/collections/mtg-singles, and leave Collection empty.
#   Collection  the last part of the collection address. Separate several with commas.
#               Use * to read the whole store (the bot keeps only the Magic cards).
#   Options     optional, separated by commas:
#                 first   stop at the first collection that works, the others are only fallbacks
#                 off     skip this line without deleting it
#
# To add a store: copy a line, change it, and save. A check runs on GitHub within a minute and shows
# whether the store can be read. Lines starting with # are ignored.
# To find a store's collection names, open Actions, choose "Check stores", select Run workflow,
# and type the store's website in the box.
# To add another collection to a store that is already listed, write another line with the same name
# and website.
#
""",
    "sealed": """# Stores for the MTG sealed product bot.
#
# One store per line, with the parts separated by the | character:
#
#   Name | Website | Collection | Options
#
#   Name        any name you like. It shows in the report.
#   Website     the store's web address, for example https://example.com.au
#               You can paste a whole collection address here instead, for example
#               https://example.com.au/collections/magic-the-gathering, and leave Collection empty.
#   Collection  the last part of the collection address. Separate several with commas.
#   Options     optional, separated by commas:
#                 off     skip this line without deleting it
#                 woo     a WooCommerce store. Leave Collection empty. The bot searches for Magic.
#                 page    a store whose web pages are read directly. Collection holds the page
#                         addresses, for example /brand/magic-the-gathering
#
# To add a store: copy a line, change it, and save. A check runs on GitHub within a minute and shows
# whether the store can be read. Lines starting with # are ignored.
# To find a store's collection names, open Actions, choose "Check stores", select Run workflow,
# and type the store's website in the box.
# To add another collection to a store that is already listed, write another line with the same name
# and website.
#
""",
}


def format_stores_file(lists, kind):
    """Turns the bot's store lists into the text of a stores file."""
    lines = [HEADER[kind].rstrip("\n"), ""]
    for e in lists.get("SHOPIFY_STORES", []):
        name, base, handles, *rest = e
        opt = " | first" if rest and rest[0] == "first" else ""
        lines.append(f"{name} | {base} | {', '.join(handles)}{opt}")
    for name, base in lists.get("WOO_STORES", []):
        lines.append(f"{name} | {base} | | woo")
    for name, base, paths in lists.get("ITEMCODE_STORES", []):
        lines.append(f"{name} | {base} | {', '.join(paths)} | page")
    return "\n".join(lines) + "\n"


def export_from_script(script_path, kind):
    """Imports a bot script without running it and returns the store lists written in it."""
    os.environ["STORE_CONFIG_SKIP"] = "1"
    script_dir = os.path.dirname(os.path.abspath(script_path))
    sys.path.insert(0, script_dir)
    try:
        spec = importlib.util.spec_from_file_location("bot_for_export", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(script_dir)
    return {n: list(getattr(module, n, [])) for n in _LIST_NAMES[kind]}


# ---------------------------------------------------------------------------
# 4. Testing stores and finding collections (read-only web requests)
# ---------------------------------------------------------------------------


def _get(url, params=None):
    import requests
    time.sleep(DELAY)
    return requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)


def _check_response(r):
    """Returns (status, detail) for an HTTP error, or None when the response is fine."""
    if r.status_code == 404:
        return "fail", "not found (check the collection name, or the site may not use this platform)"
    if r.status_code in (401, 403, 429):
        return "fail", f"HTTP {r.status_code} (the store may be blocking this server)"
    if r.status_code >= 400:
        return "fail", f"HTTP {r.status_code}"
    return None


def _probe_shopify(base, handle):
    url = base + ("/products.json" if handle == "*" else f"/collections/{handle}/products.json")
    try:
        r = _get(url, {"limit": 5})
    except Exception as exc:
        return "fail", f"could not connect ({exc.__class__.__name__})"
    bad = _check_response(r)
    if bad:
        return bad
    try:
        products = r.json().get("products", [])
    except Exception:
        return "fail", "the page is not a Shopify product feed (the store may use a different platform)"
    if not products:
        return "warn", "reachable, but it returned no products"
    return "ok", f"{len(products)} products read, for example '{products[0].get('title', '')}'"


def _probe_woo(base):
    try:
        r = _get(base + "/wp-json/wc/store/v1/products", {"search": "magic the gathering", "per_page": 3})
    except Exception as exc:
        return "fail", f"could not connect ({exc.__class__.__name__})"
    bad = _check_response(r)
    if bad:
        return bad
    try:
        products = r.json()
    except Exception:
        return "fail", "the page is not a WooCommerce product feed"
    if not isinstance(products, list) or not products:
        return "warn", "reachable, but nothing matched a search for magic the gathering"
    return "ok", f"{len(products)} products read, for example '{products[0].get('name', '')}'"


def _probe_page(base, path):
    try:
        r = _get(base + path)
    except Exception as exc:
        return "fail", f"could not connect ({exc.__class__.__name__})"
    bad = _check_response(r)
    if bad:
        return bad
    codes = len(set(re.findall(r"Item Code\s*:\s*(\d+)", r.text or "", re.I)))
    if not codes:
        return "warn", "page read, but no 'Item Code' text was found, so the reader may not understand this layout"
    return "ok", f"page read, {codes} item codes found"


def _probe_entry(e):
    rows = []
    if e["type"] == "shopify":
        for h in e["handles"]:
            status, detail = _probe_shopify(e["base"], h)
            rows.append((e["name"], "whole store" if h == "*" else h, status, detail))
    elif e["type"] == "woo":
        rows.append((e["name"], "WooCommerce search", *_probe_woo(e["base"])))
    else:
        for p in e["handles"]:
            rows.append((e["name"], p, *_probe_page(e["base"], p)))
    return rows


def probe_config(cfg):
    """Tests every store in the file. Returns rows of (store, what, status, detail), in file order."""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_probe_entry, cfg.entries))
    return [row for rows in results for row in rows]


def _word(status):
    return {"ok": "OK", "warn": "Warning", "fail": "Failed"}[status]


def _write_summary(markdown):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(markdown + "\n")
        except OSError:
            pass  # the summary is a convenience, it must never stop a check


def _print_probe(rows, title):
    stores = {}
    for name, _what, status, _d in rows:
        stores.setdefault(name, []).append(status)
    readable = sum(1 for s in stores.values() if "ok" in s)
    print(f"\n{title}: {readable} of {len(stores)} stores can be read from this server")
    width = max([len(r[0]) for r in rows] + [5])
    for name, what, status, detail in rows:
        print(f"  {name:<{width}}  {_word(status):<8}  {what}: {detail}")
        if status == "fail":
            _annotate("warning", title, 0, f"{name}: {what}: {detail}")
    md = [f"### {title}", "", f"{readable} of {len(stores)} stores can be read from this server.", "",
          "| Store | Collection | Result | Detail |", "|---|---|---|---|"]
    for name, what, status, detail in rows:
        md.append(f"| {name} | {what} | {_word(status)} | {detail.replace('|', '/')} |")
    _write_summary("\n".join(md))


def _score(text, kind):
    t = text.lower()
    s = 0
    if "magic" in t or "mtg" in t:
        s += 3
    if kind == "singles":
        if "single" in t:
            s += 2
        elif re.search(r"booster|bundle|sealed|display|box|deck|accessor|sleeve|playmat|binder|dice", t):
            s -= 4
    else:
        if re.search(r"booster|bundle|commander|sealed|display|box|deck|clearance|sale", t):
            s += 2
    if re.search(r"in[\s_-]?stock", t):
        s += 1
    if re.search(r"pok[eé]mon|yu-?gi-?oh|lorcana|one[\s_-]?piece|digimon|flesh|riftbound|star[\s_-]?wars", t) \
            and not ("magic" in t or "mtg" in t):
        s -= 3
    return s


def discover(site, kind="singles"):
    """Lists a Shopify store's collections that look like Magic, with lines ready to paste. Returns text."""
    try:
        base, _p, _q = _split_address(site)
    except ValueError as exc:
        return f"Cannot use that address: {exc}"
    found = []
    try:
        for page in range(1, 5):
            r = _get(base + "/collections.json", {"limit": 250, "page": page})
            bad = _check_response(r)
            if bad:
                return (f"Could not list collections for {base}: {bad[1]}.\n"
                        "Open the store in a browser, go to its Magic section, and paste that page's address into the "
                        "second column of a new line in the stores file.")
            try:
                items = r.json().get("collections", [])
            except Exception:
                return (f"{base} did not return a collection list, so it may not be a Shopify store.\n"
                        "Paste a collection address from the store into the second column instead.")
            found += items
            if len(items) < 250:
                break
    except Exception as exc:
        return f"Could not connect to {base} ({exc.__class__.__name__})."
    scored = []
    for c in found:
        handle, title = c.get("handle", ""), c.get("title", "")
        score = _score(f"{handle} {title}", kind)
        if score > 0:
            scored.append((score, int(c.get("products_count") or 0), handle, title))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    host = _host_key(base).split(".")[0]
    guess = host.replace("-", " ").title() or "New store"
    out = [f"{base} has {len(found)} collections. Likely matches for the {kind} bot:", ""]
    if not scored:
        out.append("  None of the collection names mention Magic.")
        if kind == "singles":
            out.append(f"  You can still try the whole store: {guess} | {base} | *")
    for score, count, handle, title in scored[:10]:
        size = f", {count} products" if count else ""
        out.append(f"  {handle}  ('{title}'{size})")
    if scored:
        out += ["", "Lines ready to paste into the stores file (change the name if you like):", ""]
        for score, count, handle, title in scored[:3]:
            out.append(f"{guess} | {base} | {handle}")
        if kind == "singles":
            out.append(f"{guess} | {base} | *      (the whole store, if none of the above are right)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 5. Command line
# ---------------------------------------------------------------------------


def _infer_kind(path):
    name = os.path.basename(path).lower()
    for k in _KINDS:
        if k in name:
            return k
    return None


def _default_files():
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for fname in ("stores-singles.txt", "stores-sealed.txt"):
        for d in (os.getcwd(), here):
            p = os.path.join(d, fname)
            if os.path.isfile(p) and p not in out:
                out.append(p)
                break
    return out


def cmd_check(files, kind, probe):
    files = files or _default_files()
    if not files:
        print("No stores file found (stores-singles.txt or stores-sealed.txt).")
        return 0
    code = 0
    for path in files:
        k = kind or _infer_kind(path)
        if k is None:
            print(f"{path}: cannot tell which bot this file is for. Add  --kind singles  or  --kind sealed")
            code = 1
            continue
        cfg = parse_file(path, k)
        label = os.path.basename(path)
        if not cfg.found:
            print(f"{label}: file not found")
            code = 1
            continue
        off = f", {cfg.switched_off} switched off" if cfg.switched_off else ""
        print(f"{label} ({k} bot): {len(cfg.entries)} stores{off}, {len(cfg.problems)} problem(s)")
        for text, line in zip(cfg.problems, cfg.problem_lines):
            print(f"  {text}")
            _annotate("error", label, line, text)
        if cfg.problems:
            code = 1
            _write_summary(f"### {label}: problems\n\n" + "\n".join(f"- {p}" for p in cfg.problems))
        if probe and cfg.entries:
            _print_probe(probe_config(cfg), label)
    return code


def cmd_discover(site, kind):
    text = discover(site, kind)
    print(text)
    _write_summary("### Collections found\n\n```\n" + text + "\n```")
    return 0


def cmd_export(script, kind):
    lists = export_from_script(script, kind)
    text = format_stores_file(lists, kind)
    print(text)
    _write_summary(f"### Stores written in {os.path.basename(script)}\n\n"
                   "Copy everything in the box into the stores file.\n\n```\n" + text + "```")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check and manage the stores file.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="check the stores file(s) and test each store")
    p.add_argument("files", nargs="*")
    p.add_argument("--kind", choices=_KINDS)
    p.add_argument("--no-probe", action="store_true", help="only check the file, make no web requests")
    p = sub.add_parser("discover", help="list a Shopify store's Magic collections")
    p.add_argument("site")
    p.add_argument("--kind", choices=_KINDS, default="singles")
    p = sub.add_parser("export", help="print the stores written in a bot script as a stores file")
    p.add_argument("script")
    p.add_argument("--kind", choices=_KINDS, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args.files, args.kind, not args.no_probe)
    if args.cmd == "discover":
        return cmd_discover(args.site, args.kind)
    return cmd_export(args.script, args.kind)


if __name__ == "__main__":
    sys.exit(main())
