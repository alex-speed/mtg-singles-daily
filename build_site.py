#!/usr/bin/env python3
"""
build_site.py: turns report.html into the styled web pages that GitHub Pages publishes.

Safe to run in production? Yes. It reads report.html and writes files inside the docs folder. It makes
no web requests and changes nothing outside this repository.

What it writes (all inside the output folder, docs by default):
    index.html              the latest report
    archive/YYYY-MM-DD.html one page for each day, newest 30 kept
    archive/index.html      the list of earlier reports
    style.css               the stylesheet shared by every page
    rescan.js               the re-scan button's script (only when re-scan is set up)
    .nojekyll               tells GitHub Pages to publish the files as they are

The look is a deep space theme inspired by the Edge of Eternities set: a violet and navy void, gold
accents, and a black hole in the header. It uses no images, fonts or scripts from anywhere else.

Every dated page keeps the original report between two marker comments, so each run rebuilds all the
dated pages from them. Changing the look here restyles the whole archive on the next run.

Usage:
    python build_site.py                              read report.html, write docs
    python build_site.py --report r.html --out site   other locations
Settings come from environment variables:
    SITE_TITLE    the page heading (default: MTG sealed deals)
    SITE_KICKER   the small line above the heading (default: Daily deal scan)
    SITE_NAV      the links in the bar at the top that switches between sites, written as
                  Label|address;Label|address, for example
                  Sealed|https://you.github.io/sealed-repo/;Singles|https://you.github.io/singles-repo/
                  Leave it empty and no bar is shown. Only http and https addresses are accepted.
    SITE_CURRENT  the label of the link that is this site, so it is highlighted (for example Sealed)

    Re-scan button (all optional; leave RESCAN_REPO empty and there is no button):
    RESCAN_REPO       owner/name of the repository whose workflow the button starts
    RESCAN_WORKFLOW   the workflow file name, for example mtg-deals.yml
    RESCAN_REF        the branch to run it on (default main)
    RESCAN_INPUTS     inputs to pass, written as name=value,name=value, for example dry_run=true,only_new=true
    RESCAN_EXPECT     a rough run time in seconds, used for the progress bar until real timings are found
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Australia/Adelaide")
    TZ_LABEL = "Adelaide time"
except Exception:  # no time zone database: fall back to UTC and say so
    _TZ = timezone.utc
    TZ_LABEL = "UTC"

KEEP_DAYS = 30
START = "<!-- report:start -->"
END = "<!-- report:end -->"
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")

# What a page may load. Scripts only from this site, and network requests only to this site and the
# GitHub API (for the re-scan button). Inline styles are allowed because the report marks rows with them.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' https://api.github.com; base-uri 'none'; form-action 'none'")

# ---------------------------------------------------------------------------------------------------
# The stylesheet
# ---------------------------------------------------------------------------------------------------

CSS = r"""
/* Deep space theme inspired by Edge of Eternities: violet and navy void, gold accents, a black hole. */
:root{
  color-scheme:dark;
  --bg0:#04061a; --bg1:#080e2e; --bg2:#150f3d;
  --panel:rgba(14,20,58,.88);
  --edge:rgba(160,150,255,.24); --line:rgba(160,150,255,.13);
  --text:#e9edfc; --muted:#a9b2d5; --faint:#8791bb;
  --gold:#f4c24f; --ember:#ff8a4c; --cyan:#62e0ff; --violet:#a58bff; --link:#b9c6ff;
  --r:14px; --w:1240px; --pad:clamp(14px,3vw,34px);
  --sans:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"Cascadia Mono","SF Mono",Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html{
  background:
    radial-gradient(880px 520px at 90% -6%,rgba(165,139,255,.26),transparent 62%),
    radial-gradient(720px 460px at 2% 4%,rgba(98,224,255,.10),transparent 60%),
    radial-gradient(1100px 480px at 50% 114%,rgba(165,139,255,.14),transparent 66%),
    linear-gradient(180deg,var(--bg0) 0%,var(--bg1) 46%,#070a24 100%);
  background-attachment:fixed;
  background-color:var(--bg0);
  scrollbar-color:#3b3f86 transparent;
}
body{margin:0;min-height:100vh;color:var(--text);font:15px/1.6 var(--sans);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:rgba(244,194,79,.35);color:#fff}

/* stars, then the curve of a planet along the bottom edge */
body::before{
  content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background-image:
    radial-gradient(1.4px 1.4px at 6% 12%,#fff,transparent 62%),
    radial-gradient(1px 1px at 14% 38%,rgba(255,255,255,.85),transparent 62%),
    radial-gradient(1.2px 1.2px at 22% 71%,rgba(214,222,255,.9),transparent 62%),
    radial-gradient(1px 1px at 29% 22%,rgba(255,255,255,.7),transparent 62%),
    radial-gradient(1.6px 1.6px at 37% 54%,#fff,transparent 62%),
    radial-gradient(1px 1px at 43% 88%,rgba(255,255,255,.75),transparent 62%),
    radial-gradient(1.2px 1.2px at 51% 9%,rgba(255,236,190,.95),transparent 62%),
    radial-gradient(1px 1px at 58% 47%,rgba(255,255,255,.7),transparent 62%),
    radial-gradient(1.4px 1.4px at 64% 78%,rgba(214,222,255,.9),transparent 62%),
    radial-gradient(1px 1px at 71% 31%,rgba(255,255,255,.8),transparent 62%),
    radial-gradient(1.2px 1.2px at 78% 64%,#fff,transparent 62%),
    radial-gradient(1px 1px at 84% 16%,rgba(255,255,255,.75),transparent 62%),
    radial-gradient(1.5px 1.5px at 91% 52%,rgba(214,222,255,.95),transparent 62%),
    radial-gradient(1px 1px at 96% 84%,rgba(255,255,255,.7),transparent 62%),
    radial-gradient(1px 1px at 3% 92%,rgba(255,255,255,.6),transparent 62%),
    radial-gradient(1px 1px at 47% 63%,rgba(255,255,255,.55),transparent 62%);
}
body::after{
  content:"";position:fixed;left:50%;bottom:-70vh;width:170vw;height:86vh;transform:translateX(-50%);
  border-radius:50%;pointer-events:none;z-index:-1;
  background:radial-gradient(ellipse at 50% 0,rgba(165,139,255,.20),rgba(98,224,255,.05) 32%,transparent 60%);
  border-top:1px solid rgba(244,194,79,.13);
}

.wrap{max-width:var(--w);margin:0 auto;padding:0 var(--pad)}
.skip{position:absolute;left:-9999px;top:0;background:var(--gold);color:#1a1203;padding:.5rem .9rem;border-radius:0 0 8px 0;z-index:10}
.skip:focus{left:0}
a{color:var(--link);text-decoration:none;border-bottom:1px solid rgba(185,198,255,.28);transition:color .15s,border-color .15s}
a:hover{color:#fff;border-bottom-color:var(--gold)}
a:focus-visible{outline:2px solid var(--gold);outline-offset:3px;border-radius:3px}

/* the bar that switches between the sealed and singles sites */
.sites{position:sticky;top:0;z-index:5;border-bottom:1px solid var(--line);background:rgba(4,6,26,.82);
  -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
.sites-in{display:flex;align-items:stretch;gap:.2rem;max-width:var(--w);margin:0 auto;padding:0 var(--pad)}
.sites .brand{display:flex;align-items:center;gap:.6rem;margin-right:auto;font:600 11.5px/1 var(--mono);letter-spacing:.24em;text-transform:uppercase;color:var(--muted)}
.sites .brand::before{content:"";width:12px;height:12px;border-radius:50%;
  background:radial-gradient(circle,#000 34%,var(--gold) 40%,rgba(244,194,79,.35) 58%,transparent 72%);box-shadow:0 0 10px rgba(244,194,79,.35)}
.sites a{display:flex;align-items:center;padding:.85rem 1.2rem;border:0;border-bottom:2px solid transparent;color:var(--muted);
  font-weight:600;font-size:.9rem;letter-spacing:.04em;transition:color .15s,border-color .15s,background .15s}
.sites a:hover{color:#fff;border-bottom-color:rgba(244,194,79,.55)}
.sites a[aria-current]{color:#fff;border-bottom-color:var(--gold);background:linear-gradient(180deg,transparent,rgba(244,194,79,.12))}
.sites a:focus-visible{outline-offset:-4px}
main.wrap{scroll-margin-top:64px}

/* masthead */
.mast{position:relative;overflow:hidden;border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(165,139,255,.10),transparent 90%)}
.mast-in{position:relative;z-index:1;max-width:var(--w);margin:0 auto;padding:clamp(30px,6vw,66px) var(--pad) clamp(24px,4vw,42px)}
.kicker{display:flex;align-items:center;gap:.65rem;margin:0 0 .7rem;font:600 11.5px/1 var(--mono);letter-spacing:.3em;text-transform:uppercase;color:var(--gold)}
.kicker::before{content:"";width:7px;height:7px;transform:rotate(45deg);background:var(--gold);box-shadow:0 0 12px 1px var(--gold)}
h1{margin:0;font-size:clamp(2rem,5.2vw,3.2rem);font-weight:700;letter-spacing:-.015em;line-height:1.05;
  background:linear-gradient(100deg,#fff 8%,#cfc8ff 52%,var(--gold) 118%);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{margin:.75rem 0 0;color:var(--muted);font-size:.95rem}
.sub time{color:var(--text)}
nav.pills{display:flex;flex-wrap:wrap;gap:.55rem;margin-top:1.3rem}
nav.pills a{display:inline-flex;align-items:center;min-height:40px;padding:.42rem .95rem;border:1px solid var(--edge);border-radius:999px;color:var(--text);
  background:rgba(20,28,74,.55);font-size:.84rem;letter-spacing:.03em}
nav.pills a:hover,nav.pills a[aria-current]{border-color:var(--gold);color:#fff;background:rgba(244,194,79,.12);box-shadow:0 0 18px rgba(244,194,79,.18)}

/* the re-scan button and its progress panel */
.pill-btn{display:inline-flex;align-items:center;gap:.55rem;min-height:40px;padding:.42rem 1rem;border:1px solid rgba(244,194,79,.6);border-radius:999px;
  background:rgba(244,194,79,.10);color:#fff;font:inherit;font-size:.84rem;letter-spacing:.03em;cursor:pointer;
  transition:background .15s,box-shadow .15s,opacity .15s}
.pill-btn::before{content:"";width:9px;height:9px;border-radius:50%;border:2px solid var(--gold);border-right-color:transparent}
.pill-btn:hover:not(:disabled){background:rgba(244,194,79,.2);box-shadow:0 0 18px rgba(244,194,79,.25)}
.pill-btn:disabled{opacity:.7;cursor:progress}
.pill-btn:focus-visible,.rs-btn:focus-visible,.rs-link:focus-visible{outline:2px solid var(--gold);outline-offset:3px}
.rescan{margin-top:1.1rem;max-width:660px;padding:1rem 1.2rem 1.1rem;border:1px solid var(--edge);border-left:3px solid var(--gold);
  border-radius:var(--r);background:rgba(8,14,46,.88);box-shadow:0 14px 44px rgba(0,0,0,.4)}
.rescan[hidden],.rescan [hidden]{display:none !important}
.rs-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:.3rem 1rem}
.rs-title{font-weight:650;color:#fff;font-size:1rem}
.rs-time{font:12px var(--mono);color:var(--muted)}
.rs-bar{height:10px;margin:.75rem 0 .6rem;border:1px solid var(--line);border-radius:999px;background:rgba(165,139,255,.16);overflow:hidden}
.rs-fill{height:100%;width:0;border-radius:999px;transition:width .6s ease;
  background:linear-gradient(90deg,var(--gold),var(--ember) 38%,var(--violet) 72%,var(--cyan))}
.rs-msg{margin:.2rem 0 0;color:var(--muted);font-size:.9rem}
.rs-links{display:flex;flex-wrap:wrap;gap:.6rem 1.2rem;margin-top:.7rem;font-size:.85rem}
.rescan[data-state="token"] .rs-bar,.rescan[data-state="token"] .rs-time{display:none}
.rescan[data-state="failed"]{border-left-color:#ff7a7a}
.rescan[data-state="failed"] .rs-fill{background:#ff7a7a}
.rescan[data-state="done"] .rs-fill{background:linear-gradient(90deg,var(--cyan),#8af5c2)}
.rs-form p{margin:.5rem 0;color:var(--muted);font-size:.88rem}
.rs-form ol{margin:.4rem 0 .6rem;padding-left:1.25rem;color:var(--muted);font-size:.88rem}
.rs-form label{display:block;margin:.7rem 0 .25rem;font-size:.85rem;color:var(--text)}
.rs-form input{width:100%;padding:.6rem .75rem;border:1px solid var(--edge);border-radius:10px;background:#0a0f33;color:var(--text);font:14px var(--mono)}
.rs-form input:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.rs-err{min-height:1.2em;margin:.4rem 0 0;color:#ffb4b4;font-size:.88rem}
.rs-row{display:flex;flex-wrap:wrap;gap:.6rem;margin-top:.6rem}
.rs-btn{padding:.55rem 1.1rem;border:1px solid rgba(244,194,79,.6);border-radius:999px;background:rgba(244,194,79,.14);color:#fff;font:inherit;font-size:.86rem;cursor:pointer;min-height:44px}
.rs-btn.alt{border-color:var(--edge);background:transparent;color:var(--muted)}
.rs-btn:hover{background:rgba(244,194,79,.24)}
.rs-btn.alt:hover{background:rgba(165,139,255,.12);color:#fff}
.rs-link{padding:.3rem 0;border:0;border-bottom:1px solid rgba(185,198,255,.28);background:none;color:var(--link);font:inherit;cursor:pointer}
.rs-link:hover{color:#fff;border-bottom-color:var(--gold)}
@media (prefers-reduced-motion:no-preference){
  .pill-btn:disabled::before{animation:rsspin 1s linear infinite}
  .rescan[data-state="running"] .rs-fill,.rescan[data-state="starting"] .rs-fill{background-size:220% 100%;animation:rsflow 2.4s linear infinite}
  @keyframes rsspin{to{transform:rotate(360deg)}}
  @keyframes rsflow{from{background-position:0 0}to{background-position:-220% 0}}
}

/* the black hole */
.void{position:absolute;right:clamp(-110px,3vw,60px);top:50%;width:clamp(190px,27vw,340px);aspect-ratio:1;transform:translateY(-50%);pointer-events:none}
.void::before{
  content:"";position:absolute;inset:0;border-radius:50%;
  background:conic-gradient(from 200deg,rgba(244,194,79,0),rgba(244,194,79,.95) 13%,rgba(255,138,76,.9) 25%,rgba(165,139,255,.8) 47%,rgba(98,224,255,.5) 65%,rgba(244,194,79,0) 77%,rgba(244,194,79,.6) 91%,rgba(244,194,79,0));
  -webkit-mask:radial-gradient(closest-side,transparent 55%,#000 57%,#000 73%,transparent 100%);
          mask:radial-gradient(closest-side,transparent 55%,#000 57%,#000 73%,transparent 100%);
  transform:rotate(-17deg) scaleY(.34);
  filter:blur(1.1px) drop-shadow(0 0 20px rgba(165,139,255,.6));
}
.void::after{
  content:"";position:absolute;z-index:1;left:50%;top:50%;width:33%;aspect-ratio:1;transform:translate(-50%,-50%);border-radius:50%;background:#000;
  box-shadow:0 0 0 1px rgba(244,194,79,.6),0 0 26px 5px rgba(244,194,79,.30),0 0 86px 22px rgba(165,139,255,.30);
}
.void i{
  position:absolute;z-index:2;inset:0;border-radius:50%;
  background:conic-gradient(from 200deg,rgba(244,194,79,0),rgba(244,194,79,.95) 13%,rgba(255,138,76,.9) 25%,rgba(165,139,255,.8) 47%,rgba(98,224,255,.5) 65%,rgba(244,194,79,0) 77%,rgba(244,194,79,.6) 91%,rgba(244,194,79,0));
  -webkit-mask-image:radial-gradient(closest-side,transparent 55%,#000 57%,#000 73%,transparent 100%),linear-gradient(180deg,transparent 42%,#000 60%);
  -webkit-mask-composite:source-in;
          mask-image:radial-gradient(closest-side,transparent 55%,#000 57%,#000 73%,transparent 100%),linear-gradient(180deg,transparent 42%,#000 60%);
          mask-composite:intersect;
  transform:rotate(-17deg) scaleY(.34);
  filter:blur(1.1px) drop-shadow(0 0 14px rgba(244,194,79,.4));
}

/* sections */
main.wrap{padding-top:clamp(24px,4vw,40px);padding-bottom:34px;counter-reset:sec}
main > p{max-width:78ch;color:var(--muted);margin:.35rem 0 1rem;font-size:.92rem}
h3{display:flex;align-items:center;gap:.8rem;margin:2.1rem 0 .85rem;font-size:1.06rem;font-weight:650;letter-spacing:.01em;color:#fff;counter-increment:sec}
h3::before{content:counter(sec,decimal-leading-zero);font:600 12px/1 var(--mono);letter-spacing:.14em;color:var(--gold);
  padding:.34rem .5rem;border:1px solid rgba(244,194,79,.4);border-radius:6px;background:rgba(244,194,79,.08)}
h3::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--edge),transparent)}

/* tables */
.tablewrap{overflow-x:auto;margin:.4rem 0 1.3rem;border:1px solid var(--edge);border-radius:var(--r);
  background:var(--panel);box-shadow:0 14px 44px rgba(0,0,0,.38),inset 0 1px 0 rgba(255,255,255,.05);-webkit-overflow-scrolling:touch}
table{width:100%;border:0;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:.72rem .95rem;border:0;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:11px;font-weight:650;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);white-space:nowrap;
  background:linear-gradient(180deg,rgba(165,139,255,.14),rgba(165,139,255,.05))}
tr:last-child > td{border-bottom:0}
td:first-child{min-width:230px}
tr:hover > td{background-color:rgba(165,139,255,.07)}

/* lists */
main ul{list-style:none;margin:.4rem 0 1.4rem;padding:0;display:grid;gap:.6rem}
main li{position:relative;padding:.78rem 1rem .78rem 2.2rem;border:1px solid var(--edge);border-radius:12px;background:var(--panel);
  box-shadow:0 8px 26px rgba(0,0,0,.26)}
main li::before{content:"";position:absolute;left:.95rem;top:1.2rem;width:6px;height:6px;transform:rotate(45deg);background:var(--violet);box-shadow:0 0 8px var(--violet)}

/* the note at the top of a report (written by the deal history) */
div[style*="border:1px solid #999"]{
  margin:0 0 1.7rem !important;padding:1.05rem 1.3rem !important;border:1px solid var(--edge) !important;border-left:3px solid var(--gold) !important;
  border-radius:var(--r);background:linear-gradient(120deg,rgba(244,194,79,.10),rgba(165,139,255,.08) 58%,transparent),var(--panel);
  box-shadow:0 14px 44px rgba(0,0,0,.34)}
div[style*="border:1px solid #999"] p{margin:.4rem 0;color:var(--text)}
div[style*="border:1px solid #999"] b{color:#fff}
div[style*="border:1px solid #999"] ul{margin:.2rem 0 .5rem;gap:.15rem}
div[style*="border:1px solid #999"] li{padding:.12rem 0 .12rem 1.15rem;border:0;background:none;box-shadow:none}
div[style*="border:1px solid #999"] li::before{left:.1rem;top:.62rem}

/* new deals, recent deals and price drops (the colours are set by the deal history) */
tr[style*="#fff3b0"],tr[style*="#ffe0b2"],tr[style*="#e8e4ff"]{background:transparent !important}
td[style*="#fff3b0"]{background:rgba(244,194,79,.13) !important}
tr[style*="#fff3b0"] > td:first-child{box-shadow:inset 3px 0 0 var(--gold)}
td[style*="#ffe0b2"]{background:rgba(98,224,255,.12) !important}
tr[style*="#ffe0b2"] > td:first-child{box-shadow:inset 3px 0 0 var(--cyan)}
td[style*="#e8e4ff"]{background:rgba(165,139,255,.11) !important}
tr[style*="#e8e4ff"] > td:first-child{box-shadow:inset 3px 0 0 var(--violet)}
li[style*="#fff3b0"]{background:linear-gradient(90deg,rgba(244,194,79,.20),rgba(244,194,79,.06) 70%),var(--panel) !important;border-color:rgba(244,194,79,.55) !important}
li[style*="#ffe0b2"]{background:linear-gradient(90deg,rgba(98,224,255,.18),rgba(98,224,255,.05) 70%),var(--panel) !important;border-color:rgba(98,224,255,.5) !important}
li[style*="#e8e4ff"]{background:linear-gradient(90deg,rgba(165,139,255,.18),rgba(165,139,255,.05) 70%),var(--panel) !important;border-color:rgba(165,139,255,.5) !important}
span[style*="background:#c00"],span[style*="background:#e67e00"],span[style*="background:#6a5acd"]{
  display:inline-block;margin-right:.45rem;padding:2px 10px !important;border-radius:999px !important;font:700 10.5px/1.5 var(--sans) !important;
  letter-spacing:.11em;text-transform:uppercase;vertical-align:1px;color:#1b1203 !important}
span[style*="background:#c00"]{background:linear-gradient(135deg,#ffe28f,#f2a93b) !important;box-shadow:0 0 14px rgba(244,194,79,.5)}
span[style*="background:#e67e00"]{background:linear-gradient(135deg,#9af3ff,#4fa6ff) !important;color:#03121c !important;box-shadow:0 0 14px rgba(98,224,255,.45)}
span[style*="background:#6a5acd"]{background:linear-gradient(135deg,#d6ccff,#9a87ff) !important;color:#120a33 !important;box-shadow:0 0 12px rgba(165,139,255,.4)}
@media (prefers-reduced-motion:no-preference){
  span[style*="background:#c00"]{animation:glow 3.2s ease-in-out infinite}
  @keyframes glow{0%,100%{box-shadow:0 0 8px rgba(244,194,79,.35)}50%{box-shadow:0 0 18px rgba(244,194,79,.75)}}
}

/* the archive list */
ul.archive{grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:.7rem;margin-top:1rem}
ul.archive li{padding:0;border-radius:12px}
ul.archive li::before{display:none}
ul.archive a{display:flex;justify-content:space-between;align-items:center;gap:.6rem;padding:.85rem 1.05rem;border:0;color:var(--text)}
ul.archive a:hover{background:rgba(165,139,255,.12);border-radius:12px}
ul.archive .iso{font:12px var(--mono);color:var(--faint)}
ul.archive .tag{font:700 10px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:#1b1203;background:var(--gold);padding:.28rem .5rem;border-radius:5px}

hr{border:0;height:1px;background:var(--line);margin:1.5rem 0}
footer.foot{padding-top:1.4rem;padding-bottom:2.4rem;color:var(--faint);font-size:.82rem;border-top:1px solid var(--line)}
footer.foot p{margin:.2rem 0}

@media (max-width:420px){
  .sites .brand{display:none}
  .sites-in{padding:0}
  .sites a{flex:1;justify-content:center;padding-left:.6rem;padding-right:.6rem}
}
@media (max-width:720px){
  body{font-size:14.5px}
  .void{opacity:.5;right:-134px;top:34%;width:215px}
  th,td{padding:.6rem .7rem}
  th{white-space:normal}
  td{min-width:84px}
  td:first-child{min-width:190px}
  h3{font-size:1rem}
}
@media print{
  html,body{background:#fff !important;color:#000 !important}
  body::before,body::after,.void,nav.pills,.sites,.skip,.rescan,.pill-btn,.rs-link{display:none !important}
  h1{color:#000 !important;background:none !important;-webkit-text-fill-color:#000}
  .tablewrap,main li,div[style*="border:1px solid #999"]{background:#fff !important;box-shadow:none !important;border-color:#999 !important}
  a{color:#000;border-bottom:0}
}
"""

# ---------------------------------------------------------------------------------------------------
# The script behind the re-scan button
# ---------------------------------------------------------------------------------------------------

RESCAN_JS = r"""
/* Re-scan button. Starts the repository's workflow through the GitHub API and follows its progress.
   The token is typed in by the visitor and kept only in this browser. It is sent only to api.github.com. */
(function () {
  "use strict";

  var btn = document.getElementById("rescan-btn");
  var panel = document.getElementById("rescan");
  if (!btn || !panel) { return; }

  var API = "https://api.github.com";
  var TOKEN_KEY = "mtgRescanToken";
  var cfg = {
    repo: btn.getAttribute("data-repo") || "",
    workflow: btn.getAttribute("data-workflow") || "",
    ref: btn.getAttribute("data-ref") || "main",
    inputs: readJson(btn.getAttribute("data-inputs"), {}),
    expect: Number(btn.getAttribute("data-expect")) || 300
  };
  var RUN_KEY = "mtgRescanRun:" + cfg.repo;
  var PAGES_WAIT = 45;            /* seconds GitHub Pages usually needs after the scan finishes */
  var PAGES_LIMIT_MS = 240000;    /* stop waiting for the page after four minutes */
  var STALE_MS = 45 * 60 * 1000;  /* forget a remembered run after 45 minutes */

  function byId(id) { return document.getElementById(id); }
  var el = {
    title: byId("rs-title"), time: byId("rs-time"), bar: byId("rs-bar"), fill: byId("rs-fill"), msg: byId("rs-msg"),
    run: byId("rs-run"), reload: byId("rs-reload"), form: byId("rs-form"), token: byId("rs-token"), err: byId("rs-err"),
    cancel: byId("rs-cancel"), forget: byId("rs-forget"), label: byId("rescan-label")
  };

  var store = {
    get: function (k) { try { return window.localStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); return true; } catch (e) { return false; } },
    del: function (k) { try { window.localStorage.removeItem(k); } catch (e) { /* storage is not available */ } }
  };
  var token = store.get(TOKEN_KEY);
  var busy = false;
  var shown = 0;

  function readJson(text, fallback) {
    try { var v = JSON.parse(text); return v && typeof v === "object" ? v : fallback; } catch (e) { return fallback; }
  }
  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function pollMs() { return Math.max(200, Number(btn.getAttribute("data-poll")) || 5000); }
  function builtOf(doc) {
    var m = doc.querySelector('meta[name="built-at"]');
    return m ? (m.getAttribute("content") || "") : "";
  }
  function fmt(seconds) {
    var s = Math.max(0, Math.round(seconds));
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
  }

  /* ------------------------------------------------------------------ GitHub API */
  async function api(path, options) {
    var opts = options || {};
    var headers = { "Accept": "application/vnd.github+json", "Authorization": "Bearer " + token };
    if (opts.body) { headers["Content-Type"] = "application/json"; }
    var res = await fetch(API + path, { method: opts.method || "GET", headers: headers, body: opts.body, cache: "no-store" });
    if (!res.ok) { var err = new Error("GitHub answered " + res.status); err.status = res.status; throw err; }
    return res.status === 204 ? null : res.json();
  }
  function wfPath(tail) { return "/repos/" + cfg.repo + "/actions/workflows/" + encodeURIComponent(cfg.workflow) + tail; }
  function runPath(id, tail) { return "/repos/" + cfg.repo + "/actions/runs/" + id + (tail || ""); }
  async function listRuns(query) { var data = await api(wfPath("/runs" + query)); return data.workflow_runs || []; }

  function explain(err, stage) {
    var s = err && err.status;
    if (err && err.kind === "timeout") { return "GitHub accepted the request, but the run has not appeared yet. Check the Actions tab on GitHub."; }
    if (s === 401) { return "GitHub did not accept the token. It may have expired or been removed."; }
    if (s === 403 && stage === "start") { return "GitHub refused to start the scan. The token needs the Actions permission set to Read and write for this repository, or GitHub is limiting requests for now."; }
    if (s === 403) { return "GitHub refused the request. It may be limiting requests, so try again in a few minutes."; }
    if (s === 404) { return stage === "start" ? "GitHub could not find the workflow. The token may not have access to this repository, or the workflow file name has changed." : "GitHub could not find the repository. The token may not have access to it."; }
    if (s === 422) { return "GitHub rejected the scan request. The workflow may not accept these settings."; }
    if (!s) { return "The page could not reach GitHub. Check your connection and try again."; }
    return "GitHub answered with an error (" + s + "). Try again in a moment.";
  }

  /* ------------------------------------------------------------------ what the panel shows */
  function setState(state) { panel.setAttribute("data-state", state); panel.hidden = false; }
  function setProgress(pct, allowBack) {
    var p = Math.max(0, Math.min(100, Math.round(pct)));
    if (!allowBack) { p = Math.max(p, shown); }
    shown = p;
    el.fill.style.width = p + "%";
    el.bar.setAttribute("aria-valuenow", String(p));
    return p;
  }
  function show(state, title, message, pct) {
    setState(state);
    el.title.textContent = title;
    el.msg.textContent = message;
    el.form.hidden = true;
    if (typeof pct === "number") { setProgress(pct); }
  }
  function setBusy(on) {
    busy = on;
    btn.disabled = on;
    el.label.textContent = on ? "Scanning" : "Re-scan now";
  }
  function updateForget() { el.forget.hidden = !token; }
  function showLink(run) {
    if (run && run.html_url) { el.run.href = run.html_url; el.run.hidden = false; } else { el.run.hidden = true; }
  }
  function friendly(name) {
    if (/set up job/i.test(name)) { return "Starting a machine"; }
    if (/checkout|get the repository/i.test(name)) { return "Fetching the code"; }
    if (/python/i.test(name)) { return "Setting up Python"; }
    if (/install/i.test(name)) { return "Installing tools"; }
    if (/run bot/i.test(name)) { return "Reading the stores and building the report"; }
    if (/attach/i.test(name)) { return "Saving the report"; }
    if (/publish/i.test(name)) { return "Publishing the page"; }
    if (/complete job|post /i.test(name)) { return "Cleaning up"; }
    return name;
  }

  /* ------------------------------------------------------------------ progress maths */
  function plan(job, ref) {
    var steps = (job && job.steps) || [];
    var weights = steps.map(function (s) {
      if (s.conclusion === "skipped") { return 1; }
      var w = ref.steps[s.name];
      if (typeof w === "number") { return Math.max(w, 1); }
      return /run bot/i.test(s.name) ? cfg.expect * 0.8 : 3;
    });
    var total = 0, done = 0, current = null, weight = 0, elapsed = 0;
    steps.forEach(function (s, i) {
      total += weights[i];
      if (s.status === "completed") { done += weights[i]; }
      else if (s.status === "in_progress" && !current) {
        current = s; weight = weights[i];
        var started = Date.parse(s.started_at);
        elapsed = isNaN(started) ? 0 : Math.max(0, (Date.now() - started) / 1000);
      }
    });
    return { total: total, done: done + (current ? Math.min(elapsed, weight * 0.97) : 0),
             current: current, over: !!current && elapsed > weight * 1.25 };
  }
  function paint(job, ref, startedAt) {
    var elapsed = (Date.now() - startedAt) / 1000;
    var waiting = !job || !job.steps || !job.steps.length;
    var pct, msg, left = "";
    if (waiting) {
      pct = 3 + Math.min(elapsed, 30) / 30 * 2;
      msg = "Waiting for GitHub to start a machine.";
    } else {
      var p = plan(job, ref);
      pct = 6 + 88 * Math.min(p.total > 0 ? p.done / p.total : 0, 1);
      msg = (p.current ? friendly(p.current.name) : "Finishing up") + (p.over ? ". This is taking longer than usual." : ".");
      var remaining = Math.max(0, p.total - p.done) + PAGES_WAIT;
      left = ", " + (remaining > 90 ? "about " + Math.round(remaining / 60) + " min left" : "about a minute left");
    }
    var shownPct = setProgress(pct);
    el.msg.textContent = msg;
    el.time.textContent = shownPct + "%, " + fmt(elapsed) + " elapsed" + left;
  }

  /* ------------------------------------------------------------------ finding the run and its timings */
  async function loadRef(runs) {
    var ref = { steps: {}, known: false };
    try {
      var good = runs.filter(function (r) { return r.status === "completed" && r.conclusion === "success" && r.run_started_at; }).slice(0, 5);
      if (!good.length) { return ref; }
      var dur = function (r) { return (Date.parse(r.updated_at) - Date.parse(r.run_started_at)) / 1000; };
      var sorted = good.slice().sort(function (a, b) { return dur(a) - dur(b); });
      var pick = sorted[Math.floor(sorted.length / 2)];
      var data = await api(runPath(pick.id, "/jobs"));
      var job = data.jobs && data.jobs[0];
      if (job && job.steps) {
        job.steps.forEach(function (s) {
          var a = Date.parse(s.started_at), b = Date.parse(s.completed_at);
          if (!isNaN(a) && !isNaN(b)) { ref.steps[s.name] = Math.max(0, (b - a) / 1000); }
        });
        ref.known = Object.keys(ref.steps).length > 0;
      }
    } catch (e) { /* the estimate falls back to defaults */ }
    return ref;
  }
  async function dispatch() {
    await api(wfPath("/dispatches"), { method: "POST", body: JSON.stringify({ ref: cfg.ref, inputs: cfg.inputs }) });
  }
  async function waitForNewRun(maxId) {
    var until = Date.now() + 60000;
    while (Date.now() < until) {
      await sleep(Math.min(2000, pollMs()));
      var runs = await listRuns("?event=workflow_dispatch&per_page=5");
      var fresh = runs.filter(function (r) { return r.id > maxId; }).sort(function (a, b) { return a.id - b.id; })[0];
      if (fresh) { return fresh; }
    }
    var err = new Error("no run appeared"); err.kind = "timeout"; throw err;
  }
  async function fetchBuilt() {
    try {
      var u = new URL(window.location.href); u.hash = ""; u.search = "?cb=" + Date.now();
      var res = await fetch(u.toString(), { cache: "no-store" });
      if (!res.ok) { return ""; }
      return builtOf(new DOMParser().parseFromString(await res.text(), "text/html"));
    } catch (e) { return ""; }
  }

  /* ------------------------------------------------------------------ following a run */
  async function follow(run, ref, builtBefore) {
    var startedAt = Date.parse(run.created_at) || Date.now();
    var fails = 0, current = run, job = null;
    showLink(run);
    show("running", "Re-scan in progress", "Waiting for GitHub to start a machine.", 3);
    for (;;) {
      try {
        current = await api(runPath(run.id));
        if (["queued", "requested", "pending", "waiting"].indexOf(current.status) === -1) {
          var data = await api(runPath(run.id, "/jobs"));
          job = (data.jobs && data.jobs[0]) || null;
        }
        fails = 0;
      } catch (e) {
        fails += 1;
        if (e.status === 401 || e.status === 403 || e.status === 404 || fails >= 4) { throw e; }
        await sleep(pollMs());
        continue;
      }
      paint(job, ref, startedAt);
      if (current.status === "completed") { break; }
      await sleep(pollMs());
    }
    if (current.conclusion !== "success") {
      var bad = new Error("the run did not succeed"); bad.kind = "run"; bad.run = current; throw bad;
    }
    /* the scan is done: wait for GitHub Pages to publish the new page */
    var t0 = Date.now();
    el.title.textContent = "Publishing the page";
    while (Date.now() - t0 < PAGES_LIMIT_MS) {
      var latest = await fetchBuilt();
      if (latest && latest !== builtBefore) {
        store.del(RUN_KEY);
        setState("done");
        el.title.textContent = "Re-scan complete";
        el.msg.textContent = "Done. Loading the new report.";
        el.time.textContent = "100%, " + fmt((Date.now() - startedAt) / 1000) + " elapsed";
        setProgress(100);
        await sleep(900);
        try { await fetch(window.location.pathname, { cache: "reload" }); } catch (e) { /* the reload below still works */ }
        window.location.reload();
        return;
      }
      var f = Math.min((Date.now() - t0) / (PAGES_WAIT * 1000), 1);
      setProgress(95 + 4 * f);
      el.msg.textContent = "The scan finished. Waiting for GitHub Pages to publish the new page.";
      el.time.textContent = shown + "%, " + fmt((Date.now() - startedAt) / 1000) + " elapsed";
      await sleep(pollMs());
    }
    store.del(RUN_KEY);
    show("failed", "The page has not updated yet", "The scan finished, but GitHub Pages has not published the new page. It can take a few minutes, so reload this page shortly.", 99);
    el.reload.hidden = false;
  }

  function fail(message, run) {
    store.del(RUN_KEY);
    show("failed", "The re-scan did not finish", message, shown);
    showLink(run);
    el.time.textContent = "";
  }
  function handle(e) {
    if (e && e.kind === "run") {
      var how = e.run && e.run.conclusion === "cancelled" ? "The scan was cancelled on GitHub." : "The scan failed on GitHub. Open the run to see what went wrong.";
      fail(how, e.run);
    } else if (e && e.status === 401) {
      token = ""; store.del(TOKEN_KEY); updateForget();
      showForm(explain(e, "start"));
    } else {
      fail(explain(e, busy && el.title.textContent === "Starting the re-scan" ? "start" : "follow"), null);
    }
  }

  /* ------------------------------------------------------------------ starting a scan */
  async function start() {
    if (busy) { panel.hidden = false; return; }
    if (!token) { showForm(""); return; }
    setBusy(true);
    shown = 0;
    el.reload.hidden = true;
    var builtBefore = builtOf(document);
    show("starting", "Starting the re-scan", "Checking whether a scan is already running.", 2);
    el.time.textContent = "";
    showLink(null);
    try {
      var runs = await listRuns("?per_page=10");
      var live = runs.filter(function (r) { return r.status !== "completed"; })[0];
      var run;
      if (live) {
        run = live;
      } else {
        var maxId = runs.reduce(function (m, r) { return Math.max(m, r.id); }, 0);
        el.msg.textContent = "Asking GitHub to start the scan.";
        await dispatch();
        run = await waitForNewRun(maxId);
      }
      store.set(RUN_KEY, JSON.stringify({ id: run.id, at: Date.now(), built: builtBefore }));
      await follow(run, await loadRef(runs), builtBefore);
    } catch (e) {
      handle(e);
    } finally {
      setBusy(false);
    }
  }

  async function resume() {
    var saved = readJson(store.get(RUN_KEY), null);
    if (!saved || !saved.id || !token) { return; }
    if (Date.now() - saved.at > STALE_MS || builtOf(document) !== saved.built) { store.del(RUN_KEY); return; }
    setBusy(true);
    shown = 0;
    try {
      var runs = await listRuns("?per_page=10");
      var run = await api(runPath(saved.id));
      await follow(run, await loadRef(runs), saved.built);
    } catch (e) {
      handle(e);
    } finally {
      setBusy(false);
    }
  }

  /* ------------------------------------------------------------------ the token form */
  function showForm(message) {
    setState("token");
    el.title.textContent = "Set up the re-scan button";
    el.msg.textContent = "";
    el.form.hidden = false;
    el.err.textContent = message || "";
    el.token.value = "";
    el.token.focus();
  }
  el.form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var value = el.token.value.trim();
    if (!value) { el.err.textContent = "Paste the token first."; return; }
    var before = token;
    token = value;
    el.err.textContent = "Checking the token.";
    try {
      await listRuns("?per_page=1");
    } catch (e) {
      token = before;
      el.err.textContent = explain(e, "check");
      return;
    }
    store.set(TOKEN_KEY, value);
    el.token.value = "";
    updateForget();
    start();
  });
  el.cancel.addEventListener("click", function () { el.form.hidden = true; panel.hidden = true; });
  el.forget.addEventListener("click", function () {
    token = ""; store.del(TOKEN_KEY); store.del(RUN_KEY); updateForget();
    show("info", "Token removed", "The saved token has been removed from this browser.", 0);
    el.time.textContent = "";
    showLink(null);
  });
  el.reload.addEventListener("click", function () { window.location.reload(); });
  btn.addEventListener("click", start);

  updateForget();
  resume();
}());
"""

# ---------------------------------------------------------------------------------------------------
# Page template (placeholders are written @@LIKE_THIS@@ so the CSS and HTML braces need no escaping)
# ---------------------------------------------------------------------------------------------------

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E"
        "%3CradialGradient id='g'%3E%3Cstop offset='.34' stop-color='%23000'/%3E%3Cstop offset='.4' stop-color='%23f4c24f'/%3E"
        "%3Cstop offset='1' stop-color='%23a58bff' stop-opacity='0'/%3E%3C/radialGradient%3E%3C/defs%3E"
        "%3Ccircle cx='16' cy='16' r='15' fill='url(%23g)'/%3E%3C/svg%3E")

PAGE = """<!doctype html>
<html lang="en-AU">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="@@CSP@@">
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#04061a">
<meta name="built-at" content="@@BUILT@@">
<title>@@TITLE@@</title>
<link rel="icon" href="@@ICON@@">
<link rel="stylesheet" href="@@PREFIX@@style.css?v=@@VER@@">
@@SCRIPT@@</head>
<body>
<a class="skip" href="#main">Skip to the deals</a>
@@SWITCH@@<header class="mast">
<div class="void" aria-hidden="true"><i></i></div>
<div class="mast-in">
<p class="kicker">@@KICKER@@</p>
<h1>@@HEADING@@</h1>
<p class="sub">@@SUB@@</p>
<nav class="pills" aria-label="Pages">@@NAV@@@@RESCAN_BTN@@</nav>
@@RESCAN_PANEL@@</div>
</header>
<main id="main" class="wrap">
@@BODY@@
</main>
<footer class="foot wrap">
<p>Prices are in AUD and leave out shipping. Check the store page before you buy.</p>
<p>Built automatically with GitHub Actions. This page is not affiliated with any store or with Wizards of the Coast.</p>
@@FOOT@@</footer>
</body>
</html>
"""

# ---------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------


def now_local():
    return datetime.now(_TZ)


def fmt_when(dt):
    hour = dt.hour % 12 or 12
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b %Y')}, {hour}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def fmt_day(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}"


_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_WRAPPED = re.compile(r'<div class="tablewrap">(<table\b.*?</table>)</div>', re.S | re.I)


def wrap_tables(body):
    """Puts every table in a scrolling, rounded panel. Tables already in a panel are left alone."""
    body = unwrap_tables(body)
    return _TABLE.sub(lambda m: f'<div class="tablewrap">{m.group(0)}</div>', body)


def unwrap_tables(body):
    return _WRAPPED.sub(r"\1", body)


def extract_body(doc):
    """Returns the original report stored in a page, or None. Reads both the current and the first layout."""
    a, z = doc.find(START), doc.find(END)
    if a != -1 and z > a:
        return unwrap_tables(doc[a + len(START):z]).strip("\n")
    m = re.search(r"<hr\s*/?>", doc, re.I)
    end = doc.lower().rfind("</body>")
    if m and end > m.end():
        return unwrap_tables(doc[m.end():end]).strip("\n")
    return None


def built_label(doc):
    m = re.search(r'<meta name="built-at" content="([^"]*)"', doc)
    if m:
        return html.unescape(m.group(1))
    m = re.search(r"built (.+?) Adelaide time", doc)
    return m.group(1).strip() if m else ""


def digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


BRAND = "MTG deals"
MAX_LABEL = 24


def parse_sites(spec):
    """
    Reads  Label|address;Label|address  into [(label, address)]. Returns (sites, warnings).
    Only complete http and https addresses are kept, so a typo or an unsafe address can never become a link.
    """
    sites, warnings, seen = [], [], set()
    for chunk in re.split(r"[;\n]+", spec or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, sep, address = chunk.partition("|")
        label, address = label.strip(), address.strip()
        if not sep or not label or not address:
            warnings.append(f"ignored '{chunk}': it should look like Label|https://address")
            continue
        parts = urlsplit(address)
        if parts.scheme not in ("http", "https") or not parts.netloc or " " in address:
            warnings.append(f"ignored '{chunk}': the address must start with https:// (or http://) and have no spaces")
            continue
        if len(label) > MAX_LABEL:
            warnings.append(f"ignored '{chunk}': the label is longer than {MAX_LABEL} characters")
            continue
        if label.lower() in seen:
            warnings.append(f"ignored '{chunk}': the label {label} is already used")
            continue
        seen.add(label.lower())
        sites.append((label, address))
    return sites, warnings


def parse_rescan(env):
    """
    Reads the RESCAN_* settings. Returns (settings or None, warnings). Anything that does not look right
    switches the button off, so a typo can never put odd text into the page.
    """
    repo = (env.get("RESCAN_REPO") or "").strip()
    workflow = (env.get("RESCAN_WORKFLOW") or "").strip()
    if not repo and not workflow:
        return None, []
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None, [f"RESCAN_REPO '{repo}' should look like owner/name, so there is no re-scan button"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow):
        return None, [f"RESCAN_WORKFLOW '{workflow}' should be a workflow file name such as mtg-deals.yml, so there is no re-scan button"]
    ref = (env.get("RESCAN_REF") or "main").strip()
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ".." in ref:
        return None, [f"RESCAN_REF '{ref}' is not a usable branch name, so there is no re-scan button"]
    warnings, inputs = [], {}
    for chunk in re.split(r"[,;\s]+", env.get("RESCAN_INPUTS") or ""):
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_-]+", key) or not re.fullmatch(r"[A-Za-z0-9_.-]*", value):
            warnings.append(f"RESCAN_INPUTS: ignored '{chunk}', it should look like name=value")
            continue
        inputs[key] = value
    try:
        expect = int(env.get("RESCAN_EXPECT") or 300)
    except ValueError:
        expect = 300
        warnings.append("RESCAN_EXPECT is not a whole number, so 300 seconds is used")
    expect = max(30, min(expect, 3600))
    return {"repo": repo, "workflow": workflow, "ref": ref, "inputs": inputs, "expect": expect}, warnings


def attr(value):
    return html.escape(value, quote=True)


def render_rescan(settings):
    """Returns (button, panel, script tag, footer line) for the front page, or empty strings."""
    if not settings:
        return "", "", "", ""
    button = (f'<button type="button" class="pill-btn" id="rescan-btn" aria-controls="rescan" '
              f'data-repo="{attr(settings["repo"])}" data-workflow="{attr(settings["workflow"])}" '
              f'data-ref="{attr(settings["ref"])}" data-inputs="{attr(json.dumps(settings["inputs"], separators=(",", ":")))}" '
              f'data-expect="{settings["expect"]}"><span id="rescan-label">Re-scan now</span></button>')
    panel = (
        '<section class="rescan" id="rescan" data-state="idle" aria-label="Re-scan" hidden>\n'
        '<div class="rs-head"><span class="rs-title" id="rs-title">Re-scan</span><span class="rs-time" id="rs-time"></span></div>\n'
        '<div class="rs-bar" id="rs-bar" role="progressbar" aria-label="Re-scan progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">'
        '<div class="rs-fill" id="rs-fill"></div></div>\n'
        '<p class="rs-msg" id="rs-msg" role="status" aria-live="polite"></p>\n'
        '<div class="rs-links"><a id="rs-run" href="#" target="_blank" rel="noopener noreferrer" hidden>Open the run on GitHub</a>'
        '<button type="button" class="rs-btn alt" id="rs-reload" hidden>Reload the page</button></div>\n'
        '<form class="rs-form" id="rs-form" autocomplete="off" hidden>\n'
        '<p>To start a scan from this page, paste a GitHub fine-grained token. It is kept in this browser only and is sent only to GitHub.</p>\n'
        '<ol><li><a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener noreferrer">Create a fine-grained token</a> on GitHub.</li>'
        '<li>Under Repository access, choose Only select repositories, then pick the bot repositories.</li>'
        '<li>Under Permissions, set Actions to Read and write.</li>'
        '<li>Paste the token below.</li></ol>\n'
        '<label for="rs-token">Token</label>\n'
        '<input id="rs-token" name="token" type="password" autocomplete="off" spellcheck="false">\n'
        '<p class="rs-err" id="rs-err" role="alert"></p>\n'
        '<div class="rs-row"><button type="submit" class="rs-btn">Save and start the scan</button>'
        '<button type="button" class="rs-btn alt" id="rs-cancel">Cancel</button></div>\n'
        '</form>\n</section>\n')
    script = f'<script src="rescan.js?v={digest(RESCAN_JS)}" defer></script>\n'
    foot = '<p><button type="button" class="rs-link" id="rs-forget" hidden>Forget the saved re-scan token</button></p>\n'
    return button, panel, script, foot


def render_switch(sites, current):
    """The bar at the top of every page. Empty when there are no links."""
    if not sites:
        return ""
    cur = (current or "").strip().lower()
    links = "".join(
        f'<a href="{html.escape(address, quote=True)}"' + (' aria-current="page"' if label.lower() == cur else "")
        + f">{html.escape(label)}</a>" for label, address in sites)
    return (f'<nav class="sites" aria-label="Sites"><div class="sites-in">'
            f'<span class="brand">{html.escape(BRAND)}</span>{links}</div></nav>\n')


def render(*, site_title, kicker, heading, sub, body, depth, current, built, doc_title, switch="", rescan=None):
    """Builds one full page. depth is 0 for the front page and 1 for pages inside archive/."""
    prefix = "../" if depth else ""
    latest, earlier = ("../", "./") if depth else ("./", "archive/")
    here = ' aria-current="page"'
    nav = (f'<a href="{latest}"{here if current == "latest" else ""}>Latest report</a>'
           f'<a href="{earlier}"{here if current == "archive" else ""}>Earlier reports</a>')
    button, panel, script, foot = render_rescan(rescan) if (depth == 0 and current == "latest") else ("", "", "", "")
    values = {"CSP": CSP, "BUILT": html.escape(built, quote=True), "TITLE": html.escape(doc_title), "ICON": ICON,
              "PREFIX": prefix, "VER": digest(CSS), "KICKER": html.escape(kicker), "HEADING": html.escape(heading),
              "SUB": sub, "NAV": nav, "BODY": body, "SWITCH": switch, "RESCAN_BTN": button,
              "RESCAN_PANEL": panel, "SCRIPT": script, "FOOT": foot}
    out = PAGE
    for key, value in values.items():
        out = out.replace(f"@@{key}@@", value)
    return out


# The deal history describes its colours as yellow, red, lavender and orange. This theme uses gold, violet and cyan.
RETEXT = (("(yellow, red NEW badge)", "(gold, NEW badge)"),
          ("(lavender, purple RECENT badge)", "(violet, RECENT badge)"),
          ("(orange badge)", "(cyan badge)"))


def report_body(raw):
    for old, new in RETEXT:
        raw = raw.replace(old, new)
    return f"{START}\n{wrap_tables(raw)}\n{END}"


def archive_body(days):
    if not days:
        return "<p>There are no earlier reports yet.</p>"
    items = []
    for i, day in enumerate(days):
        tag = '<span class="tag">Latest</span>' if i == 0 else f'<span class="iso">{day}</span>'
        items.append(f'<li><a href="{day}.html"><span>{html.escape(fmt_day(day))}</span>{tag}</a></li>')
    return ("<p>One page for each day, newest first. The last "
            f"{KEEP_DAYS} days are kept.</p>\n<ul class=\"archive\">\n" + "\n".join(items) + "\n</ul>")


def dated_files(archive_dir):
    days = []
    for p in archive_dir.iterdir() if archive_dir.is_dir() else []:
        m = DAY_FILE.match(p.name)
        if not m:
            continue
        try:
            date.fromisoformat(m.group(1))
        except ValueError:
            continue
        days.append(m.group(1))
    return sorted(days, reverse=True)


def build(report_path, out_dir, title, kicker, keep=KEEP_DAYS, now=None, sites=None, current_site="", rescan=None):
    """Writes the site. sites is a list of (label, address) for the bar at the top, and rescan holds the
    settings for the re-scan button (or None). Returns a short summary."""
    out = Path(out_dir)
    archive = out / "archive"
    raw = Path(report_path).read_text(encoding="utf-8", errors="replace")
    now = now or now_local()
    today, when = now.date().isoformat(), fmt_when(now)
    archive.mkdir(parents=True, exist_ok=True)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    (out / "style.css").write_text(CSS.lstrip("\n"), encoding="utf-8")
    script = out / "rescan.js"
    if rescan:
        script.write_text(RESCAN_JS.lstrip("\n"), encoding="utf-8")
    elif script.exists():
        script.unlink()

    sub_latest = f'Last scan <time datetime="{now.isoformat(timespec="minutes")}">{html.escape(when)}</time> {TZ_LABEL}'
    common = dict(site_title=title, kicker=kicker, heading=title, switch=render_switch(sites or [], current_site))
    # today's report, as the front page and as a dated page
    (out / "index.html").write_text(render(**common, sub=sub_latest, body=report_body(raw), depth=0, current="latest",
                                           built=when, doc_title=title, rescan=rescan), encoding="utf-8")
    (archive / f"{today}.html").write_text(
        render(**common, sub=f"Report for {html.escape(fmt_day(today))}, scanned {html.escape(when)} {TZ_LABEL}",
               body=report_body(raw), depth=1, current="archive", built=when,
               doc_title=f"{title}, {today}"), encoding="utf-8")

    # forget the oldest days, then restyle the rest from the reports they hold
    days = dated_files(archive)
    for old in days[keep:]:
        (archive / f"{old}.html").unlink()
    days = days[:keep]
    restyled, skipped = 0, []
    for day in days:
        if day == today:
            continue
        path = archive / f"{day}.html"
        doc = path.read_text(encoding="utf-8", errors="replace")
        body = extract_body(doc)
        if body is None:
            skipped.append(day)
            continue
        built = built_label(doc)
        sub = f"Report for {html.escape(fmt_day(day))}" + (f", scanned {html.escape(built)} {TZ_LABEL}" if built else "")
        path.write_text(render(**common, sub=sub, body=report_body(body), depth=1, current="archive",
                               built=built, doc_title=f"{title}, {day}"), encoding="utf-8")
        restyled += 1
    (archive / "index.html").write_text(
        render(**common, sub="Every report this page has published, newest first", body=archive_body(days),
               depth=1, current="archive", built=when, doc_title=f"{title}: earlier reports"), encoding="utf-8")
    return {"today": today, "kept": len(days), "restyled": restyled, "skipped": skipped}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the styled report pages.")
    ap.add_argument("--report", default="report.html")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--keep", type=int, default=KEEP_DAYS)
    args = ap.parse_args(argv)
    if not Path(args.report).is_file():
        print(f"{args.report} was not found, so the site was not changed.")
        return 0
    title = os.environ.get("SITE_TITLE") or "MTG sealed deals"
    kicker = os.environ.get("SITE_KICKER") or "Daily deal scan"
    sites, warnings = parse_sites(os.environ.get("SITE_NAV", ""))
    for w in warnings:
        print(f"SITE_NAV: {w}")
    current = os.environ.get("SITE_CURRENT", "")
    if sites and current and current.strip().lower() not in {l.lower() for l, _ in sites}:
        print(f"SITE_CURRENT: '{current}' does not match any label in SITE_NAV, so no link is highlighted")
    rescan, rescan_warnings = parse_rescan(os.environ)
    for w in rescan_warnings:
        print(w)
    info = build(args.report, args.out, title, kicker, keep=max(args.keep, 1), sites=sites, current_site=current,
                 rescan=rescan)
    print(f"Site written to {args.out}: report for {info['today']}, {info['kept']} dated page(s), "
          f"{info['restyled']} restyled" + (f", could not restyle {', '.join(info['skipped'])}" if info["skipped"] else "")
          + (", re-scan button on" if rescan else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
