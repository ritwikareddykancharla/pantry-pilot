/* Pantry Pilot coordinator console. Vanilla JS; polls /api/state every 4 s (every 2 s while a cycle runs). */
(function () {
  "use strict";

  const POLL_MS = 4000;
  const POLL_FAST_MS = 2000;
  const AGENT_LABEL = { dispatcher: "Dispatcher", roster: "Roster", steward: "Steward" };
  let runningSince = null;
  let pollTimer = null;
  let refreshing = false;
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const fmtTs = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const fmtTime = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };

  let state = null;
  let busy = false;

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
    return data;
  }

  async function refresh() {
    if (refreshing) return; // a slow /api/state (the runtime is busy) must not stack up requests
    refreshing = true;
    try {
      state = await api("/api/state");
      render();
    } catch (err) {
      showStatus("Cannot reach the server: " + err.message, true);
    } finally {
      refreshing = false;
      clearTimeout(pollTimer);
      const active = state && state.runner && state.runner.running;
      pollTimer = setTimeout(refresh, active ? POLL_FAST_MS : POLL_MS);
    }
  }

  function showStatus(text, isError) {
    const strip = $("#status-strip");
    strip.innerHTML = "";
    if (!text) { strip.hidden = true; return; }
    strip.hidden = false;
    strip.className = "status-strip" + (isError ? " error" : "");
    if (!isError) strip.appendChild(el("span", "dot"));
    strip.appendChild(el("span", null, text));
  }

  // While a cycle runs, the strip shows which agent is working, for how long, and its last few steps.
  function showLive(label, progress) {
    const strip = $("#status-strip");
    strip.innerHTML = "";
    strip.hidden = false;
    strip.className = "status-strip live";
    const lines = (progress || []).slice(-5);
    const last = lines[lines.length - 1];
    const head = el("div", "live-head");
    head.appendChild(el("span", "dot"));
    const who = last ? `${AGENT_LABEL[last.agent] || last.agent} is working` : "starting the swarm";
    head.appendChild(el("span", "live-title", `Agents are working on the ${label}: ${who}.`));
    const elapsed = el("span", "live-elapsed");
    elapsed.id = "live-elapsed";
    head.appendChild(elapsed);
    strip.appendChild(head);
    const list = el("ol", "live-lines");
    lines
      .filter((p, i) => p.kind !== "thinking" || i === lines.length - 1)
      .forEach((p) => {
        const li = el("li", p.kind);
        li.appendChild(el("span", "who", AGENT_LABEL[p.agent] || p.agent));
        li.appendChild(el("span", null, p.text.length > 180 ? p.text.slice(0, 177) + "..." : p.text));
        list.appendChild(li);
      });
    strip.appendChild(list);
    tickElapsed();
  }

  function tickElapsed() {
    const e = document.getElementById("live-elapsed");
    if (!e) return;
    if (runningSince == null) { e.textContent = ""; return; }
    const s = Math.max(0, Math.round((Date.now() - runningSince) / 1000));
    e.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  // ------------------------------------------------------------------ render
  function render() {
    const s = state;
    $("#org-name").textContent = (s.org && s.org.name) || "Coordinator console";
    $("#today").textContent = `${s.weekday}, ${s.today}`;
    const last = s.last_cycle;
    $("#last-run").textContent = last
      ? `Last cycle ${last.id} (${last.kind}) at ${fmtTs(last.finished_at || last.started_at)}`
      : "Last cycle: never";
    const running = s.runner && s.runner.running;
    $("#btn-sweep").disabled = !!running;
    $("#btn-sweep").textContent = running ? "Running..." : "Run daily cycle";
    runningSince = running ? Date.now() - ((s.runner && s.runner.running_for_seconds) || 0) * 1000 : null;
    if (running) showLive(running, s.progress);
    else if (s.runner && s.runner.last_error) showStatus(s.runner.last_error, true);
    else showStatus(null);

    renderDecisions(s.decisions.pending);
    renderBrief(s.last_report);
    renderBoard(s.shifts, s.today);
    renderInventory(s.inventory);
    renderMessages(s.messages, s.people);
    renderActivity(s.audit, s.last_cycle);
  }

  function renderDecisions(pending) {
    const box = $("#decisions");
    box.innerHTML = "";
    const count = $("#needs-count");
    count.textContent = pending.length;
    count.className = "count" + (pending.length ? "" : " zero");
    if (!pending.length) {
      box.appendChild(el("div", "empty-state", "Nothing needs you right now. The agent handled the routine."));
      return;
    }
    pending.forEach((d) => {
      const card = el("div", "card " + d.kind);
      const meta = el("div", "meta");
      meta.appendChild(el("span", null, d.id));
      meta.appendChild(el("span", null, d.kind === "approval" ? "approval needed" : "decision needed"));
      meta.appendChild(el("span", null, `raised by ${d.created_by || "agent"} at ${fmtTs(d.created_at)}`));
      card.appendChild(meta);
      card.appendChild(el("div", "summary", d.summary));

      const payload = d.payload || {};
      if (payload.tool === "send_broadcast") {
        card.appendChild(el("div", "preview", payload.preview || (payload.input && payload.input.body) || ""));
      }
      if (payload.tool === "place_supply_order" && Array.isArray(payload.table)) {
        const t = el("table", "po");
        const head = el("tr");
        ["Item", "Qty", "Unit", "Unit cost"].forEach((h) => head.appendChild(el("th", null, h)));
        t.appendChild(head);
        payload.table.forEach((row) => {
          const tr = el("tr");
          [row.item, row.qty, row.unit, row.unit_cost !== undefined ? `$${row.unit_cost}` : ""].forEach((v) => tr.appendChild(el("td", null, v)));
          t.appendChild(tr);
        });
        card.appendChild(t);
        card.appendChild(el("div", "meta", `Vendor: ${payload.input.vendor}. Estimated total: $${payload.input.est_cost}`));
      }
      if (d.recommendation) card.appendChild(el("div", "rec", "Recommendation: " + d.recommendation));

      const opts = el("div", "options");
      if (d.kind === "approval") {
        const ok = el("button", "btn option recommended", d.options[0] || "Approve");
        ok.onclick = () => answer(d.id, "yes");
        const no = el("button", "btn danger", d.options[1] || "Decline");
        no.onclick = () => answer(d.id, "no");
        opts.appendChild(ok);
        opts.appendChild(no);
      } else {
        (d.options || []).forEach((opt, i) => {
          const rec = d.recommendation && d.recommendation.toLowerCase().includes(opt.toLowerCase());
          const b = el("button", "btn option" + (rec ? " recommended" : ""), opt);
          b.onclick = () => answer(d.id, String(i));
          opts.appendChild(b);
        });
      }
      card.appendChild(opts);

      const reply = el("div", "reply");
      const input = el("input");
      input.placeholder = d.kind === "approval" ? "Edit the text, then Approve with edits" : "Or reply in your own words";
      const send = el("button", "btn small", d.kind === "approval" ? "Approve with edits" : "Reply");
      send.onclick = () => {
        if (!input.value.trim()) return;
        if (d.kind === "approval") answer(d.id, "yes", { body: input.value.trim() });
        else answer(d.id, input.value.trim());
      };
      reply.appendChild(input);
      reply.appendChild(send);
      card.appendChild(reply);
      box.appendChild(card);
    });
  }

  async function answer(id, response, edits) {
    if (busy) return;
    busy = true;
    try {
      const out = await api(`/api/decisions/${id}`, { method: "POST", body: JSON.stringify({ response, edits: edits || {} }) });
      if (out.follow_up) showStatus("Your answer was queued for the agents; they are carrying it out now.");
      await refresh();
    } catch (err) {
      showStatus("Could not record decision: " + err.message, true);
    } finally {
      busy = false;
    }
  }

  function renderBrief(report) {
    const box = $("#brief");
    box.innerHTML = "";
    if (!report) {
      $("#brief-when").textContent = "";
      box.appendChild(el("div", "empty-state", "Run the daily cycle to generate this week's brief."));
      return;
    }
    $("#brief-when").textContent = `${report.period_start} to ${report.period_end}`;
    const kpis = el("div", "kpis");
    [
      [`${report.coverage_pct}%`, "coverage next 7 days"],
      [report.open_slots.length, "open slots"],
      [report.escalations_pending, "waiting on you"],
      [report.items_below_par.length, "items below par"],
      [report.expiring_soon.length, "expiring soon"],
      [report.volunteer_hours_logged, "hours logged"],
    ].forEach(([v, label]) => {
      const k = el("div", "kpi");
      k.appendChild(el("b", null, String(v)));
      k.appendChild(el("span", null, label));
      kpis.appendChild(k);
    });
    box.appendChild(kpis);
    if (report.summary) box.appendChild(el("p", null, report.summary));
    const lists = [
      ["Open slots", report.open_slots],
      ["Donations", report.donations_received],
      ["Thank you", report.thank_you],
      ["For you", report.coordinator_actions],
    ];
    lists.forEach(([title, items]) => {
      if (!items || !items.length) return;
      box.appendChild(el("h3", null, title));
      const ul = el("ul");
      items.forEach((i) => ul.appendChild(el("li", null, i)));
      box.appendChild(ul);
    });
  }

  function renderBoard(shifts, today) {
    const board = $("#board");
    board.innerHTML = "";
    const start = new Date(today + "T00:00:00");
    for (let i = 0; i < 7; i++) {
      const d = new Date(start);
      d.setDate(start.getDate() + i);
      const iso = d.toISOString().slice(0, 10);
      const day = el("div", "day" + (i === 0 ? " today" : ""));
      const head = el("div", "day-head");
      head.appendChild(el("b", null, d.toLocaleDateString(undefined, { weekday: "short" })));
      head.appendChild(document.createTextNode(d.toLocaleDateString(undefined, { month: "short", day: "numeric" })));
      day.appendChild(head);
      shifts.filter((s) => s.date === iso).forEach((s) => {
        const status = s.filled === 0 ? "empty" : s.open > 0 ? "short" : "full";
        const slot = el("div", "slot " + status);
        const title = el("div", "slot-title");
        title.appendChild(el("span", null, s.title));
        title.appendChild(el("span", null, `${s.filled}/${s.needed}`));
        slot.appendChild(title);
        slot.appendChild(el("div", "slot-time", `${s.start}-${s.end} · ${s.role}`));
        const names = el("div", "names");
        s.roster.forEach((r) => names.appendChild(el("span", "name " + r.status, r.name.split(" ")[0])));
        if (s.open > 0) names.appendChild(el("span", "name", `+${s.open} needed`));
        slot.appendChild(names);
        const flags = [];
        if (s.minors_on_roster.length) flags.push(s.supervisor_confirmed ? "minor + supervisor ok" : "minor, no supervisor yet");
        if (s.ask_rounds) flags.push(`${s.ask_rounds} ask round(s)`);
        if (s.reminder_sent) flags.push("reminded");
        if (flags.length) slot.appendChild(el("div", "flags", flags.join(" · ")));
        day.appendChild(slot);
      });
      board.appendChild(day);
    }
  }

  function renderInventory(items) {
    const box = $("#inventory");
    box.innerHTML = "";
    const sorted = [...items].sort((a, b) => a.qty / a.par - b.qty / b.par);
    sorted.forEach((it) => {
      const row = el("div", "inv" + (it.below_par ? " low" : ""));
      row.appendChild(el("span", null, it.item));
      const bar = el("div", "bar");
      const fill = el("div");
      fill.style.width = Math.min(100, Math.round((100 * it.qty) / Math.max(1, it.par))) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("span", "qty", `${it.qty}/${it.par} ${it.unit}`));
      box.appendChild(row);
    });
    const exp = $("#expiring");
    exp.innerHTML = "";
    items
      .filter((i) => i.days_to_expiry !== null && i.days_to_expiry !== undefined && i.days_to_expiry <= 7)
      .sort((a, b) => a.days_to_expiry - b.days_to_expiry)
      .forEach((i) => {
        const li = el("li");
        li.appendChild(el("span", null, `${i.item} (${i.qty} ${i.unit})`));
        li.appendChild(el("span", "days", i.days_to_expiry <= 0 ? "today" : `${i.days_to_expiry} day${i.days_to_expiry === 1 ? "" : "s"}`));
        exp.appendChild(li);
      });
    if (!exp.children.length) exp.appendChild(el("li", null, "Nothing expiring this week."));
  }

  function renderMessages(messages, people) {
    const sel = $("#inbound-from");
    if (sel.options.length === 0 && people) {
      people.forEach((p) => {
        const o = el("option", null, `${p.name} (${p.type})`);
        o.value = p.id;
        sel.appendChild(o);
      });
      const c = el("option", null, "Aisha Bello (coordinator)");
      c.value = "coordinator";
      sel.appendChild(c);
    }
    const inbound = messages.inbound.slice().reverse();
    const outbound = messages.outbound.slice().reverse();
    $("#msg-counts").textContent = `${inbound.filter((m) => !m.handled).length} unhandled · ${outbound.length} sent`;
    const inBox = $("#inbound");
    inBox.innerHTML = "";
    inbound.forEach((m) => {
      const cls = "msg" + (m.handled ? "" : " unhandled") + (m.from_id === "coordinator" ? " coordinator" : "");
      const n = el("div", cls);
      const who = el("div", "who");
      who.appendChild(el("span", null, `${m.from_name || m.from_id} · ${m.channel}`));
      who.appendChild(el("span", null, fmtTs(m.ts)));
      n.appendChild(who);
      n.appendChild(el("div", "body", m.body));
      if (m.handled && m.handled_note) n.appendChild(el("div", "note", "Handled: " + m.handled_note));
      else if (!m.handled) n.appendChild(el("div", "note", "Waiting for the next cycle"));
      inBox.appendChild(n);
    });
    if (!inbound.length) inBox.appendChild(el("div", "empty-state", "No inbound messages."));
    const outBox = $("#outbound");
    outBox.innerHTML = "";
    outbound.forEach((m) => {
      const n = el("div", "msg" + (m.kind === "draft" ? " draft" : ""));
      const who = el("div", "who");
      const status = (m.meta && m.meta.status) || "sent";
      who.appendChild(el("span", null, `to ${m.to_name} · ${m.kind}${status !== "sent" ? " · " + status.replace(/_/g, " ") : ""}`));
      who.appendChild(el("span", null, fmtTs(m.ts)));
      n.appendChild(who);
      n.appendChild(el("div", "body", m.body));
      outBox.appendChild(n);
    });
    if (!outbound.length) outBox.appendChild(el("div", "empty-state", "Nothing sent yet."));
  }

  function renderActivity(audit, lastCycle) {
    const trail = $("#trail");
    trail.innerHTML = "";
    const nodes = (lastCycle && lastCycle.handoff_trail) || [];
    if (nodes.length) {
      trail.appendChild(el("span", "hint", "Handoff trail: "));
      nodes.forEach((n, i) => {
        if (i) trail.appendChild(el("span", "arrow", "→"));
        trail.appendChild(el("span", "node " + n, n));
      });
    } else {
      trail.appendChild(el("span", "hint", "No cycle has run yet."));
    }
    const box = $("#activity");
    box.innerHTML = "";
    audit
      .filter((a) => a.kind !== "trail")
      .slice()
      .reverse()
      .forEach((a) => {
        const row = el("div", "act " + a.kind);
        row.appendChild(el("span", "ts", fmtTime(a.ts)));
        row.appendChild(el("span", "agent " + (a.agent || ""), a.agent));
        let toolText = a.tool;
        if (a.tool === "handoff_to_agent" && a.input && a.input.agent_name) toolText = `handoff → ${a.input.agent_name}`;
        row.appendChild(el("span", "tool", toolText));
        const detail = a.tool === "handoff_to_agent" ? a.input.message || "" : summarizeInput(a.input) + (a.result ? "  ⇒ " + a.result : "");
        const dn = el("span", "detail", detail);
        dn.title = detail;
        row.appendChild(dn);
        box.appendChild(row);
      });
    if (!box.children.length) box.appendChild(el("div", "empty-state", "The activity feed fills in after the first cycle."));
  }

  function summarizeInput(input) {
    if (!input || typeof input !== "object") return "";
    return Object.entries(input)
      .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
      .join(", ")
      .slice(0, 160);
  }

  // ------------------------------------------------------------------ actions
  $("#btn-sweep").onclick = async () => {
    try {
      await api("/api/sweep", { method: "POST", body: "{}" });
      showStatus("Daily cycle started: dispatcher is reading the inbox.");
      setTimeout(refresh, 800);
    } catch (err) {
      showStatus("Could not start the cycle: " + err.message, true);
    }
  };

  $("#inbound-form").onsubmit = async (e) => {
    e.preventDefault();
    const text = $("#inbound-text").value.trim();
    if (!text) return;
    try {
      await api("/api/inbound", { method: "POST", body: JSON.stringify({ from_id: $("#inbound-from").value, text }) });
      $("#inbound-text").value = "";
      showStatus("Text received; the swarm is handling it now.");
      setTimeout(refresh, 800);
    } catch (err) {
      showStatus("Could not send: " + err.message, true);
    }
  };

  $("#ask-form").onsubmit = async (e) => {
    e.preventDefault();
    const prompt = $("#ask-input").value.trim();
    if (!prompt) return;
    const out = $("#ask-answer");
    out.hidden = false;
    out.textContent = "Thinking...";
    try {
      const data = await api("/api/ask", { method: "POST", body: JSON.stringify({ prompt }) });
      out.textContent = data.answer || JSON.stringify(data);
    } catch (err) {
      out.textContent = "Could not answer: " + err.message;
    }
  };

  refresh();
  setInterval(tickElapsed, 1000);
})();
