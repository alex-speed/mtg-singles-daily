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
