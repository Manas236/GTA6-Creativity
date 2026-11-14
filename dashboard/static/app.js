"use strict";
let state, detail, pendingAction, confirmation, refreshTimer;
let connectionRows = [], connectionsBusy = false;
const content = document.querySelector("#content");
const dialog = document.querySelector("#action-dialog");
const colors = {
  instagram: "#f49abb",
  facebook: "#85b9ff",
  A: "#b2a1ff",
  B: "#70d8b2",
};
const labels = {
  approve: "Approve both variants",
  aonly: "Approve A only",
  reject: "Reject item",
  hookok: "Hook is right",
  edit_hook: "Edit hook",
  reslot: "Re-slot item",
  retry: "Retry stage",
  pause: "Pause all posting",
  resume: "Resume posting",
};
const filters = { queue: "", stage: "all", failures: "all" };
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const number = (value) => Number(value || 0).toLocaleString();
const compact = (value) =>
  new Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value || 0);
const pretty = (value) => String(value || "unknown").replaceAll("_", " ");
const itemLink = (id) => "/items/" + encodeURIComponent(id);
const itemById = (id) => state.items.find((i) => i.id === id);
const formatTime = (value, options = {}) =>
  value
    ? new Intl.DateTimeFormat("en-GB", {
        timeZone: state.timezone,
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        ...options,
      }).format(new Date(value))
    : "Not scheduled";
const dayKey = (value) =>
  value
    ? new Intl.DateTimeFormat("en-CA", {
        timeZone: state.timezone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date(value))
    : "unscheduled";
function pill(text, tone = "") {
  return `<span class="pill ${tone}">${escape(text)}</span>`;
}
function button(action, id = "", text = labels[action]) {
  return `<button class="button ${action === "reject" ? "danger" : action === "approve" ? "primary" : "secondary"}" data-action="${action}" data-id="${escape(id)}">${escape(text)}</button>`;
}
function heading(title, subtitle, controls = "") {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${escape(subtitle)}</p></div>${controls}</div>`;
}
function metric(label, value, caption, tone = "") {
  return `<div class="metric"><div class="eyebrow">${label}</div><div class="value ${tone}">${value}</div><div class="caption">${escape(caption)}</div></div>`;
}
function panelHead(title, sub = "", extra = "") {
  return `<div class="panel-head"><div><h2>${title}</h2>${sub ? `<p>${escape(sub)}</p>` : ""}</div>${extra}</div>`;
}
function empty(message) {
  return `<div class="empty">${escape(message)}</div>`;
}
function totals() {
  const latest = new Map();
  for (const m of state.metrics) {
    const previous = latest.get(m.post_id);
    if (!previous || new Date(m.fetched_at) > new Date(previous.fetched_at))
      latest.set(m.post_id, m);
  }
  return [...latest.values()];
}
function queueItems() {
  return state.items
    .filter((i) => ["approved", "queued"].includes(i.stage))
    .map((item) => ({
      item,
      posts: state.posts.filter((p) => p.item_id === item.id),
    }))
    .sort((a, b) =>
      (a.posts[0]?.slot_datetime || "z").localeCompare(
        b.posts[0]?.slot_datetime || "z",
      ),
    );
}
function commandsPanel() {
  if (!state.commands.length) return "";
  return `<section class="panel commands">${panelHead("Recent controls", "Commands apply at the runner’s next stage boundary.")}${state.commands
    .slice(0, 6)
    .map(
      (c) =>
        `<div class="command-row">${pill(c.status, c.status === "applied" ? "green" : c.status === "pending" ? "amber" : "red")}<div class="grow"><strong>${escape(labels[c.action] || c.action)}</strong><p>${escape(c.result || "Waiting for the runner. Start or resume its loop to apply this command.")}</p></div><code>${escape(formatTime(c.created_at))}</code></div>`,
    )
    .join("")}</section>`;
}
function overview() {
  const next = queueItems()
      .filter((q) => q.posts.length)
      .slice(0, 5),
    latest = totals();
  const live = state.posts.filter((p) => p.status === "live").length;
  const review = state.items.filter((i) =>
    ["needs_review", "awaiting_approval", "rendered"].includes(i.stage),
  ).length;
  const max = Math.max(1, ...state.stages.map((s) => s.count));
  const runtime = state.runtime,
    lock = state.lock;
  const quotaBlocked =
    runtime.gemini_blocked_until &&
    new Date(runtime.gemini_blocked_until) > new Date();
  const quotaKnown = state.runtime_known;
  const quotaText = quotaKnown
    ? quotaBlocked
      ? "Cooldown"
      : "No cooldown reported"
    : "Unknown";
  const cooldownMinutes = Math.max(1, Math.ceil(
    (new Date(state.meta_cooldown_until) - new Date(state.at)) / 60000,
  ));
  const cooldown = state.meta_cooldown_until
    ? `Meta cooldown · ${cooldownMinutes} min remaining · ${state.meta_cooldown_reason}`
    : "";
  const metaDetails = [
    state.dashboard_paused ? "Paused by dashboard." : "",
    cooldown,
  ].filter(Boolean).join(" ");
  const health = (name, status, desc, tone = "") =>
    `<div class="health-row"><div><div class="health-name">${escape(name)}</div><div class="health-detail">${escape(desc)}</div></div><div class="health-state"><span class="light ${tone}"></span>${escape(status)}</div></div>`;
  content.innerHTML =
    heading(
      "Your pipeline, at a glance.",
      `${formatTime(state.at, { weekday: "long" })} · ${state.timezone}`,
      state.dashboard_paused
        ? button("resume", "", "Lift dashboard pause")
        : state.meta_paused ? "" : button("pause"),
    ) +
    `<div class="metrics-grid">${metric("READY TO GO", `${state.ready} <em>/ ${state.target}</em>`, "Ready buffer · target from config", "pink")}${metric("LIVE PLATFORM POSTS", number(live), "Instagram + Facebook")}${metric("RECORDED VIEWS", compact(latest.reduce((n, m) => n + m.views, 0)), "Latest stored snapshot per post", "green")}${metric("AWAITING A DECISION", review, "Rendered, approval & review queues", "purple")}</div>
    <div class="grid-main"><section class="panel">${panelHead("Production funnel", "Every pipeline stage · current inventory", pill(`${state.items.length} items`))}
    ${state.stages.map((s) => `<a href="/queue?stage=${s.stage}" class="funnel-row"><span class="funnel-label">${escape(pretty(s.stage))}</span><div class="track"><div class="fill" style="width:${(s.count / max) * 100}%;background:${["failed", "rejected"].includes(s.stage) ? "var(--red)" : s.stage === "needs_review" ? "var(--amber)" : s.stage === "queued" ? "var(--pink)" : "var(--purple)"}"></div></div><b>${number(s.count)}</b></a>`).join("")}</section>
    <section class="panel">${panelHead("System health", "Observed state from the runner and SQLite")}
    ${health("Runner", pretty(lock.status), lock.age === null ? "No valid heartbeat" : `Heartbeat ${lock.age}s ago · ${lock.host || "unknown host"}${runtime.stage ? " · " + runtime.stage : ""}`, lock.status === "alive" ? "" : lock.status === "stale" ? "red" : "gray")}
    ${health("Meta publishing", state.meta_paused ? "Paused" : "Enabled", state.meta_paused ? metaDetails : state.dry_run ? "Dry run is on; publish requests are suppressed." : "The runner publishes at scheduled slots.", state.meta_paused ? "amber" : "")}
    ${health("Gemini quota", quotaText, quotaBlocked ? `Runner reported a block until ${formatTime(runtime.gemini_blocked_until)}.` : "Remaining daily allowance is not exposed by the pipeline.", quotaBlocked ? "amber" : quotaKnown ? "" : "gray")}
    ${state.limits
      .filter((l) => l.source !== "meta_graph")
      .map((l) =>
        health(
          pretty(l.source),
          l.paused ? "Paused" : "Available",
          l.paused ? l.reason : "No active source cooldown.",
          l.paused ? "amber" : "",
        ),
      )
      .join("")}
    ${health("Approval mode", pretty(state.approval_mode), state.days_live === null ? "The first live post starts the seven-day clock." : `${state.days_live.toFixed(1)} days since first live post.`)}
    <div class="ring-wrap"><div class="ring">${Math.round(state.probabilities.A * 100)}%</div><div><h3>Variant A allocation</h3><div class="small">B receives ${Math.round(state.probabilities.B * 100)}% of eligible draws</div></div></div><p class="reason">${escape(state.variant_reason)}</p></section></div>
    <div class="grid-main"><section class="panel">${panelHead("Next in line", "Scheduled in " + state.timezone, '<a class="small" href="/queue">Open calendar ↗</a>')}
    ${
      next.length
        ? next
            .map((q) => {
              const slot = q.posts[0].slot_datetime;
              return `<a class="upcoming-row" href="${itemLink(q.item.id)}"><div class="date-tile">${escape(formatTime(slot, { day: undefined, hour: undefined, minute: undefined, month: "short" }))}<b>${escape(formatTime(slot, { month: undefined, hour: undefined, minute: undefined, day: "2-digit" }))}</b></div><div class="grow"><h3>${escape(q.item.original_title)}</h3><div class="meta">${escape(q.item.author_name)} · ${escape(q.item.source)}</div></div><div class="next-time">${escape(formatTime(slot, { day: undefined, month: undefined }))}<div>${pill(q.posts[0].variant === "?" ? "A/B at publish" : `Variant ${q.posts[0].variant}`)}</div></div></a>`;
            })
            .join("")
        : empty("No scheduled items. Approve a render to fill the queue.")
    }</section>
    <section class="panel">${panelHead("Views by platform", "Latest snapshot totals, grouped by publish day")}<div class="chart-box"><canvas id="overview-chart" role="img" aria-label="Views by platform over publish dates"></canvas></div><div class="legend"><span><i style="background:var(--pink)"></i>Instagram</span><span><i style="background:var(--blue)"></i>Facebook</span></div></section></div>${commandsPanel()}`;
  platformChart("overview-chart", latest);
}
function queue() {
  const query = new URLSearchParams(location.search).get("stage");
  if (query && state.stages.some((s) => s.stage === query))
    filters.stage = query;
  const all =
    filters.stage === "all"
      ? state.items.filter((i) => ["approved", "queued"].includes(i.stage))
      : state.items.filter((i) => i.stage === filters.stage);
  const rows = all
    .filter((i) =>
      (i.original_title + " " + i.author_name + " " + i.id)
        .toLowerCase()
        .includes(filters.queue.toLowerCase()),
    )
    .map((item) => ({
      item,
      posts: state.posts.filter((p) => p.item_id === item.id),
    }))
    .sort((a, b) =>
      (a.posts[0]?.slot_datetime || "z").localeCompare(
        b.posts[0]?.slot_datetime || "z",
      ),
    );
  const groups = new Map();
  for (const row of rows) {
    const key = dayKey(row.posts[0]?.slot_datetime);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  content.innerHTML =
    heading(
      "The publishing calendar.",
      `${state.profile} profile · ${state.timezone} · slots ${state.slots.join(" & ")}`,
      pill(`${rows.length} items`),
    ) +
    `<div class="toolbar"><input type="search" id="queue-search" aria-label="Search items" placeholder="Search clips, creators or item IDs" value="${escape(filters.queue)}"><select id="stage-filter" aria-label="Filter stage"><option value="all">Approved & queued</option>${state.stages.map((s) => `<option value="${s.stage}" ${filters.stage === s.stage ? "selected" : ""}>${pretty(s.stage)} (${s.count})</option>`).join("")}</select></div>
  ${[...groups].map(([day, list]) => `<div class="section-label">${day === "unscheduled" ? "WAITING FOR A SLOT" : escape(formatTime(list[0].posts[0].slot_datetime, { hour: undefined, minute: undefined, weekday: "long", year: "numeric" }))}</div>${list.map(({ item, posts }) => `<article class="queue-card"><div class="queue-time">${posts[0] ? escape(formatTime(posts[0].slot_datetime, { day: undefined, month: undefined })) : "—"}<div class="small">${posts[0] ? "SCHEDULED" : "UNSCHEDULED"}</div></div><div><h3><a href="${itemLink(item.id)}">${escape(item.original_title)}</a></h3><p>${escape(item.author_name)} · ${escape(item.source)} · ${escape(item.hook_text || "Hook not generated")}</p><div class="chips">${pill(pretty(item.stage), item.stage === "queued" ? "pink" : "")}${pill(["A", "B"].includes(posts[0]?.variant || item.chosen_variant) ? `Variant ${posts[0]?.variant || item.chosen_variant}` : "A/B drawn at publish")}${posts.map((p) => pill(`${p.platform} · ${p.status}`)).join("")}</div></div><a class="button secondary" href="${itemLink(item.id)}">Review clip ↗</a></article>`).join("")}`).join("") || empty("No items match this filter.")}${commandsPanel()}`;
}
function analytics() {
  const latest = totals(),
    views = latest.reduce((n, m) => n + m.views, 0);
  const median = latest.length
    ? [...latest].sort((a, b) => a.views - b.views)[
        Math.floor(latest.length / 2)
      ].views
    : 0;
  const breakdown = (field) => {
    const groups = new Map();
    for (const m of latest) {
      const k = m[field] || "unknown";
      const v = groups.get(k) || { views: 0, n: 0 };
      v.views += m.views;
      v.n++;
      groups.set(k, v);
    }
    return [...groups].sort((a, b) => b[1].views - a[1].views);
  };
  const bars = (field) => {
    const rows = breakdown(field),
      max = Math.max(1, ...rows.map((r) => r[1].views));
    return (
      rows
        .map(
          ([label, v]) =>
            `<div class="bar-row"><div class="bar-label"><span>${escape(pretty(label))}</span><span>${compact(v.views)} <span class="muted">· ${v.n} posts</span></span></div><div class="track"><div class="fill" style="width:${(v.views / max) * 100}%;background:${field === "source" ? "var(--green)" : "var(--purple)"}"></div></div></div>`,
        )
        .join("") || empty("No stored measurements yet.")
    );
  };
  content.innerHTML =
    heading(
      "Learn from every Reel.",
      "Stored insights only · no live API requests",
    ) +
    `<div class="metrics-grid">${metric("TOTAL RECORDED VIEWS", compact(views), "Latest available snapshot per platform post", "pink")}${metric("MEASURED POSTS", latest.length, "Platform posts with at least one snapshot")}${metric("MEDIAN VIEWS", compact(median), "Across measured platform posts", "green")}${metric("SNAPSHOTS", state.metrics.length, "All stored observation windows", "purple")}</div>
  <div class="grid-main"><section class="panel">${panelHead("Views over time", "Latest views grouped by publish date; cumulative snapshots are not added together.")}<div class="chart-box"><canvas id="analytics-chart" role="img" aria-label="Instagram and Facebook views by publish date"></canvas></div><div class="legend"><span><i style="background:var(--pink)"></i>Instagram</span><span><i style="background:var(--blue)"></i>Facebook</span></div></section>
  <section class="panel">${panelHead("A/B allocation", "Current probabilities from the scheduler")}
  <div class="allocation"><div style="width:${state.probabilities.A * 100}%;background:var(--purple)">A · ${Math.round(state.probabilities.A * 100)}%</div><div style="width:${state.probabilities.B * 100}%;background:var(--green)">B · ${Math.round(state.probabilities.B * 100)}%</div></div><p class="reason">${escape(state.variant_reason)}</p>
  ${["A", "B"].map((v) => `<div class="health-row"><span>Variant ${v}</span><span class="small">${state.variants[v]?.n || 0} items · score ${(state.variants[v]?.score || 0).toFixed(3)}</span></div>`).join("")}</section></div>
  <div class="grid-half"><section class="panel">${panelHead("Performance by hook style", "Latest recorded views · grouped by post hook style")}${bars("hook_style")}</section><section class="panel">${panelHead("Performance by source", "Latest recorded views · grouped by post source")}${bars("source")}</section></div>
  <section class="panel">${panelHead("Follow an individual post", "Views at each stored observation time")}<div class="toolbar"><select id="post-select" aria-label="Select post"><option value="">Choose a platform post</option>${latest.map((m) => `<option value="${m.post_id}">${escape(itemById(m.item_id)?.original_title || m.item_id)} · ${m.platform} · ${m.variant}</option>`).join("")}</select></div><div class="chart-box"><canvas id="post-chart" role="img" aria-label="Selected post views at snapshot times"></canvas></div></section>`;
  platformChart("analytics-chart", latest);
  const select = document.querySelector("#post-select");
  select.addEventListener("change", () => postChart(select.value));
  if (latest.length) {
    select.value = latest[0].post_id;
    postChart(select.value);
  } else drawLines("post-chart", [], []);
}
function failureList(items) {
  return (
    items
      .map(
        (i) =>
          `<article class="failure-card"><div class="failure-symbol">${i.stage === "needs_review" ? "?" : "!"}</div><div class="grow"><h3><a href="${itemLink(i.id)}">${escape(i.original_title)}</a></h3><p>${escape(i.error_message || i.rejection_reason || i.hook_text || "Needs a human decision")}</p><div class="chips">${pill(pretty(i.error_stage || i.stage), i.stage === "needs_review" ? "amber" : "red")}${pill(i.source)}</div>${["posting", "publishing"].includes(i.error_stage) ? '<p class="error">Display-only. Retry could double-post.</p>' : ""}</div><a class="button secondary" href="${itemLink(i.id)}">Inspect ↗</a></article>`,
      )
      .join("") || empty("Nothing here.")
  );
}
function failures() {
  const failed = state.items.filter((i) => i.stage === "failed"),
    rejected = state.items.filter((i) => i.stage === "rejected"),
    review = state.items.filter((i) => i.stage === "needs_review");
  const stats = state.cut_stats;
  content.innerHTML =
    heading(
      "Catch what needs attention.",
      "Inspect the cause, review the clip, then choose the next step.",
    ) +
    `<div class="metrics-grid">${metric("FAILED", failed.length, "Items stopped by a stage error", "pink")}${metric("NEEDS REVIEW", review.length, "Hook or layout decisions waiting", "purple")}${metric("REJECTED", rejected.length, "Quality, dedupe or human decisions")}${metric("NO CLEAN ENDING", `${Math.round(stats.rate * 100)}%`, `${stats.no_clean_ending} of ${stats.attempts} cut attempts · last 24h`)}</div>
 <div class="grid-main"><section class="panel">${panelHead("Failed & rejected", "Publishing failures are display-only.")}<div class="toolbar"><select id="failure-filter" aria-label="Filter failures"><option value="all">All failed & rejected</option><option value="failed" ${filters.failures === "failed" ? "selected" : ""}>Failed only</option><option value="rejected" ${filters.failures === "rejected" ? "selected" : ""}>Rejected only</option></select></div>${failureList([...failed, ...rejected].filter((i) => filters.failures === "all" || i.stage === filters.failures))}</section>
 <div><section class="panel">${panelHead("Needs a human", "Open the item to see its segment and binding.", pill(`${review.length} waiting`, "amber"))}${failureList(review)}</section><section class="panel commands">${panelHead("Cut outcomes", "Last 24 hours")}<div class="bar-row"><div class="bar-label"><span>No clean ending</span><span>${stats.no_clean_ending}</span></div><div class="track"><div class="fill" style="width:${stats.rate * 100}%;background:var(--amber)"></div></div></div><p class="reason">${stats.attempts} completed cut attempts. This rate excludes attempts still in progress.</p></section></div></div>${commandsPanel()}`;
}
async function itemPage() {
  const id = decodeURIComponent(location.pathname.split("/")[2]);
  detail = await api("/api/items/" + encodeURIComponent(id));
  const i = detail.item;
  let renderHelp = "Render previews will appear when available.";
  if (i.stage === "needs_review") {
    renderHelp = "Review the hook and cut segment before rendering.";
  } else if (detail.media.a && detail.media.b && detail.actions.includes("approve")) {
    renderHelp = "Play both variants before approving.";
  }
  const video = (v, title) =>
    `<div class="video-card"><header><h3>${title}</h3>${pill(v.toUpperCase(), "pink")}</header>${detail.media[v] ? `<video controls preload="metadata" src="/media/${encodeURIComponent(i.id)}/${v}#t=0.1" aria-label="${title}"></video>` : empty("No render available for this variant.")}</div>`;
  content.innerHTML =
    `<a class="back-link" href="/queue">← Back to queue</a>` +
    heading(
      escape(i.original_title || i.id),
      `${i.author_name} · ${i.source} · ${i.id}`,
      pill(pretty(i.stage), "pink"),
    ) +
    (["posting", "publishing"].includes(i.error_stage) ||
    detail.posts.some((p) => !["pending", "dry_run"].includes(p.status))
      ? '<div class="notice">Posting history exists. Started or failed publishes are display-only to prevent duplicate posts.</div>'
      : "") +
    (i.preview_only
      ? '<div class="notice">Preview-only item. Approval and pipeline controls are blocked.</div>'
      : "") +
    `<div class="grid-main"><section class="panel">${panelHead("Render comparison", renderHelp)}
 <div class="video-grid">${video("a", "Captions only")}${video("b", "Voiceover + clip")}</div>
 <div class="detail-actions">${detail.actions
   .filter((a) => ["approve", "aonly", "reject", "hookok"].includes(a))
   .map((a) =>
     button(
       a,
       i.id,
       a === "hookok" && detail.review.kind === "source_text_collision"
         ? "Render anyway"
         : labels[a],
     ),
   )
   .join("")}</div>
 ${detail.media.cut ? `<details class="commands"><summary class="muted">Watch the cut segment</summary><video controls preload="none" src="/media/${encodeURIComponent(i.id)}/cut" style="width:100%;max-height:300px"></video></details>` : ""}</section>
 <section class="panel">${panelHead("The story", i.hook_style ? pretty(i.hook_style) + " hook" : "Hook pending", pill(i.authenticity || "unverified", i.authenticity === "official" ? "green" : "amber"))}<div class="hook-display">${escape(i.hook_text || "No hook generated yet")}</div><div class="label-value"><div class="eyebrow">VOICEOVER SCRIPT</div><p>${escape(i.voiceover_script || "Not generated")}</p></div><div class="label-value"><div class="eyebrow">CUT WINDOW</div><p>${i.cut_start == null ? "Not cut" : `${Number(i.cut_start).toFixed(2)} → ${Number(i.cut_end).toFixed(2)} s · ${Number(i.cut_duration || i.cut_end - i.cut_start).toFixed(2)} s`}</p></div>
 ${Object.entries(detail.binding)
   .map(
     ([k, v]) =>
       `<div class="binding-status ${v ? "warning" : ""}">${v ? "◉" : "✓"} ${escape(pretty(k))} binding${v ? `<br>${escape(v)}` : " verified for this segment"}</div>`,
   )
   .join("")}
 <div class="detail-actions">${detail.actions
   .filter((a) => !["approve", "aonly", "reject", "hookok"].includes(a))
   .map((a) => button(a, i.id))
   .join("")}</div></section></div>
 ${detail.review.reason ? `<section class="panel commands">${panelHead("Review context")}<p class="reason">${escape(detail.review.reason)}</p><p>${escape(detail.review.transcript_excerpt || "")}</p></section>` : ""}
 <section class="panel commands">${panelHead("Stage history", "Full history, oldest to newest", pill(`${detail.logs.length} events`))}${detail.logs.map((l) => `<div class="log-entry"><time>${escape(formatTime(l.created_at))}</time><span>${escape(pretty(l.stage))}</span><div>${pill(l.status, ["failed", "failure"].includes(l.status) ? "red" : l.status === "success" ? "green" : "")}<p>${escape(l.message)}${l.duration_s ? ` · ${Number(l.duration_s).toFixed(1)}s` : ""}</p></div></div>`).join("") || empty("No stage history recorded.")}</section>${commandsPanel()}`;
}
function platformChart(id, latest) {
  const days = [
    ...new Set(
      latest.filter((m) => m.posted_at).map((m) => dayKey(m.posted_at)),
    ),
  ].sort();
  const series = ["instagram", "facebook"].map((platform) => ({
    name: platform,
    color: colors[platform],
    values: days.map((day) =>
      latest
        .filter((m) => m.platform === platform && dayKey(m.posted_at) === day)
        .reduce((n, m) => n + m.views, 0),
    ),
  }));
  drawLines(
    id,
    days.map((d) => d.slice(5)),
    series,
  );
}
function postChart(id) {
  const rows = state.metrics.filter((m) => String(m.post_id) === String(id));
  drawLines(
    "post-chart",
    rows.map((m) => formatTime(m.fetched_at)),
    rows.length
      ? [
          {
            name: rows[0].platform,
            color: colors[rows[0].platform],
            values: rows.map((m) => m.views),
          },
        ]
      : [],
  );
}
function drawLines(id, labels, series) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const bounds = canvas.getBoundingClientRect(),
    w = bounds.width,
    h = bounds.height,
    dpr = devicePixelRatio || 1;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.font = "10px Segoe UI, Arial";
  ctx.fillStyle = "#919bb0";
  if (!labels.length) {
    ctx.textAlign = "center";
    ctx.fillText("No stored measurements yet", w / 2, h / 2);
    return;
  }
  const left = 44,
    top = 15,
    bottom = 33,
    right = 12,
    pw = w - left - right,
    ph = h - top - bottom,
    max = Math.max(1, ...series.flatMap((s) => s.values)) * 1.15;
  for (let n = 0; n < 5; n++) {
    const y = top + (ph * n) / 4;
    ctx.strokeStyle = "#2a3040";
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(w - right, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(compact(max * (1 - n / 4)), left - 10, y + 3);
  }
  const x = (i) =>
    left + (labels.length === 1 ? pw / 2 : (pw * i) / (labels.length - 1));
  const y = (v) => top + ph * (1 - v / max);
  const stride = Math.max(1, Math.ceil(labels.length / 5));
  const ticks = labels.map((label, i) => {
    const width = Math.min(ctx.measureText(label).width, w - 8);
    const start = Math.min(Math.max(4, x(i) - width / 2), w - 4 - width);
    return { label, start, width };
  });
  const last = ticks.at(-1);
  ctx.textAlign = "left";
  let nextStart = 4;
  ticks.forEach(({ label, start, width }, i) => {
    if (i === ticks.length - 1 ||
        (i % stride === 0 && start >= nextStart && start + width + 8 <= last.start)) {
      ctx.fillText(label, start, h - 9, width);
      nextStart = start + width + 8;
    }
  });
  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    s.values.forEach((v, i) =>
      i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)),
    );
    ctx.stroke();
    for (let i = 0; i < s.values.length; i++) {
      ctx.fillStyle = s.color;
      ctx.beginPath();
      ctx.arc(x(i), y(s.values[i]), 3, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  canvas.setAttribute(
    "aria-label",
    series
      .map(
        (s) =>
          `${s.name}: ${s.values.map((v, i) => `${labels[i]} ${v} views`).join(", ")}`,
      )
      .join("; "),
  );
}
async function api(url, body) {
  const response = await fetch(
    url,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": state.csrf,
          },
          body: JSON.stringify(body),
        },
  );
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(
      `Request failed (${response.status}). Refresh and try again.`,
    );
  }
  if (!response.ok)
    throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}
function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.hidden = false;
  setTimeout(() => (node.hidden = true), 7000);
}
function connections() {
  const tones = { ok: "green", failed: "red", warning: "amber", not_set: "gray", unchecked: "gray" };
  const names = { ok: "OK", failed: "Failed", warning: "Warning", not_set: "Not set", unchecked: "Not checked" };
  const disabled = connectionsBusy ? " disabled" : "";
  content.innerHTML = heading(
    "Connections",
    "Checks run only when pressed, on this computer. Each has a 10-second timeout.",
    `<button class="button primary" data-connection="all"${disabled}>${connectionsBusy ? "Testing…" : "Test all (sends a real success ping)"}</button>`,
  ) + `<section class="panel">${panelHead("Service connections", "Test all checks services in parallel and sends a real Healthchecks success ping. Telegram messages require the separate button below.")}
    <div class="table-wrap"><table class="data-table connections-table"><thead><tr><th>Service</th><th>Status</th><th>Reason</th><th>Last check</th><th>Test</th></tr></thead><tbody>
    ${connectionRows.map((row) => `<tr><td><code>${escape(row.label)}</code></td><td>${pill(names[row.status] || row.status, tones[row.status] || "gray")}</td><td class="connection-reason">${escape(row.reason)}</td><td>${row.checked_at ? escape(formatTime(row.checked_at, { second: "2-digit" })) : "Never"}</td><td><button class="button secondary" data-connection="${escape(row.id)}"${disabled}>${row.id === "healthcheck" ? "Test (sends a real success ping)" : "Test"}</button></td></tr>`).join("")}
    </tbody></table></div><div class="detail-actions"><button class="button secondary" data-send-telegram${disabled}>Send test message</button><span class="reason">Sends one Telegram message to the configured chat. Never included in Test all.</span></div><p id="telegram-test-result" class="reason" role="status"></p></section>`;
}
async function testConnection(service, sendMessage = false) {
  if (connectionsBusy) return;
  connectionsBusy = true;
  connections();
  try {
    const data = await api(sendMessage ? "/api/connections/send-telegram-test" : "/api/connections/test", sendMessage ? {} : { service });
    if (sendMessage) {
      const text = `${data.result.status}: ${data.result.reason}`;
      toast(text);
      connectionsBusy = false;
      connections();
      document.querySelector("#telegram-test-result").textContent = text;
    } else connectionRows = data.connections;
  } catch (error) {
    toast(error.message);
  } finally {
    connectionsBusy = false;
    if (!sendMessage) connections();
    document.querySelectorAll("[data-connection], [data-send-telegram]").forEach((node) => node.disabled = false);
  }
}
async function refresh(manual = false) {
  try {
    state = await api("/api/state");
    document.querySelector("#refresh-status").textContent =
      "Updated " +
      new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    document.querySelector("#sidebar-profile").textContent =
      state.profile + " · " + state.timezone;
    const banner = document.querySelector("#mode-banner");
    banner.classList.toggle("live", !state.dry_run);
    banner.innerHTML = `<span><strong>${state.dry_run ? "DRY RUN IS ON" : "LIVE POSTING IS ON"}</strong> &nbsp; ${state.dry_run ? "The runner simulates publishing." : "Approved clips can be published by the runner."}</span><span class="${state.demo ? "demo" : ""}">${state.demo ? "DEMO DATABASE · seeded example data" : state.config_changed ? "CONFIG CHANGED · restart runner to apply" : state.runtime_known ? "RUNNER SETTINGS" : state.meta_paused ? "META POSTING PAUSED" : "CONFIG SETTINGS · runner not observed"}</span>`;
    document.querySelector("#nav-queued").textContent = state.items.filter(
      (i) => i.stage === "queued",
    ).length;
    document.querySelector("#nav-failed").textContent = state.items.filter(
      (i) => ["failed", "needs_review"].includes(i.stage),
    ).length;
    const page = location.pathname.startsWith("/items/")
      ? "item"
      : location.pathname.slice(1) || "overview";
    document
      .querySelectorAll("nav a")
      .forEach((a) =>
        a.classList.toggle(
          "active",
          a.dataset.page === page ||
            (page === "item" && a.dataset.page === "queue"),
        ),
      );
    document.querySelector("#breadcrumb").textContent =
      "Workspace / " +
      {
        overview: "Overview",
        queue: "Queue & calendar",
        analytics: "Analytics",
        failures: "Failures & review",
        connections: "Connections",
        item: "Item detail",
      }[page];
    document.querySelector("#footer").textContent =
      `${state.timezone} · ${state.items.length} items · ${state.posts.length} platform posts · all data from the selected SQLite database`;
    if (page === "connections") {
      if (!connectionsBusy) {
        connectionRows = (await api("/api/connections")).connections;
        connections();
      }
    } else if (page === "item") {
      if (manual || !detail) await itemPage();
    } else if (
      !dialog.open &&
      !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)
    )
      (({ overview, queue, analytics, failures })[page] || overview)();
  } catch (error) {
    document.querySelector("#refresh-status").textContent = "Update failed";
    toast(error.message);
  }
}
function openAction(action, id) {
  pendingAction = { action, item_id: id || null };
  confirmation = null;
  document.querySelector("#dialog-title").textContent = labels[action];
  document.querySelector("#dialog-item").textContent =
    itemById(id)?.original_title || "Instagram + Facebook";
  document.querySelector("#dialog-error").textContent = "";
  document.querySelector("#confirm-content").hidden = true;
  const fields = document.querySelector("#action-fields");
  fields.hidden = false;
  if (action === "edit_hook")
    fields.innerHTML = `<label for="hook-edit">New hook text</label><textarea id="hook-edit" maxlength="220" required>${escape(detail?.item?.hook_text || "")}</textarea><p class="reason">The runner checks the edited text against the cut, invalidates the old renders, and renders both variants again.</p>`;
  else if (action === "reslot")
    fields.innerHTML = `<label for="slot-after">Find a free slot after (UTC)</label><input id="slot-after" type="datetime-local" required value="${new Date(Date.now() + 60000).toISOString().slice(0, 16)}"><p class="reason">The scheduler uses ${escape(state.timezone)}, the configured minimum gap and at most two items per local day. The next free slot is recalculated when applied.</p>`;
  else
    fields.innerHTML = `<p class="reason">${action === "pause" ? "This holds posting until you lift the dashboard pause. A request already in flight may finish." : action === "resume" ? "This lifts the dashboard pause. Any Meta cooldown stays in effect until it expires." : action === "hookok" ? "Confirm that the current hook fits the cut, or that the source-text layout is acceptable. The item returns to rendering before approval." : "This decision will be revalidated against the item’s current state before the runner applies it."}</p>`;
  document.querySelector("#submit-action").textContent = "Review change";
  dialog.showModal();
}
document.addEventListener("click", (event) => {
  const connectionButton = event.target.closest("[data-connection], [data-send-telegram]");
  if (connectionButton) {
    testConnection(connectionButton.dataset.connection, connectionButton.hasAttribute("data-send-telegram"));
    return;
  }
  const buttonNode = event.target.closest("[data-action]");
  if (buttonNode) openAction(buttonNode.dataset.action, buttonNode.dataset.id);
});
document.querySelector("#close-dialog").onclick = () => dialog.close();
document.querySelector("#cancel-dialog").onclick = () => dialog.close();
document.querySelector("#refresh").onclick = () => refresh(true);
document
  .querySelector("#action-form")
  .addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = document.querySelector("#submit-action");
    submit.disabled = true;
    try {
      if (!confirmation) {
        const payload = {};
        if (pendingAction.action === "edit_hook")
          payload.hook_text = document.querySelector("#hook-edit").value;
        if (pendingAction.action === "reslot")
          payload.after = new Date(
            document.querySelector("#slot-after").value + "Z",
          ).toISOString();
        confirmation = await api("/api/prepare", { ...pendingAction, payload });
        document.querySelector("#action-fields").hidden = true;
        document.querySelector("#dialog-error").textContent = "";
        const node = document.querySelector("#confirm-content");
        node.hidden = false;
        node.innerHTML = `<p><strong>${escape(confirmation.description)}</strong></p>${payload.hook_text ? `<p>“${escape(payload.hook_text)}”</p>` : ""}<p class="muted">${state.lock.status === "alive" ? "The runner will apply this at its next stage boundary." : "The runner is not active. This command will remain pending until its loop starts."}</p><p class="muted">Confirm this change to ${escape(confirmation.title)}?</p>`;
        document.querySelector("#dialog-title").textContent =
          "Confirm your change";
        submit.textContent = "Confirm & queue";
      } else {
        const result = await api("/api/confirm", {
          ticket: confirmation.ticket,
        });
        dialog.close();
        toast(
          "Command " +
            result.command.status +
            ". Its result appears in Recent controls.",
        );
        await refresh(true);
      }
    } catch (error) {
      document.querySelector("#dialog-error").textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
document.addEventListener("change", (event) => {
  if (event.target.id === "stage-filter") {
    filters.stage = event.target.value;
    history.replaceState(null, "", "/queue");
    queue();
  }
  if (event.target.id === "failure-filter") {
    filters.failures = event.target.value;
    failures();
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "queue-search") {
    const position = event.target.selectionStart;
    filters.queue = event.target.value;
    queue();
    const input = document.querySelector("#queue-search");
    input.focus();
    input.setSelectionRange(position, position);
  }
});
window.addEventListener("resize", () => {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    if (!dialog.open && !location.pathname.startsWith("/items/")) refresh();
  }, 250);
});
refresh(true);
setInterval(() => {
  if (!dialog.open && !document.hidden) refresh();
}, 15000);
