/* First-run guide: a step-by-step tour for new visitors. It spotlights one part of the page at a time (the rest is dimmed but stays
   clickable), waits for the visitor to do the step themselves (open the scenarios, run one) or does it for them, then walks the
   results and the other three tabs with what each is for. ? Guide in the tab row starts or ends it; it opens by itself on the first
   visit (remembered in localStorage "sbx.guide") or with ?guide in the URL. It only calls the page's own functions. */
(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const q = (sel) => document.querySelector(sel);
  const fn = (name) => (typeof window[name] === "function" ? window[name] : () => {});
  const smooth = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
  const isRunning = () => typeof running !== "undefined" && !!running;
  const hadRun = () => typeof lastRun !== "undefined" && !!lastRun;
  const popOpen = () => !!byId("launch")?.classList.contains("pop");
  const guidedOpen = () => popOpen() && !byId("mode-guided")?.classList.contains("hidden");
  const shown = (el) => (el && el.getClientRects().length && !el.closest("[hidden]") ? el : null);
  const KEY = "sbx.guide";
  const store = { get: () => { try { return localStorage.getItem(KEY); } catch { return "x"; } }, set: (v) => { try { localStorage.setItem(KEY, v); } catch {} } };

  function goTab(tab, psec) {
    const was = typeof curTab !== "undefined" ? curTab : null;
    closePicker(); fn("showTab")(tab); if (psec) fn("showPsec")(psec);
    const m = q("main"); if (was !== tab && m && scrollY > m.offsetTop) scrollTo({ top: m.offsetTop });
  }
  function openPicker() { fn("showTab")("happened"); fn("setMode")("guided"); fn("collapseLaunch")(false); }
  function closePicker() { if (popOpen()) fn("collapseLaunch")(true); }
  const tabParts = (tab) => [q(`#tabsRow button[data-tab="${tab}"]`), q(`#tab-${tab} .psub`)];

  // Each step: target() -> element(s) to light up (none: a centred card), enter() prepares the page, done() advances on its own when
  // the visitor did the step, act = [label, run] does it for them, live() is a status line kept current while the step shows.
  // Each step is timeboxed (about 3 words a second, 8-25 s; secs overrides) and moves on when it runs out, unless hold (true, or a
  // function) says it waits for the visitor. ⏱ on the card turns that off (remembered in localStorage "sbx.guide.auto").
  const STEPS = [
    { id: "welcome", kick: "Welcome", wide: true, title: "Govern every AI call in one place",
      body: `<p>This sandbox puts <b>Azure API Management</b> in front of <b>Microsoft Foundry</b> models, MCP tools and A2A agents, as an AI gateway. Every call goes through it, so you can see:</p>
        <div class="gd-grid"><div><b>Who is calling</b>which app or user, and which plan pays</div><div><b>What's allowed</b>models, tools, token limits, safety</div>
        <div><b>What it costs</b>$ budgets enforced before the spend</div><div><b>The proof</b>logs and metrics for every decision</div></div>
        <p>In about three minutes you'll run one real scenario (a few calls, a fraction of a cent) and see where to find the what, the why, the cost and the proof.</p>`,
      next: "Start the tour", alt: "Not now", hold: true, enter: () => { closePicker(); goTab("happened"); } },
    { id: "tabs", kick: "The layout", title: "Four tabs, four questions",
      target: () => byId("tabsRow"), enter: () => { closePicker(); goTab("happened"); },
      body: `<ul><li><b>▶ Try it: what happened</b>: run something and watch it go through the gateway.</li><li><b>Why: policies</b>: the controls that decided it.</li>
        <li><b>What it cost</b>: budgets, prices and who pays.</li><li><b>Proof</b>: the logs and metrics behind every call.</li></ul>
        <p>The row stays at the top as you scroll. After each run, a dot marks the tabs worth a look.</p>` },
    { id: "open", kick: "Step 1 of 3 · Pick", title: "Open the scenarios",
      target: () => q('.lbar .mode[data-mode="guided"]'), enter: () => { closePicker(); goTab("happened"); }, pulse: true,
      body: `<p>Scenarios are short, scripted stories: each sends a small, fixed number of real calls and explains what the gateway did.</p>
        <p>Click <b>▶ Scenarios</b> to open the picker. (<b>✎ Build a request</b> next to it is for your own calls, later.)</p>`,
      done: guidedOpen, act: ["Open it for me", openPicker] },
    { id: "find", kick: "Step 1 of 3 · Pick", title: "Find one by what you want to show",
      target: () => [q("#scenarios .sctools"), byId("scChips")], enter: openPicker, needsPicker: true, alignEnd: true,
      body: `<p>Search by a control or a question (<i>token limit</i>, <i>who pays</i>, <i>failover</i>), or filter by topic.</p>
        <p><b>★ Start here</b> is a five-step story: identity, plan, budgets, routing, evidence. It's the best first pass for a customer conversation.</p>` },
    { id: "preview", kick: "Step 1 of 3 · Pick", title: "Preview it before you run it",
      target: () => { const pv = byId("scPv"); return shown(pv) && pv.textContent.trim() ? pv : byId("scList"); }, enter: openPicker, needsPicker: true,
      body: `<p>Selecting a scenario shows its diagram, what it costs and the <b>policies behind it</b>, before any call is sent.</p>
        <p><b>✎ Customize in the builder</b> turns it into your own request; <b>▶ Example in the flow</b> plays it without calling anything.</p>` },
    { id: "run", kick: "Step 2 of 3 · Run", title: "Run it",
      target: () => q("#scPick .scrun") || byId("scPv"), enter: openPicker, needsPicker: true, pulse: true, hold: true,
      body: `<p>Press <b>▶ Run</b>. The calls go through the gateway to Foundry for real, priced and logged like production traffic.</p>
        <p>The pop-out closes so you can watch. The first call can take up to half a minute while the scenario resets the budgets.</p>`,
      live: () => (q("#scPick .scrun") ? null : ["wait", "Select a scenario in the list to get its ▶ Run button."]),
      done: isRunning, act: ["Run it for me", () => { const b = q("#scPick .scrun"); if (b && !b.disabled) return b.click(); const r = q("#scList [data-i]"); if (r) fn("scRun")(Number(r.dataset.i)); }] },
    { id: "watch", kick: "Step 3 of 3 · See", title: "Watch what happened",
      target: () => byId("cvCard"), enter: () => { closePicker(); goTab("happened"); }, hold: isRunning,
      body: `<p>Each prompt and answer appears here as it happens, with its <b>status</b>, latency, tokens and <b>cost</b>. A blocked call shows the 401, 403 or 429 the app gets, and which control refused it.</p>
        <p><b>Details</b> under an answer opens what the gateway decided for that call.</p>`,
      live: () => { const rs = (byId("runStatus")?.textContent || "").trim(); return isRunning() ? ["run", /^running/i.test(rs) ? rs : `Running${rs ? ": " + rs : "…"}`]
        : hadRun() ? ["ok", "Finished. Read the answers above, then Next for what it means."] : ["wait", "No run yet: go back a step and press ▶ Run, or continue the tour without one."]; } },
    { id: "takeaway", kick: "Results", title: "The takeaway, and where to look next",
      target: () => { const look = byId("scLook"); if (shown(look) && look.children.length) return look; const sc = byId("scenario"); return shown(sc) ? sc : null; },
      enter: () => goTab("happened"), skip: (t) => !t,
      body: `<p>The scenario card says what the run proved. <b>Where to look next</b> picks up to four places in the other tabs for <i>this</i> run, the most useful first.</p>
        <p>The bar at the top now offers <b>↻ Run again</b> and <b>Next ›</b> for the following scenario.</p>` },
    { id: "flow", kick: "Results", title: "Follow one call through the gateway",
      target: () => [q("#gwFlow > h2"), byId("gwfSplit")], enter: () => goTab("happened", "investigate"),
      body: `<p>The <b>Gateway flow</b> replays each call stage by stage: identity, plan, limits and budgets, routing to a model region, metering.</p>
        <p><b>Policies at work</b> beside it lists what each control did: ✓ applied, ↷ changed the call, ✗ refused it. Click a call in the request log to replay it.</p>` },
    { id: "why", kick: "The other tabs", title: "Why: policies",
      target: () => tabParts("why"), enter: () => goTab("why", "explorer"),
      body: `<p><b>Policy explorer</b>: every control on the gateway, what it does, its exact lines in the deployed policy, and the scenarios that show it.</p>
        <p><b>AI Gateway</b>: a product of its own built for agentic workloads, not an API Management tier. When a customer should add it to Foundry, and how these controls carry over.</p>
        <div class="gd-val">governance is readable configuration, not a black box. Every decision you saw traces back to a line you can show.</div>` },
    { id: "cost", kick: "The other tabs", title: "What it cost",
      target: () => tabParts("cost"), enter: () => goTab("cost", "spend"),
      body: `<p><b>Budgets &amp; spend</b>: $ left per key and how fast it burns. <b>Plans &amp; prices</b>: what each plan allows and costs. <b>Chargeback</b>: spend per user, team and session.</p>
        <div class="gd-val">every token, tool call and agent task is priced and attributed at the gateway, so FinOps gets showback and hard budgets without app changes.</div>` },
    { id: "proof", kick: "The other tabs", title: "Proof: logs & monitoring",
      target: () => tabParts("proof"), enter: () => goTab("proof", "telemetry"),
      body: `<p><b>Logs &amp; telemetry</b>: where each piece of evidence lands (response headers, traces, Log Analytics, Application Insights), with live KQL. <b>Azure Monitor</b>: cost, outcome and token charts.</p>
        <div class="gd-val">security, platform and finance teams see the same numbers the app saw, in the tools they already use.</div>` },
    { id: "copilot", kick: "Help along the way", title: "Ask Copilot",
      target: () => shown(q(".cop-fab")), enter: () => goTab("happened"), skip: (t) => !t,
      body: `<p>Ask about plans, policies, your calls or the logs. It answers from the live gateway configuration and your session, and highlights the answer on the page.</p>
        <p>It's read-only: it never changes the gateway or sends calls for you.</p>` },
    { id: "done", kick: "You're set", title: "Where to go from here",
      target: () => byId("gdBtn"), enter: () => goTab("happened"),
      body: `<ul><li><b>★ Start here</b> in the scenarios walks five scenarios in story order.</li><li><b>✎ Build a request</b> lets you pick any caller, model, tool or agent.</li>
        <li><b>Keys + Entra | Entra only</b> in the bar switches how callers authenticate.</li></ul><p>Reopen this tour any time with <b>? Guide</b>.</p>`,
      next: "Finish", hold: true },
  ];

  const G = { on: false, i: 0, raf: 0, st: {}, card: null, hole: null, dim: null, last: "", t: null, tick: 0, el: 0, dur: 0, prev: 0, hover: false };
  const AUTO = "sbx.guide.auto";
  const auto = { get: () => { try { return localStorage.getItem(AUTO) !== "off"; } catch { return true; } }, set: (on) => { try { localStorage.setItem(AUTO, on ? "on" : "off"); } catch {} } };
  const held = (s) => (typeof s.hold === "function" ? !!s.hold() : !!s.hold);
  const stepSecs = (s) => { if (s.secs) return s.secs; const d = document.createElement("div"); d.innerHTML = s.title + " " + s.body; const w = d.textContent.split(/\s+/).filter(Boolean).length; return Math.max(8, Math.min(25, Math.round(w / 3) + 3)); };

  const btn = document.createElement("button");
  btn.type = "button"; btn.id = "gdBtn"; btn.className = "gd-btn"; btn.setAttribute("aria-pressed", "false");
  btn.title = "A guided tour: run your first scenario and see what each part of the page is for";
  btn.innerHTML = `<span class="q" aria-hidden="true">?</span><span class="t">Guide</span>`;
  btn.onclick = () => (G.on ? end() : start(0));
  (function mount(tries = 0) {
    const row = byId("tabsRow"), sel = row?.querySelector(".viewsel");
    if (!row) { if (tries < 100) setTimeout(() => mount(tries + 1), 100); return; }
    row.insertBefore(btn, sel || null);
  })();

  function start(i = 0) {
    if (G.on) return show(i);
    G.on = true; btn.classList.add("on"); btn.setAttribute("aria-pressed", "true");
    G.card = document.createElement("section"); G.card.className = "gd-card"; G.card.setAttribute("role", "dialog"); G.card.setAttribute("aria-live", "polite");
    G.hole = document.createElement("div"); G.hole.className = "gd-hole"; G.hole.setAttribute("aria-hidden", "true");
    G.dim = document.createElement("div"); G.dim.className = "gd-dim"; G.dim.setAttribute("aria-hidden", "true");
    document.body.append(G.dim, G.hole, G.card);
    G.card.addEventListener("click", onCard);
    G.card.addEventListener("mouseenter", () => { G.hover = true; });
    G.card.addEventListener("mouseleave", () => { G.hover = false; });
    show(i);
  }
  function end(finished = false) {
    if (!G.on) return;
    store.set(finished ? "done" : "seen");
    G.on = false; cancelAnimationFrame(G.raf); G.raf = 0;
    [G.card, G.hole, G.dim].forEach((e) => e?.remove()); G.card = G.hole = G.dim = null;
    btn.classList.remove("on"); btn.setAttribute("aria-pressed", "false");
    if (!finished) { btn.classList.remove("hello"); void btn.offsetWidth; btn.classList.add("hello"); }
  }
  function show(i) {
    G.i = Math.max(0, Math.min(STEPS.length - 1, i)); G.st = {}; G.last = ""; G.t = null;
    const s = STEPS[G.i];
    G.el = 0; G.prev = 0; G.dur = stepSecs(s) * 1000;
    s.enter?.();
    // after the page has switched tab / opened the pop-out, find the target; steps without one on this page (no run yet) are skipped
    setTimeout(() => {
      if (!G.on || STEPS[G.i] !== s) return;
      const t = targets(s);
      if (s.skip?.(t.length ? t : null)) return show(G.i + (G.dir < 0 ? -1 : 1));
      if (t.length && !t[0].closest(".lbody")) {
        const r = rectOf(t), tabsH = byId("tabsRow")?.offsetHeight || 0, launchH = byId("launch")?.offsetHeight || 0;
        if (r.top < tabsH + launchH + 8 || r.bottom > innerHeight - 8) {
          const el = t[t.length - 1] === t[0] ? t[0] : t[0].closest("#tabsRow") ? t[1] : t[0];
          if (!el.closest("#tabsRow")) {
            const er = el.getBoundingClientRect(), top = tabsH + launchH + 16;
            scrollTo({ top: scrollY + (r.height > innerHeight * 0.55 ? Math.min(r.top, er.top) - top : er.top + er.height / 2 - (top + innerHeight) / 2), behavior: smooth() });
          }
        }
      }
      render(); loop();
    }, s.needsPicker ? 250 : 80);
  }
  function targets(s) {
    const t = s.target?.();
    return (Array.isArray(t) ? t : [t]).filter((e) => shown(e));
  }
  function rectOf(els) {
    const rs = els.map((e) => e.getBoundingClientRect());
    const l = Math.min(...rs.map((r) => r.left)), t = Math.min(...rs.map((r) => r.top)), r = Math.max(...rs.map((r) => r.right)), b = Math.max(...rs.map((r) => r.bottom));
    return new DOMRect(l, t, r - l, b - t);
  }
  function render() {
    const s = STEPS[G.i], n = STEPS.length, live = s.live?.();
    G.last = JSON.stringify(live || null);
    G.card.className = "gd-card" + (s.wide ? " wide" : "");
    G.card.setAttribute("aria-label", `Guide, step ${G.i + 1} of ${n}: ${s.title}`);
    const dots = STEPS.map((_, k) => `<i class="${k === G.i ? "on" : k < G.i ? "done" : ""}"></i>`).join("");
    const liveHtml = live ? `<div class="gd-live ${live[0] === "ok" ? "ok" : live[0] === "wait" ? "wait" : ""}">${live[0] === "run" ? `<span class="spin"></span>` : live[0] === "ok" ? "✓" : "•"}<span>${live[1]}</span></div>` : "";
    const waits = !!s.done, first = G.i === 0;
    G.card.innerHTML = `<div class="gd-top"><span class="gd-kick">${s.kick}</span><span class="gd-dots" aria-hidden="true">${dots}</span>
        <button type="button" class="gd-x" data-gd="end" title="End the guide (you can reopen it with ? Guide)" aria-label="End the guide">✕</button></div>
      <h3>${s.title}</h3><div class="gd-body">${s.body}${liveHtml}</div>
      <div class="gd-timer"><span class="gd-bar" aria-hidden="true"><i></i></span>
        <button type="button" class="gd-auto" data-gd="auto" aria-pressed="${auto.get()}" title="Move to the next step by itself when the time runs out (hover over this card to pause)"><span class="ic" aria-hidden="true">⏱</span><span class="lbl"></span></button></div>
      <div class="gd-foot">${first ? `<button type="button" class="gd-ghost" data-gd="end">${s.alt || "Not now"}</button>` : `<button type="button" class="gd-ghost" data-gd="back">‹ Back</button>`}
        <span class="sp"></span>
        ${s.act ? `<button type="button" class="${waits ? "gd-do" : "gd-sec"}" data-gd="act">${s.act[0]}</button>` : ""}
        <button type="button" class="${s.act ? "gd-sec" : "gd-do"}" data-gd="next">${s.next || (waits ? "Skip ›" : "Next ›")}</button></div>`;
    G.hole.classList.toggle("pulse", !!s.pulse);
    timer();
    place();
    if (first) G.card.querySelector('[data-gd="next"]')?.focus({ preventScroll: true });
  }
  function onCard(e) {
    const b = e.target.closest("[data-gd]"); if (!b) return;
    const k = b.dataset.gd, s = STEPS[G.i];
    if (k === "end") end(G.i === STEPS.length - 1);
    else if (k === "back") { G.dir = -1; show(G.i - 1); }
    else if (k === "next") { G.dir = 1; if (G.i === STEPS.length - 1) { end(true); fn("showTab")("happened"); } else show(G.i + 1); }
    else if (k === "act") { G.dir = 1; s.act[1](); }
    else if (k === "auto") { const on = !auto.get(); auto.set(on); G.el = 0; timer(); }
  }
  // The step's timebox: the bar fills and the label counts down; it pauses while the pointer is on the card or the page is hidden
  function timer() {
    const box = G.card?.querySelector(".gd-timer"); if (!box) return;
    const s = STEPS[G.i], on = auto.get(), wait = held(s), paused = G.hover || document.hidden;
    const left = Math.max(0, Math.ceil((G.dur - G.el) / 1000));
    box.classList.toggle("off", !on || wait); box.classList.toggle("paused", on && !wait && paused);
    box.querySelector(".gd-bar i").style.width = on && !wait ? `${Math.min(100, (G.el / G.dur) * 100)}%` : "0";
    const b = box.querySelector(".gd-auto"); b.setAttribute("aria-pressed", String(on));
    b.querySelector(".lbl").textContent = !on ? "Auto-next off" : wait ? "Auto-next waits for you" : paused ? `Paused · ${left}s` : `Next in ${left}s`;
  }
  function expire(s) {
    G.dir = 1;
    if (s.act && !s.done?.(G.st)) return s.act[1]();  // e.g. open the picker: its done() then moves the tour on
    if (G.i < STEPS.length - 1) show(G.i + 1);
  }
  // Keep the spotlight on its target as the page scrolls or re-renders, refresh the live line, and move on when the visitor did the step
  function loop() {
    cancelAnimationFrame(G.raf);
    const tick = () => {
      if (!G.on) return;
      const s = STEPS[G.i];
      if (s.needsPicker && !popOpen() && !isRunning() && !G.closing) { G.closing = true; G.dir = -1; setTimeout(() => { G.closing = false; if (G.on && STEPS[G.i] === s && !popOpen()) show(STEPS.findIndex((x) => x.id === "open")); }, 400); }
      if (s.done?.(G.st)) { G.dir = 1; return show(G.i + 1); }
      if (++G.tick % 20 === 0 && s.live && JSON.stringify(s.live() || null) !== G.last) render();
      const now = performance.now(), dt = G.prev ? Math.min(250, now - G.prev) : 0; G.prev = now;
      if (auto.get() && !held(s) && !G.hover && !document.hidden && G.card?.querySelector(".gd-timer")) {
        G.el += dt;
        if (G.el >= G.dur) { G.el = 0; expire(s); if (STEPS[G.i] !== s || !G.on) return; }
      }
      if (G.tick % 6 === 0) timer();
      place();
      G.raf = requestAnimationFrame(tick);
    };
    G.raf = requestAnimationFrame(tick);
  }
  function place() {
    const s = STEPS[G.i], t = targets(s), card = G.card;
    if (!t.length) {
      G.hole.style.display = "none"; G.dim.style.display = "";
      card.style.left = `${Math.max(12, (innerWidth - card.offsetWidth) / 2)}px`; card.style.top = `${Math.max(12, (innerHeight - card.offsetHeight) / 2)}px`;
      return;
    }
    G.dim.style.display = "none"; G.hole.style.display = "";
    const r = rectOf(t), pad = 6, vw = innerWidth, vh = innerHeight;
    const hl = Math.max(4, r.left - pad), ht = Math.max(4, r.top - pad), hr = Math.min(vw - 4, r.right + pad), hb = Math.min(vh - 4, r.bottom + pad);
    Object.assign(G.hole.style, { left: `${hl}px`, top: `${ht}px`, width: `${Math.max(0, hr - hl)}px`, height: `${Math.max(0, hb - ht)}px` });
    const cw = card.offsetWidth, ch = card.offsetHeight, gap = 14, m = 12;
    const fitsX = (x) => Math.max(m, Math.min(s.alignEnd ? hr - cw : x, vw - cw - m));  // alignEnd: keep the left of the target visible
    let x, y;
    if (hb + gap + ch <= vh - m) { y = hb + gap; x = fitsX(hl); }                    // below
    else if (ht - gap - ch >= m) { y = ht - gap - ch; x = fitsX(hl); }               // above
    else if (hr + gap + cw <= vw - m) { x = hr + gap; y = Math.max(m, Math.min(ht, vh - ch - m)); }  // right
    else if (hl - gap - cw >= m) { x = hl - gap - cw; y = Math.max(m, Math.min(ht, vh - ch - m)); }  // left
    else { x = vw - cw - m - 72; y = vh - ch - m; if (x < m) x = fitsX(m); }        // over a large target: bottom right, clear of Ask Copilot
    card.style.left = `${x}px`; card.style.top = `${y}px`;
  }
  addEventListener("keydown", (e) => {
    if (!G.on || e.key !== "Escape" || !G.card.contains(document.activeElement)) return;
    e.stopPropagation(); end();
  }, true);
  addEventListener("resize", () => { if (G.on) place(); });

  // First visit (or ?guide in the URL): open on the welcome card once the page has loaded its configuration and the scenarios
  (function boot(tries = 0) {
    const ready = typeof cfg !== "undefined" && cfg && byId("scList") && byId("gdBtn");
    if (!ready) { if (tries < 150) setTimeout(() => boot(tries + 1), 200); return; }
    const force = /[?&]guide\b/.test(location.search);
    if (force || !store.get()) setTimeout(() => { if (!G.on && !isRunning()) start(0); }, 600);
  })();
  window.sandboxGuide = { start, end, steps: () => STEPS.map((s) => s.id), get step() { return G.on ? STEPS[G.i].id : null; } };
})();
