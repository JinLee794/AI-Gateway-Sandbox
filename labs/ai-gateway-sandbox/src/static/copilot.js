/* Sandbox Copilot: a floating assistant that answers questions about this sandbox from the live API Management configuration
   and the user's session in Azure Monitor, and drives the page while it answers. app.py runs the agent (POST /api/copilot) and
   streams JSON Lines events (llm, step, step_done, action, answer, error); this file renders the chat and runs the UI actions
   it receives: navigate, highlight (a spotlight with a caption), open a call's evidence, play a flow example, draw a policy as
   a step-by-step flow and prefill the request builder. It only calls the page's own functions and never sends gateway calls. */
(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const q = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const page = (name) => (typeof window[name] === "function" ? window[name] : () => {});
  const smooth = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
  const circled = (n) => (typeof n === "number" && n >= 1 && n <= 20 ? String.fromCharCode(0x245f + n) : "•");
  const pageCalls = () => (typeof session !== "undefined" && Array.isArray(session.calls) ? session.calls : []);
  const cardOf = (el) => (el && el.closest(".card")) || el;
  const ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 2.5l1.95 5.05L18 9.5l-5.05 1.95L11 16.5l-1.95-5.05L4 9.5l5.05-1.95z"/><path d="M18.5 13.5l.95 2.05 2.05.95-2.05.95-.95 2.05-.95-2.05-2.05-.95 2.05-.95z"/><path d="M6.5 16l.7 1.55 1.55.7-1.55.7-.7 1.55-.7-1.55-1.55-.7 1.55-.7z"/></svg>`;

  const S = { msgs: [], busy: false, ctrl: null, spots: [], raf: 0, queue: Promise.resolve(), conf: {} };

  // ---------- DOM: floating button + pop-out chat ----------
  const fab = document.createElement("button");
  fab.className = "cop-fab"; fab.type = "button"; fab.title = "Ask Sandbox Copilot about plans, policies, your calls and the logs";
  fab.setAttribute("aria-label", "Ask Sandbox Copilot"); fab.setAttribute("aria-controls", "copPanel");
  fab.innerHTML = `${ICON}<span>Ask Copilot</span>`;
  const panel = document.createElement("section");
  panel.className = "cop-panel"; panel.id = "copPanel"; panel.hidden = true; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "Sandbox Copilot");
  panel.innerHTML = `<header class="cop-head"><span class="cop-logo">${ICON}</span>
      <div class="cop-ttl"><b>Sandbox Copilot</b><small>Knows this gateway's configuration and your session's logs</small></div>
      <button class="cop-ib" type="button" id="copNew" title="New chat">↺</button><button class="cop-ib" type="button" id="copClose" title="Close (Esc)" aria-label="Close">✕</button></header>
    <div class="cop-body" id="copBody" aria-live="polite"></div>
    <div class="cop-ctx" id="copCtx"></div>
    <form class="cop-compose" id="copForm"><textarea id="copInput" rows="1" placeholder="Ask about plans, policies, your calls or the logs…" aria-label="Ask Sandbox Copilot"></textarea>
      <button type="submit" id="copSend" title="Send (Enter)" aria-label="Send">➤</button></form>
    <div class="cop-foot" id="copFoot">Read-only: it explains and shows, it never changes the gateway or sends requests for you.</div>`;
  document.body.append(fab, panel);
  document.body.classList.add("has-copilot");
  const chat = byId("copBody"), input = byId("copInput"), send = byId("copSend");

  // The page's config arrives asynchronously (index.html init): hide the button if app.py turned the copilot off
  (function boot(tries = 0) {
    const c = typeof cfg !== "undefined" ? cfg : null;
    if (!c) { if (tries < 300) setTimeout(() => boot(tries + 1), 200); return; }
    S.conf = c.copilot || {};
    if (S.conf.enabled === false) { fab.hidden = true; return; }
    byId("copFoot").innerHTML = `Answers by <b>${esc(S.conf.model || "a model")}</b> through this gateway (key <code>${esc(S.conf.subscription || "sandbox-copilot")}</code> on the `
      + `<code>${esc(S.conf.plan || "agent-platform")}</code> plan), governed and metered like any other agent. Read-only.`;
  })();

  function open(on = true) {
    panel.hidden = !on; fab.hidden = on || S.conf.enabled === false;
    document.body.classList.toggle("cop-open", on);
    if (on) { if (!chat.children.length) intro(); ctxLine(); setTimeout(() => input.focus(), 30); }
    else { clearSpots(); fab.focus(); }
  }

  function suggestions() {
    const calls = pageCalls(), out = [];
    for (let i = calls.length - 1; i >= 0; i--) if ([401, 403, 429].includes(calls[i].status)) { out.push(`Why was call #${i + 1} blocked?`); break; }
    if (calls.length) out.push("What did my session cost, according to Application Insights?");
    const a = typeof selectedAgent !== "undefined" ? selectedAgent : null;
    out.push(a ? `What can ${a.displayName} do on the ${a.tier} plan?` : "What does each plan allow?",
      "Walk me through what the product policy does", "Which demo users exist, and on which plans?", "How does regional failover work here?");
    return out.slice(0, 4);
  }
  function intro() {
    chat.innerHTML = `<div class="cop-intro"><div class="cop-hi">${ICON}</div><b>Ask me anything about this sandbox.</b>
      <p>I read the live API Management configuration and query Azure Monitor for your session, then point at the parts of the page that matter.</p></div>
      <div class="cop-sugs">${suggestions().map((s) => `<button type="button" class="cop-sug">${esc(s)}</button>`).join("")}</div>`;
  }

  // ---------- What the copilot can see: the user's view, session and the calls this page sent ----------
  const TAB_NAMES = { happened: "What happened", why: "Why: policies", cost: "What it cost", proof: "Proof: logs & monitoring" };
  const PSEC_NAMES = { investigate: "Gateway flow", live: "Session overview", explorer: "Policy explorer", spend: "Budgets & spend", plans: "Plans & prices",
    chargeback: "Chargeback", telemetry: "Logs & telemetry", monitor: "Azure Monitor" };
  function viewLabel() {
    const tab = typeof curTab !== "undefined" ? curTab : "happened", sub = typeof psec !== "undefined" ? psec : "";
    let s = TAB_NAMES[tab] || tab;
    if (sub) {
      s += ` › ${PSEC_NAMES[sub] || sub}`;
      if (sub === "live" && typeof viewRun !== "undefined" && viewRun != null) s += ` (showing the report of run #${viewRun} instead of the live session)`;
      if (sub === "investigate" && typeof evIndex !== "undefined" && evIndex >= 0 && !byId("evPanel")?.classList.contains("hidden")) s += ` (evidence of call #${evIndex + 1} open)`;
    }
    const m = typeof mode !== "undefined" ? mode : "";
    if (m && !byId("launch")?.classList.contains("collapsed")) s += ` · launch panel: ${m === "build" ? "request builder" : "guided scenarios"}`;
    return s;
  }
  function ctxLine() {
    const sid = byId("sessionId")?.value.trim(), n = pageCalls().length;
    byId("copCtx").innerHTML = `I can see: <b>${esc(viewLabel())}</b>${sid ? ` · session <code>${esc(sid)}</code>` : ""} · ${n} call${n === 1 ? "" : "s"} on this page`;
  }
  function callInfo(r, i, ref) {
    const h = r.headers || {};
    return { n: i + 1, ref: ref || undefined, at: r.at ? new Date(r.at).toISOString() : undefined, run: r.run, past: r.past || undefined, agent: r.agent, surface: r.surface,
      status: r.status, target: r.surface === "tool" ? (r.tool || h["x-gw-tool"]) : r.surface === "agent" ? "sourcing-agent" : r.requestedModel,
      served: h["x-gw-model-served"], blockedBy: h["x-gw-blocked-by"], governance: h["x-gw-governance"] && h["x-gw-governance"] !== "none" ? h["x-gw-governance"] : undefined,
      costUsd: typeof callCost === "function" ? +callCost(r).toFixed(6) : undefined, tokens: r.tokens, session: h["x-gw-session"], requestId: h["x-gw-request-id"],
      traceId: h["apim-trace-id"], backend: h["x-gw-backend"], latencyMs: r.latencyMs, error: r.error ? String(r.error).slice(0, 300) : undefined,
      answer: typeof r.content === "string" ? r.content.slice(0, 300) : undefined, headers: h,
      steps: Array.isArray(r.steps) ? r.steps.slice(0, 10).map((s) => ({ type: s.type, name: s.name, status: s.status, costMicroUsd: s.costMicroUsd, billedTo: s.billedTo })) : undefined };
  }
  function context() {
    // the latest 40 calls, plus any older call the conversation mentions as #n
    const calls = pageCalls(), first = Math.max(0, calls.length - 40), picked = new Map();
    for (const m of S.msgs) for (const [, n] of String(m.content).matchAll(/#(\d{1,5})\b/g)) if (+n - 1 < first && calls[+n - 1]) picked.set(+n - 1, true);
    for (let i = first; i < calls.length; i++) picked.set(i, false);
    const out = [...picked.keys()].sort((x, y) => x - y).map((i) => callInfo(calls[i], i, picked.get(i)));
    const a = typeof selectedAgent !== "undefined" ? selectedAgent : null, sc = byId("scenario");
    return { view: viewLabel(), session: byId("sessionId")?.value.trim() || null, endUser: byId("endUser")?.value.trim() || "",
      caller: a ? `${a.displayName} (${a.name}, ${a.tier} plan)` : "", scenario: sc && !sc.classList.contains("hidden") ? byId("scTitle")?.textContent || "" : "",
      runs: (typeof runs !== "undefined" ? runs : []).slice(-25).map((r) => ({ n: r.n, title: r.title, status: r.status, calls: (r.calls || []).length,
        sessions: [...new Set((r.calls || []).map((i) => calls[i]?.headers?.["x-gw-session"]).filter((s) => s && s !== "no-session"))].slice(0, 6) })), calls: out };
  }

  // ---------- Asking: one question streams back as JSON Lines events ----------
  function setBusy(on) {
    S.busy = on; send.textContent = on ? "■" : "➤"; send.title = on ? "Stop" : "Send (Enter)"; send.setAttribute("aria-label", on ? "Stop" : "Send");
    send.classList.toggle("stop", on);
  }
  function scrollDown() { chat.scrollTop = chat.scrollHeight; }
  function addUser(text) {
    const el = document.createElement("div"); el.className = "cop-msg user"; el.textContent = text; chat.appendChild(el); scrollDown();
  }
  function addBot() {
    const el = document.createElement("div"); el.className = "cop-msg bot";
    el.innerHTML = `<details class="cop-how" open><summary><span class="cop-think"><span class="cop-dots"><i></i><i></i><i></i></span><span class="t">Thinking…</span></span></summary><ol></ol></details>`
      + `<div class="cop-txt"></div><div class="cop-vis"></div><div class="cop-shown" hidden><span class="l">On the page:</span></div>`;
    chat.appendChild(el); scrollDown();
    return { el, list: el.querySelector(".cop-how ol"), sum: el.querySelector(".cop-how summary"), steps: {}, shown: 0, lookups: 0, failed: false };
  }
  function think(b, text) { const t = b.sum.querySelector(".t"); if (t) t.textContent = text; }
  function addStep(b, id, label, cls = "") {
    const li = document.createElement("li"); li.className = `run ${cls}`;
    li.innerHTML = `<span class="st"></span><span class="lb">${esc(label)}</span><small></small>`;
    b.list.appendChild(li); b.steps[id] = li; scrollDown();
  }
  function doneStep(b, id, ok, summary, link) {
    const li = b.steps[id]; if (!li) return;
    li.classList.remove("run"); li.classList.add(ok ? "ok" : "bad");
    li.querySelector("small").textContent = summary ? `· ${summary}` : "";
    if (link && /^https:\/\//.test(link)) li.insertAdjacentHTML("beforeend", `<a href="${esc(link)}" target="_blank" rel="noopener" title="Run the same query in the Azure portal">Logs ↗</a>`);
  }
  const fmtTok = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0));
  function summarize(b, usage) {
    const u = usage || {}, tokens = (u.promptTokens || 0) + (u.completionTokens || 0);
    b.sum.innerHTML = `How I found this · ${b.lookups} lookup${b.lookups === 1 ? "" : "s"} · ${u.gatewayCalls || 0} gateway call${u.gatewayCalls === 1 ? "" : "s"} · `
      + `${fmtTok(tokens)} tokens · $${Number(u.costUsd || 0).toFixed(4)}`;
    b.sum.parentElement.open = false;
  }
  function fail(b, message, ev = {}) {
    b.failed = true;
    const h = ev.headers || {};
    const governed = h["x-gw-blocked-by"] ? `<small>The gateway governed the copilot itself: <code>${esc(h["x-gw-blocked-by"])}</code>`
      + `${h["x-gw-tier"] ? ` on the <code>${esc(h["x-gw-tier"])}</code> plan` : ""}${h["retry-after"] ? `. Try again in ${esc(h["retry-after"])} s` : ""}. Same policies as every other agent.</small>` : "";
    b.el.querySelector(".cop-txt").innerHTML = `<div class="cop-err">${esc(message)}${governed}</div>`;
    b.sum.innerHTML = ev.usage ? `What I tried · ${ev.usage.gatewayCalls || 0} gateway call(s)` : "What I tried";
    if (!b.list.children.length) b.sum.parentElement.hidden = true;
  }
  function onEvent(ev, b) {
    if (ev.type === "llm") {
      const id = "llm-" + ev.n;
      if (ev.state === "start") {
        think(b, ev.n > 1 ? "Working out the answer…" : "Thinking…");
        addStep(b, id, `Model call ${ev.n} through the gateway (${ev.model || "model"})`, "gw");
      } else {
        const h = ev.headers || {}, served = h["x-gw-model-served"];
        doneStep(b, id, ev.status === 200, [`HTTP ${ev.status}`, served ? `served ${served}` : "", ev.tokens ? `${fmtTok(ev.tokens)} tokens` : "",
          h["x-gw-cost-usd"] ? `$${Number(h["x-gw-cost-usd"]).toFixed(5)}` : "", ev.latencyMs ? `${ev.latencyMs} ms` : ""].filter(Boolean).join(" · "));
      }
    } else if (ev.type === "step") {
      b.lookups++; think(b, ev.label || "Looking it up…"); addStep(b, ev.id, ev.label || ev.tool);
    } else if (ev.type === "step_done") {
      doneStep(b, ev.id, ev.ok, ev.summary, ev.portalLink);
    } else if (ev.type === "action") {
      if (ev.action.type === "open_reference") b.ref = ev.action;  // the lightbox covers the page: open it after the answer's other actions
      else S.queue = S.queue.then(() => act(ev.action, b)).catch((e) => console.error("Copilot action failed", ev.action, e));
    } else if (ev.type === "answer") {
      b.el.querySelector(".cop-txt").innerHTML = md(ev.text || "");
      summarize(b, ev.usage);
      S.msgs.push({ role: "assistant", content: String(ev.text || "").slice(0, MSG_CHARS) });
      S.queue = S.queue.then(() => pointAtPipeline(b)).then(() => b.ref && act(b.ref, b)).catch(console.error);
      return true;
    } else if (ev.type === "error") {
      fail(b, ev.error || "Something went wrong.", ev);
    }
    scrollDown();
    return false;
  }
  // The conversation sent with each question: the newest turns that fit app.py's COPILOT_HISTORY / COPILOT_HISTORY_CHARS
  const MSG_CHARS = 12000, HISTORY = 40, HISTORY_CHARS = 120000;
  function history() {
    const out = []; let chars = 0;
    for (let i = S.msgs.length - 1; i >= 0 && out.length < HISTORY; i--) {
      chars += S.msgs[i].content.length; if (chars > HISTORY_CHARS) break;
      out.unshift(S.msgs[i]);
    }
    while (out.length > 1 && out[0].role !== "user") out.shift();
    return out;
  }
  async function ask(text) {
    text = String(text || "").trim();
    if (!text || S.busy) return;
    clearSpots(); chat.querySelector(".cop-intro")?.remove(); chat.querySelector(".cop-sugs")?.remove();
    addUser(text); S.msgs.push({ role: "user", content: text.slice(0, MSG_CHARS) });
    input.value = ""; autosize(); ctxLine();
    const b = addBot(); let answered = false;
    setBusy(true); S.ctrl = new AbortController();
    try {
      const res = await fetch("/api/copilot", { method: "POST", headers: { "Content-Type": "application/json" }, signal: S.ctrl.signal,
        body: JSON.stringify({ messages: history(), ui: context() }) });
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || `HTTP ${res.status}`); }
      const take = (line) => { line = line.trim(); if (!line) return; let ev; try { ev = JSON.parse(line); } catch { return; } if (onEvent(ev, b)) answered = true; };
      if (res.body?.getReader) {
        const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
        for (;;) {
          const { value, done } = await reader.read(); if (done) break;
          buf += dec.decode(value, { stream: true });
          for (let i = buf.indexOf("\n"); i >= 0; i = buf.indexOf("\n")) { take(buf.slice(0, i)); buf = buf.slice(i + 1); }
        }
        take(buf + dec.decode());
      } else (await res.text()).split("\n").forEach(take);
      if (!answered && !b.failed) fail(b, "The copilot stopped without an answer. Try again.");
    } catch (e) {
      fail(b, e.name === "AbortError" ? "Stopped."
        : e instanceof TypeError ? "The sandbox UI server isn't responding (is app.py still running?). Restart it, then ask again."
        : `Couldn't reach the copilot: ${e.message}`);
    } finally {
      if (!answered) S.msgs.pop();  // keep the history to answered turns
      setBusy(false); S.ctrl = null; scrollDown();
    }
  }

  // ---------- UI actions streamed by the agent ----------
  const VIEW_NAMES = { scenarios: "Guided scenarios", builder: "Request builder", conversation: "Conversation", live: "What happened › Session overview", runs: "What happened › Run history",
    "policies.investigate": "What happened › Gateway flow", "policies.pipeline": "Why: policies › Policy explorer", "policies.deployed": "Why: policies › Policy explorer (by document)",
    spend: "What it cost › Budgets & spend", plans: "What it cost › Plans & prices", chargeback: "What it cost › Chargeback",
    "policies.telemetry": "Proof › Logs & telemetry", monitor: "Proof › Azure Monitor" };
  function flash(el) { if (!el) return; el.classList.remove("cop-flash"); void el.offsetWidth; el.classList.add("cop-flash"); setTimeout(() => el.classList.remove("cop-flash"), 1500); }
  function goView(view) {
    if (!view) return null;
    if (view === "scenarios" || view === "builder") { page("setMode")(view === "builder" ? "build" : "guided"); page("collapseLaunch")(false); return byId("launch"); }
    if (view === "conversation") return byId("cvCard");
    // view names predate the four tabs; showTab / showPsec map them (runs -> What happened, chargeback -> What it cost, monitor -> Proof, ...)
    const [tab, sub] = view.split(".");
    page("showTab")(tab); if (sub) page("showPsec")(sub);
    if (tab === "runs" && byId("tab-runs")) byId("tab-runs").open = true;
    const top = typeof window.topTabOf === "function" ? window.topTabOf(sub || tab) : tab;
    flash(q(`.tabs button[data-tab="${top}"]`)); if (sub) flash(q(`.psub button[data-psec="${sub}"]`));
    return sub ? q(`div.psec[data-psec="${sub}"]`) : byId("tab-" + tab);
  }
  // Highlight targets: name -> [view, finder]. The names are listed in app.py's COPILOT_PROMPT; keep both in sync.
  const adv = (el) => { const d = byId("bAdv"); if (d && el && d.contains(el)) d.open = true; return el; };  // expert settings sit behind ⚙ Advanced
  const T = {
    scenarios: ["scenarios", () => byId("scenarios")], builder: ["builder", () => byId("mode-build")], callers: ["builder", () => page("callerMenu")(true) || byId("callerBtn")],
    "surface-picker": ["builder", () => byId("surfacePivot")], "model-select": ["builder", () => byId("model")], prompt: ["builder", () => byId("cvInput")],
    "session-id": ["builder", () => adv(byId("sessionId")?.parentElement)], "end-user": ["builder", () => adv(byId("endUser")?.parentElement)],
    "trace-toggle": ["builder", () => adv(byId("useTrace")?.closest("label"))], "token-toggle": ["builder", () => adv(byId("useToken")?.closest("label"))],
    "send-button": ["builder", () => byId("btnSend")],
    conversation: ["conversation", () => byId("cvCard")],
    "budget-meter": ["conversation", () => { const m = byId("cvMeter"); return m && !m.classList.contains("hidden") ? m : byId("cvCard"); }],
    kpis: ["live", () => byId("kpiBar")], signals: ["live", () => cardOf(byId("sigs"))], "last-response": ["live", () => cardOf(byId("last"))],
    budgets: ["spend", () => { const d = byId("bDetails"); if (d) d.open = true; return d; }], "budget-burn": ["spend", () => cardOf(byId("cBurn"))],
    "outcomes-chart": ["live", () => cardOf(byId("cOutcomes"))], "cost-chart": ["spend", () => cardOf(byId("cCostAgent"))],
    "latency-chart": ["live", () => cardOf(byId("cLatency"))], "live-request-log": ["live", () => cardOf(byId("log"))],
    "run-history": ["runs", () => byId("tab-runs")],
    "chargeback-flow": ["chargeback", () => q("#tab-chargeback > .card")], "chargeback-ledger": ["chargeback", () => cardOf(byId("cbLedger"))],
    "chargeback-monitor": ["chargeback", () => cardOf(byId("cbMonitor"))],
    "plans-matrix": ["plans", () => cardOf(byId("plansMatrix"))], "price-list": ["plans", () => cardOf(byId("priceList"))], endpoints: ["plans", () => cardOf(byId("endpoints"))],
    "request-log": ["policies.investigate", () => byId("evLogCard")], "flow-diagram": ["policies.investigate", () => byId("gwFlow")],
    "policies-at-work": ["policies.investigate", () => { const p = byId("gwfPaw"); return p && !p.hidden ? p : byId("gwFlow"); }],
    "evidence-panel": ["policies.investigate", () => { const p = byId("evPanel"); return p && !p.classList.contains("hidden") ? p : byId("evLogCard"); }],
    pipeline: ["policies.pipeline", () => cardOf(byId("pipe"))], "policy-map": ["policies.pipeline", () => byId("polMap")], "backend-pool": ["policies.pipeline", () => cardOf(byId("backendsKv"))],
    "evidence-sinks": ["policies.telemetry", () => cardOf(byId("sinks"))], "policy-evidence": ["policies.telemetry", () => byId("policyEvidenceCard")],
    "deployed-policies": ["policies.deployed", () => window.pxShowDocs?.()],
    "monitor-cost": ["monitor", () => cardOf(byId("mCostAgent"))], "monitor-outcomes": ["monitor", () => cardOf(byId("mOutcomes"))],
    "monitor-cost-time": ["monitor", () => cardOf(byId("mCostTime"))], "monitor-chargeback": ["monitor", () => cardOf(byId("mChargeback"))],
    "monitor-resources": ["monitor", () => cardOf(byId("mResources"))], "monitor-tokens": ["monitor", () => cardOf(byId("mTokens"))],
    "monitor-governance": ["monitor", () => cardOf(byId("mGov"))],
    "portal-links": [null, () => q(".cmdbar")], "reset-budgets": [null, () => byId("btnReset")], references: [null, () => q('.cmdbar [data-pat="basics"]')],
  };
  const STAGES = { client: 1, identity: 2, plan: 3, routing: 4, metering: 5 };
  // Log Analytics table names (as in the copilot's prompt) -> the sink cards' titles
  const SINK_ALIAS = { apptraces: "App Insights traces", appmetrics: "App Insights customMetrics", apprequests: "App Insights requests",
    custommetrics: "App Insights customMetrics", traces: "App Insights traces", requests: "App Insights requests", headers: "x-gw-* response headers", trace: "Request trace" };
  const same = (el, s) => !!el && el.textContent.trim().toLowerCase() === s.toLowerCase();
  function planColumn(tier) {
    const table = q("#plansMatrix table"); if (!table) return null;
    const ths = [...table.querySelectorAll("thead th")];
    const idx = ths.findIndex((th) => th.querySelector(`.badge.${CSS.escape(tier.toLowerCase())}`) || same(th, tier));
    if (idx < 1) return null;
    const cells = [...table.querySelectorAll("tbody tr")].map((tr) => tr.children[idx]).filter(Boolean), last = cells[cells.length - 1] || ths[idx];
    return { th: ths[idx], tint: () => [ths[idx], ...cells].forEach((c) => c.classList.add("cop-col")),
      rect: () => { const a = ths[idx].getBoundingClientRect(), z = last.getBoundingClientRect(); return new DOMRect(a.left, a.top, a.width, z.bottom - a.top); } };
  }
  function resolve(target) {
    const i = target.indexOf(":"), k = i < 0 ? target : target.slice(0, i), arg = i < 0 ? "" : target.slice(i + 1).trim();
    if (!arg) return T[k] ? { view: T[k][0], find: T[k][1] } : null;
    const all = (sel) => [...document.querySelectorAll(sel)];
    switch (k) {
      case "scenario": return { view: "scenarios", find: () => window.scFocus?.((parseInt(arg, 10) || 0) - 1) ?? q(`.scenarios .scrow[data-i="${(parseInt(arg, 10) || 0) - 1}"]`) };
      case "caller": return { view: "builder", find: () => { page("callerMenu")(true); return all(".agent[data-agent]").find((e) => e.dataset.agent === arg); } };
      case "plan": return { view: "plans", find: () => planColumn(arg)?.th, rect: () => planColumn(arg)?.rect(), prep: () => planColumn(arg)?.tint() };
      case "flow": return { view: "policies.investigate", find: () => q(`#gwfSvg g[data-node="${CSS.escape(arg)}"]`) };
      case "stage": return { view: "policies.pipeline", find: () => (STAGES[arg] ? q(`#pipe .stage:nth-child(${STAGES[arg]})`) : null) };
      case "sink": {
        const name = (SINK_ALIAS[arg.toLowerCase()] || arg).toLowerCase();
        return { view: "policies.telemetry", fallback: "evidence-sinks", find: () => all("#sinks .sink").find((e) => (e.querySelector(".t")?.firstChild?.textContent || "").trim().toLowerCase().startsWith(name)) };
      }
      case "deployed": return { view: "policies.deployed", fallback: "deployed-policies", find: () => window.pxFindDoc?.(arg) };
      case "session": return { view: "chargeback", fallback: "chargeback-monitor", find: () => q(`#cbMonitor tr[data-session="${CSS.escape(arg)}"]`) };
      case "policy": return { view: "policies.pipeline", fallback: "pipeline", find: () => window.pxFindPolicy?.(arg) };  // a policy element: its control in the Policy explorer
    }
    return null;
  }
  const shown = (el) => (el && el.getClientRects().length ? el : null);
  async function waitFor(get, ms) { const end = Date.now() + ms; for (;;) { const v = get(); if (v || Date.now() > end) return v; await sleep(150); } }
  async function highlight(target, caption, n) {
    let t = resolve(String(target || ""));
    if (!t) return false;
    goView(t.view);
    let el = await waitFor(() => shown(t.find()), 8000);  // deployed policies and charts may still be loading
    if (!el && t.fallback) { t = resolve(t.fallback); el = await waitFor(() => shown(t.find()), 3000); }
    if (!el) return false;
    t.prep?.();
    const r = t.rect?.() || el.getBoundingClientRect(), tall = r.height > innerHeight * 0.6;
    const margin = el.style.scrollMarginTop;
    el.style.scrollMarginTop = "64px";  // leaves room above the element for its caption
    el.scrollIntoView({ behavior: smooth(), block: tall ? "start" : "center", inline: "nearest" });
    el.style.scrollMarginTop = margin;
    addSpot(el, caption, n, t.rect);
    setTimeout(dodge, 500);
    return true;
  }
  // On screens where the open chat overlays the page, move it to the other side when it would hide a spotlight
  function dodge() {
    if (panel.hidden || !S.spots.length) { panel.classList.remove("cop-left"); return; }
    const w = panel.offsetWidth + 16;
    const covered = (from, to) => S.spots.reduce((sum, sp) => {
      const r = (sp.rect && sp.rect()) || sp.el.getBoundingClientRect();
      return sum + Math.max(0, Math.min(r.right, to) - Math.max(r.left, from)) * Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
    }, 0);
    const right = covered(innerWidth - w, innerWidth), left = covered(0, w);
    panel.classList.toggle("cop-left", right > 0 && left < right * 0.6);
  }
  function addChip(b, label, onClick, failed = false, target = "") {
    const row = b.el.querySelector(".cop-shown"); row.hidden = false;
    const c = document.createElement("button"); c.type = "button"; c.className = "cop-chip" + (failed ? " off" : ""); c.textContent = label;
    c.title = failed ? `Couldn't find ${target || "this"} on the page` : "Show it again"; c.disabled = failed; c.onclick = onClick;
    const clr = row.querySelector(".clr");
    row.insertBefore(c, clr);
    if (!clr && !failed) {
      const x = document.createElement("button"); x.type = "button"; x.className = "cop-chip clr"; x.textContent = "✕ clear"; x.title = "Remove the highlights (Esc)";
      x.onclick = clearSpots; row.appendChild(x);
    }
  }
  function openCall(n) {
    const r = pageCalls()[n - 1];
    if (!r || typeof window.showEvidence !== "function") return false;
    window.showEvidence(n - 1);  // replays it in the flow and loads its trace and log rows
    return true;
  }
  async function act(a, b) {
    if (a.type === "navigate") {
      goView(a.view)?.scrollIntoView({ behavior: smooth(), block: "start" });
      addChip(b, VIEW_NAMES[a.view] || a.view, () => goView(a.view)?.scrollIntoView({ behavior: smooth(), block: "start" }));
      await sleep(400);
    } else if (a.type === "highlight") {
      const n = ++b.shown, label = `${circled(n)} ${a.caption || a.target}`;
      const ok = await highlight(a.target, a.caption, n);
      addChip(b, label, () => { clearSpots(); highlight(a.target, a.caption, n); }, !ok, a.target);
      await sleep(ok ? 700 : 0);
    } else if (a.type === "open_request") {
      const ok = openCall(a.call);
      addChip(b, `Evidence of #${a.call}`, () => openCall(a.call), !ok);
      await sleep(900);
    } else if (a.type === "play_flow") {
      goView("policies.investigate"); await sleep(60);
      byId("gwFlow")?.scrollIntoView({ behavior: smooth(), block: "center" });
      window.gwFlow?.play(a.example);
      addChip(b, `▶ Flow: ${a.example}`, () => { goView("policies.investigate"); byId("gwFlow")?.scrollIntoView({ behavior: smooth(), block: "center" }); window.gwFlow?.play(a.example); });
      await sleep(600);
    } else if (a.type === "policy_flow") {
      renderFlow(b, a);
    } else if (a.type === "prepare_request") {
      prepare(a);
      const n = ++b.shown;
      const ok = await highlight("send-button", "Ready: press Send to try it", n);
      addChip(b, `${circled(n)} Request prepared`, () => { prepare(a); clearSpots(); highlight("send-button", "Ready: press Send to try it", n); }, !ok);
    } else if (a.type === "open_reference") {
      const open = () => { if (typeof window.gwFlow?.pattern !== "function") return false; window.gwFlow.pattern(a.topic, a.index || 0); return true; };
      const ok = open();
      addChip(b, `📖 ${REF_NAMES[a.topic] || a.topic}`, open, !ok, "the References");
      await sleep(ok ? 500 : 0);
    }
    ctxLine(); scrollDown();
  }
  const REF_NAMES = { basics: "APIM basics", overview: "Surfaces", identity: "Identity", plan: "Plan enforcement", routing: "Routing & resilience",
    metering: "Metering & logging", caching: "Caching", safety: "Content safety" };
  function prepare(a) {
    page("setMode")("build"); page("collapseLaunch")(false);
    page("selectAgent")(a.caller); page("setSurface")(a.surface);
    if (a.surface === "model" && a.model) byId("model").value = a.model;
    if (a.surface === "tool" && a.tool) { byId("tool").value = a.tool; byId("toolArgs").value = JSON.stringify((typeof TOOL_ARGS !== "undefined" && TOOL_ARGS[a.tool]) || {}); }
    if (a.prompt) { byId(a.surface === "agent" ? "agentTask" : "prompt").value = a.prompt; if (byId("cvInput")) byId("cvInput").value = a.prompt; }
    page("cvCtx")();
  }

  // ---------- A policy (or a call) drawn as a step-by-step flow, each step linked to the page ----------
  const OUTCOME = { 401: "bad", 403: "bad", 429: "warn", downgrade: "warn", cap: "warn", 200: "ok" };
  function renderFlow(b, a) {
    const box = document.createElement("div"); box.className = "cop-flow";
    let html = `<div class="cop-flow-h"><b>${esc(a.title || "Policy flow")}</b>${a.scope ? `<span class="cop-scope">${esc(a.scope)}</span>` : ""}</div><ol>`, sec = "";
    a.steps.forEach((s, i) => {
      if (s.section !== sec) { sec = s.section; html += `<li class="cop-sec ${esc(sec)}">${esc(sec)}</li>`; }
      const out = String(s.outcome || "").trim(), cls = OUTCOME[out.toLowerCase()] || "";
      html += `<li class="cop-step ${esc(s.section)}" tabindex="0" data-policy="${esc(s.policy)}" data-does="${esc(s.does)}" title="Show ${esc(s.policy)} on the page">`
        + `<span class="cop-num">${i + 1}</span><div><code>${esc(s.policy)}</code>${out ? `<span class="cop-out ${cls}">${esc(out)}</span>` : ""}<small>${esc(s.does)}</small></div></li>`;
    });
    box.innerHTML = html + "</ol>";
    const go = (e) => { const st = e.target.closest(".cop-step"); if (!st) return; clearSpots(); highlight("policy:" + st.dataset.policy, st.dataset.does, "•"); };
    box.addEventListener("click", go);
    box.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(e); } });
    b.el.querySelector(".cop-vis").appendChild(box);
    b.flows = (b.flows || 0) + 1;
  }
  // A flow drawn only in the chat still gets a pointer to where it runs on the page, without another model call
  async function pointAtPipeline(b) {
    if (b.shown || !b.flows) return;
    const n = ++b.shown, caption = "Where these steps run: click a step in the chat to find it";
    const ok = await highlight("pipeline", caption, n);
    if (ok) addChip(b, `${circled(n)} Policy explorer`, () => { clearSpots(); highlight("pipeline", caption, n); });
  }

  // ---------- Spotlights: outline + numbered caption over a page element, kept in place while the page scrolls ----------
  function addSpot(el, caption, n, rect) {
    const box = document.createElement("div"); box.className = "cop-spot";
    const cap = document.createElement("div"); cap.className = "cop-cap"; cap.innerHTML = `<b>${circled(n)}</b>${esc(caption || "")}`;
    if (!caption) cap.hidden = true;
    document.body.append(box, cap);
    S.spots.push({ el, box, cap, rect });
    if (!S.raf) S.raf = requestAnimationFrame(place);
  }
  function place() {
    S.raf = 0;
    const lb = byId("gwLb"), covered = !!lb && !lb.hidden;  // spotlights wait behind the References lightbox and come back when it closes
    for (const sp of S.spots) {
      const r = (sp.rect && sp.rect()) || sp.el.getBoundingClientRect(), on = !covered && sp.el.isConnected && (r.width > 0 || r.height > 0) && !!shown(sp.el);
      sp.box.style.display = on ? "" : "none"; sp.cap.style.display = on && !sp.cap.hidden ? "" : "none";
      if (!on) continue;
      Object.assign(sp.box.style, { left: `${r.left - 6}px`, top: `${r.top - 6}px`, width: `${r.width + 12}px`, height: `${r.height + 12}px` });
      const ch = sp.cap.offsetHeight, cw = sp.cap.offsetWidth;
      let top = r.bottom + 12;
      if (top + ch > innerHeight - 8) top = r.top - 12 - ch;
      if (top < 8) top = Math.min(innerHeight - ch - 8, Math.max(r.top, 8) + 10);
      sp.cap.style.left = `${Math.max(8, Math.min(r.left, innerWidth - cw - 8))}px`; sp.cap.style.top = `${top}px`;
    }
    if (S.spots.length) S.raf = requestAnimationFrame(place);
  }
  function clearSpots() {
    cancelAnimationFrame(S.raf); S.raf = 0;
    S.spots.forEach((sp) => { sp.box.remove(); sp.cap.remove(); }); S.spots = [];
    document.querySelectorAll(".cop-col").forEach((c) => c.classList.remove("cop-col"));
    panel.classList.remove("cop-left");
  }

  // ---------- Just enough markdown for the answers (escaped first): paragraphs, lists, headings, code, bold, https links, #n call links ----------
  function md(text) {
    const out = []; let list = null, fence = null;
    const inline = (s) => s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(>])#(\d{1,4})\b/g, '$1<a class="cop-call" data-call="$2" title="Open the evidence of call #$2">#$2</a>');
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    const unesc = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
    const fencePre = (lines) => { const raw = unesc(lines.join("\n")); return /^\s*</.test(raw) && typeof xmlPre === "function" ? xmlPre(raw, "xmlhl") : `<pre>${lines.join("\n")}</pre>`; };
    const cells = (s) => s.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    let table = null;
    const flush = () => { if (table) { out.push(`<div class="cop-tbl"><table>${table.join("")}</tbody></table></div>`); table = null; } };
    for (const raw of esc(text).split("\n")) {
      if (/^\s*```/.test(raw)) { flush(); if (fence) { out.push(fencePre(fence)); fence = null; } else { close(); fence = []; } continue; }
      if (fence) { fence.push(raw); continue; }
      if (/^\s*\|.*\|\s*$/.test(raw)) {  // markdown table: header row, |---| separator, body rows
        close();
        if (/^\s*\|[\s:|-]+\|\s*$/.test(raw)) continue;
        if (!table) table = [`<thead><tr>${cells(raw).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>`];
        else table.push(`<tr>${cells(raw).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`);
        continue;
      }
      flush();
      const li = raw.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/);
      if (li) { const tag = li[1] ? "ol" : "ul"; if (list !== tag) { close(); out.push(`<${tag}>`); list = tag; } out.push(`<li>${inline(li[2])}</li>`); continue; }
      close();
      const h = raw.match(/^\s*#{1,4}\s+(.*)$/);
      if (h) out.push(`<div class="cop-h">${inline(h[1])}</div>`); else if (raw.trim()) out.push(`<p>${inline(raw)}</p>`);
    }
    if (fence) out.push(fencePre(fence));
    flush(); close();
    return out.join("");
  }

  // ---------- Wiring ----------
  function autosize() { input.style.height = "auto"; input.style.height = `${Math.min(140, input.scrollHeight + 2)}px`; }
  fab.onclick = () => open(true);
  byId("copClose").onclick = () => open(false);
  byId("copNew").onclick = () => { if (S.busy) S.ctrl?.abort(); S.msgs = []; clearSpots(); chat.innerHTML = ""; intro(); ctxLine(); input.focus(); };
  byId("copForm").onsubmit = (e) => { e.preventDefault(); if (S.busy) S.ctrl?.abort(); else ask(input.value); };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!S.busy) ask(input.value); } });
  input.addEventListener("input", autosize);
  chat.addEventListener("click", (e) => {
    const sug = e.target.closest(".cop-sug"); if (sug) return ask(sug.textContent);
    const call = e.target.closest("a.cop-call"); if (call) { e.preventDefault(); if (!openCall(+call.dataset.call)) call.title = "That call isn't on this page"; }
  });
  panel.addEventListener("focusin", ctxLine);
  document.addEventListener("click", () => { if (!panel.hidden) setTimeout(ctxLine, 80); }, true);  // the user moved around the page
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (S.spots.length) clearSpots(); else if (!panel.hidden && panel.contains(document.activeElement)) open(false);
  });
  window.sandboxCopilot = { open, ask, highlight: (target, caption) => { clearSpots(); return highlight(target, caption, 1); }, clear: clearSpots,
    // Runs one UI action as if the agent had sent it (presenter scripts and UI checks): e.g. { type: "open_reference", topic: "routing" }
    act: async (a) => { const b = addBot(); b.sum.parentElement.hidden = true; await act(a, b); return !b.el.querySelector(".cop-chip.off"); },
    targets: () => Object.keys(T) };
})();
