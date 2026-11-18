"use strict";
let state, detail, pendingAction, confirmation, refreshTimer;
let preferences, reviewId = new URLSearchParams(location.search).get("item"), jobId;
const busyActions = new Set();
const inboxPending = new Map();
const waitingCommands = new Map();
let reviewRequest = 0;
let undoQueue = Promise.resolve();
const undoHistory = [];
const notices = new Map();
const actionVerbs = { position: "move", up: "move", down: "move", aonly: "approve", bonly: "approve", clip_url: "queue clip from URL", restore_approval: "restore approval for" };
const keys = { a: "approve", "1": "aonly", "2": "bonly", h: "hookok", r: "reject", e: "edit_hook", c: "edit_caption", m: "remake_cut", g: "remake_hook" };
const reviewLink = (id) => {
  const tab = { needs_review: "decision", failed: "failed", rejected: "rejected" }[itemById(id)?.stage];
  return localLink("/review?item=" + encodeURIComponent(id) + (tab ? "&tab=" + tab : ""));
};
let libraryAnchor, libraryRequest = 0, draggedItem, queueCompact = false;
const selectedItems = new Set();
let connectionRows = [], connectionsBusy = false;
const content = document.querySelector("#content");
const dialog = document.querySelector("#action-dialog");
const themeColor = (name) => getComputedStyle(document.documentElement).getPropertyValue("--" + name).trim();
const colors = { get instagram() { return themeColor("pink"); }, get facebook() { return themeColor("blue"); } };
const labels = {
  position: "Move",
  go_live: "Go live", go_dry: "Back to dry run", approve_versions: "Approve versions",
  edit_caption_words: "Edit captions in video", edit_voiceover: "Edit voiceover",
  move_to_slot: "Move to slot", run_stage: "Run stage", snapshot_now: "Snapshot now",
  make_from_discovered: "Make this one",
  approve: "Approve both versions",
  aonly: "Approve Captions version only",
  bonly: "Approve Voiceover version only",
  reject: "Reject item",
  hookok: "Hook is right",
  edit_hook: "Edit hook",
  reslot: "Re-slot item",
  retry: "Retry stage",
  pause: "Pause all posting",
  resume: "Resume posting",
  make: "Make clips",
  clip_url: "Clip from URL",
  remake_cut: "Remake cut",
  remake_hook: "Regenerate hook",
  delete: "Delete",
  unapprove: "Unapprove",
  up: "Move up",
  down: "Move down",
  post_now: "Post now",
  edit_caption: "Edit post text (Instagram caption)",
};
const filters = { queue: "", stage: "all" };
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
const itemLink = (id) => localLink("/items/" + encodeURIComponent(id) + "?from=" + encodeURIComponent(location.pathname + location.search));
const itemById = (id) => state?.items.find((i) => i.id === id);
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
function queueTime(value) {
  const current = dayKey(new Date().toISOString());
  const next = new Date(current + "T12:00:00Z");
  next.setUTCDate(next.getUTCDate() + 1);
  const day = dayKey(value);
  const label = day === current ? "Today" : day === next.toISOString().slice(0, 10) ? "Tomorrow" :
    formatTime(value, { weekday: "short", hour: undefined, minute: undefined }).replace(",", "");
  const time = new Intl.DateTimeFormat("en-GB", {
    timeZone: state.timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(value));
  return label + " " + time;
}
function pill(text, tone = "") {
  return `<span class="pill ${tone}">${escape(text)}</span>`;
}
function button(action, id = "", text = labels[action]) {
  return `<button class="button ${["reject", "delete"].includes(action) ? "danger" : action === "approve" ? "primary" : "secondary"}" data-action="${action}" data-id="${escape(id)}">${escape(text)}</button>`;
}
function heading(title, subtitle, controls = "") {
  return `<div class="page-heading"><div><h1>${title}</h1>${subtitle ? `<p>${escape(subtitle)}</p>` : ""}</div>${controls}</div>`;
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
function resultMessage(message) {
  const rendered = escape(message);
  return rendered.replace(/(\/items\/[A-Za-z0-9_-]+)/g, '<a href="$1">Open existing item</a>');
}
const commandState = (c) => c.status === "pending" ? "queued" : c.status;
function commandsPanel() {
  if (!state.commands.length) return "";
  return `<section id="recent-controls" class="panel commands">${panelHead("Recent controls", `Quick changes apply immediately when stopped. Queued changes usually apply within about ${state.interval}s, depending on the active stage.`)}
    ${state.commands.slice(0, 8).map((c) => commandRow(c)).join("")}
    ${state.commands.length > 8 ? `<details><summary class="muted">Older controls</summary>${state.commands.slice(8).map(commandRow).join("")}</details>` : ""}</section>`;
}
function commandRow(c) {
  return `<div class="command-row">${pill(commandState(c), c.status === "applied" ? "green" : ["pending", "running"].includes(c.status) ? "amber" : "red")}<div class="grow"><strong>${escape(labels[c.action] || c.action)}</strong><p>${resultMessage(c.result || (c.status === "running" ? "Worker running." : "Waiting for a worker or the next runner boundary."))}</p></div><code>${escape(formatTime(c.created_at))}</code></div>`;
}
function overview() {
  const status = summaryStatus();
  const todo = state.todo || {};
  const rows = [
    [`${todo.to_review ?? state.items.filter((item) => isReviewItem(item) && item.stage !== "needs_review").length} clips to review`, '<a class="button secondary" href="/review">Review</a>'],
    [status.next_post_at ? "Next post " + formatTime(status.next_post_at, { day: undefined, month: undefined }) : "No post scheduled", '<a class="button secondary" href="/queue">Queue</a>'],
    [`Runner ${status.runner}`, `<button class="button secondary" data-runner="${status.runner === "running" ? "stop" : "start"}">${status.runner === "running" ? "Stop" : "Start"}</button>`],
    [`${status.problems} ${status.problems === 1 ? "problem" : "problems"}`, '<a class="button secondary" href="/review?tab=failed">Failures tab</a>'],
  ];
  patchMarkup(content, heading("Overview", "") +
    `<section class="panel todo-list">${rows.map(([label, control]) => `<div class="todo-row"><strong>${escape(label)}</strong>${control}</div>`).join("")}</section>
    <div class="health-strip">${pill("Gemini quota · " + (Date.parse(state.runtime?.gemini_blocked_until) > Date.now() ? "cooldown" : "not reported"))}
    ${pill("Ready buffer · " + state.ready + " / " + state.target)}
    ${pill("Approval · " + pretty(state.approval_mode))}
    ${button(state.dashboard_paused ? "resume" : "pause")}</div>${commandsPanel()}`);
}
function summaryStatus() {
  return state.status_line || {
    dry_run: state.dry_run,
    runner: state.lock.status === "alive" ? "running" : state.lock.status === "stale" ? "stale" : "stopped",
    next_post_at: queueItems().find(({ posts }) => posts[0]?.slot_datetime)?.posts[0].slot_datetime,
    problems: state.items.filter((item) => ["needs_review", "failed"].includes(item.stage)).length,
  };
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
    const key = queueCompact ? "compact" : dayKey(row.posts[0]?.slot_datetime);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  patchMarkup(content,
    heading(
      "Queue",
      `${state.profile} profile · ${state.timezone} · slots ${state.slots.join(" & ")}`,
      `<button class="button secondary" id="queue-view" aria-pressed="${queueCompact}">${queueCompact ? "Calendar" : "List view"}</button>`,
    ) + calendarMarkup() +
    `<div class="toolbar"><input type="search" id="queue-search" aria-label="Search items" placeholder="Search clips, creators or item IDs" value="${escape(filters.queue)}"><select id="stage-filter" aria-label="Filter stage"><option value="all">Approved & queued</option>${state.stages.map((s) => `<option value="${s.stage}" ${filters.stage === s.stage ? "selected" : ""}>${pretty(s.stage)} (${s.count})</option>`).join("")}</select></div>
  ${[...groups].map(([day, list]) => `<div class="section-label">${day === "compact" ? "Queue order" : day === "unscheduled" ? "Waiting for a slot" : escape(formatTime(list[0].posts[0].slot_datetime, { hour: undefined, minute: undefined, weekday: "long", year: "numeric" }))}</div>${list.map(({ item, posts }) => `<article class="queue-card ${queueCompact ? "queue-compact" : ""}" data-queue-item="${escape(item.id)}" draggable="${item.stage === "approved" || item.actions.includes("up")}"><div class="queue-time">${posts[0] ? escape(queueTime(posts[0].slot_datetime)) : "—"}<div class="small">${posts[0] ? "Scheduled" : "Unscheduled"}</div></div><div><h3><a href="${reviewLink(item.id)}">${escape(item.original_title)}</a></h3><p>${escape(item.author_name)} · ${escape(item.source)} · ${escape(item.hook_text || "Hook not generated")}</p><div class="chips">${pill(pretty(item.stage), item.stage === "queued" ? "pink" : "")}${pill(versionName(posts[0]?.variant || item.chosen_variant))}${posts.map((p) => pill(`${p.platform} · ${p.status}`)).join("")}</div></div><div class="detail-actions">${item.actions.includes("post_now") ? button("post_now", item.id) : ""}
      <details class="queue-menu"><summary class="button secondary" aria-label="More actions">…</summary>
      <div>${item.actions.filter((action) => ["unapprove", "up", "down", "reslot"].includes(action)).map((action) => button(action, item.id)).join("")}
      <a href="${reviewLink(item.id)}">Open in Review</a></div></details></div><div class="reslot-host"></div></article>`).join("")}`).join("") || empty("No items match this filter.")}${commandsPanel()}`);
}
document.addEventListener("click", (event) => {
  if (event.target.closest("#queue-view")) { queueCompact = !queueCompact; queue(); return; }
  const row = event.target.closest("[data-queue-item]");
  if (row && !event.target.closest("a, button, details, input, form")) location.href = localLink(reviewLink(row.dataset.queueItem));
});

document.addEventListener("dragstart", (event) => {
  const row = event.target.closest('[data-queue-item][draggable="true"]');
  if (!row) return;
  draggedItem = row.dataset.queueItem;
  event.dataTransfer.setData("text/plain", draggedItem);
  event.dataTransfer.effectAllowed = "move";
  row.classList.add("dragging");
});
document.addEventListener("dragover", (event) => {
  const row = event.target.closest("[data-queue-item]");
  if (!draggedItem || !row || row.dataset.queueItem === draggedItem) return;
  event.preventDefault();
  document.querySelector(".drop-target")?.classList.remove("drop-target");
  row.classList.add("drop-target");
});
document.addEventListener("drop", (event) => {
  const row = event.target.closest("[data-queue-item]");
  if (!draggedItem || !row || row.dataset.queueItem === draggedItem) return;
  event.preventDefault();
  runAction("position", draggedItem, { target: row.dataset.queueItem });
});
document.addEventListener("dragend", () => {
  draggedItem = null;
  document.querySelector(".dragging")?.classList.remove("dragging");
  document.querySelector(".drop-target")?.classList.remove("drop-target");
});

function metricTable(metrics) {
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Snapshot</th><th>Views</th><th>Likes</th><th>Comments</th><th>Shares</th></tr></thead><tbody>
    ${metrics.map((m) => `<tr><td>${m.hours ?? m.window_hours ?? m.window_h ?? m.snapshot_hours ?? "—"} h ${m.late ? pill("late", "amber") : ""}</td><td>${number(m.views)}</td><td>${number(m.likes)}</td><td>${number(m.comments)}</td><td>${number(m.shares)}</td></tr>`).join("")}</tbody></table></div>`;
}
function analytics() {
  const latest = totals();
  const warnings = state.analytics_warnings || [];
  patchMarkup(content, heading("Analytics", "Stored insights from published clips") +
    (warnings.length ? `<section class="panel warning-card"><h2>Missed snapshot windows</h2>${warnings.map((warning) =>
      `<p><a href="${itemLink(warning.item_id)}">${escape(itemById(warning.item_id)?.original_title || warning.item_id)}</a> · ${warning.window_h} h window missed · ${warning.late_snapshot_taken ? "Late snapshot taken" : "No late snapshot yet"}</p>`).join("")}</section>` : "") +
    (latest.length ? `<div class="metrics-grid">${["views", "likes", "comments", "shares"].map((field) => metric(field[0].toUpperCase() + field.slice(1), number(latest.reduce((sum, row) => sum + Number(row[field] || 0), 0)), "Latest snapshot per post")).join("")}</div>
    <section class="panel commands"><h2>Views by publish date</h2><div class="chart-box"><canvas id="analytics-chart" role="img" aria-label="Views by platform"></canvas></div><div class="legend"><span><i style="background:var(--pink)"></i>Instagram</span><span><i style="background:var(--blue)"></i>Facebook</span></div></section>
    <section class="panel commands"><h2>Post snapshots</h2>${latest.map((row) => `<article data-row="${escape(row.post_id)}"><h3><a href="${itemLink(row.item_id)}">${escape(itemById(row.item_id)?.original_title || row.item_id)}</a> · ${escape(row.platform)} · ${escape(versionName(row.variant))}</h3>${metricTable(state.metrics.filter((m) => m.post_id === row.post_id))}</article>`).join("")}</section>` :
    empty("Insights appear after a live post reaches its first snapshot window and the runner collects metrics.")));
  if (latest.length) platformChart("analytics-chart", latest);
}

async function itemPage(id = decodeURIComponent(location.pathname.split("/")[2])) {
  const priorId = detail?.item.id;
  detail = await loadDetail(id);
  const i = detail.item;
  selectedVersion = priorId === id ? selectedVersion : undefined;
  patchMarkup(content,
    `<a class="back-link" href="${escape(fromLink())}">← Back</a>` +
    heading(
      escape(i.original_title || i.id),
      `${i.author_name} · ${i.source} · ${i.id}`,
      pill(pretty(i.stage), "pink"),
    ) +
    (["posting", "publishing"].includes(i.error_stage) ||
    detail.posts.some((p) => p.status && !["pending", "dry_run", "live"].includes(p.status))
      ? '<div class="notice">Posting history exists. Started or failed publishes are display-only to prevent duplicate posts.</div>'
      : "") +
    (i.preview_only
      ? '<div class="notice">Preview-only item. Approval and pipeline controls are blocked.</div>'
      : "") +
    `<div class="grid-main"><section class="panel">${versionPanel(detail)}
 <div class="detail-actions">${approvalButtons(detail)}${detail.actions
   .filter((a) => ["approve", "aonly", "bonly", "reject", "hookok"].includes(a))
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
 ${detail.media.cut ? `<details class="commands"><summary class="muted">Watch the cut segment</summary><video controls preload="none" src="${mediaURL("/media/" + encodeURIComponent(i.id) + "/cut")}" style="width:100%;max-height:300px"></video></details>` : ""}</section>
 <section class="panel">${panelHead("The story", i.hook_style ? pretty(i.hook_style) + " hook" : "Hook pending", pill(i.authenticity || "unverified", i.authenticity === "official" ? "green" : "amber"))}<div class="hook-display">${escape(i.hook_text || "No hook generated yet")}</div><div class="label-value"><div class="eyebrow">Voiceover script</div><p>${escape(i.voiceover_script || "Not generated")}</p></div><div class="label-value"><div class="eyebrow">Cut window</div><p>${i.cut_start == null ? "Not cut" : `${Number(i.cut_start).toFixed(2)} → ${Number(i.cut_end).toFixed(2)} s · ${Number(i.cut_duration || i.cut_end - i.cut_start).toFixed(2)} s`}</p></div>
 ${Object.entries(detail.binding)
   .map(
     ([k, v]) =>
       `<div class="binding-status ${v ? "warning" : ""}">${v ? "◉" : "✓"} ${escape(pretty(k))} binding${v ? `<br>${escape(v)}` : " verified for this segment"}</div>`,
   )
   .join("")}
 <div class="label-value"><div class="eyebrow">Cut signal</div><p>${escape(pretty(i.cut_signal))}</p></div>
 <div class="detail-actions">${detail.actions
   .filter((a) => !["approve", "aonly", "bonly", "reject", "hookok"].includes(a) && a !== "edit_caption" && !contractActions.has(a))
   .map((a) => button(a, i.id))
   .join("")}</div></section></div>
 <section class="panel commands">${panelHead("Post text (Instagram caption)", "Creator credit and disclosure are appended when an override is saved.")}
 <p class="caption-preview">${escape(detail.caption)}</p>${detail.actions.includes("edit_caption") ? button("edit_caption", i.id) : ""}</section>
 ${postedDetails(detail)}
 ${detail.review.reason ? `<section class="panel commands">${panelHead("Review context")}<p class="reason">${escape(detail.review.reason)}</p><p>${escape(detail.review.transcript_excerpt || "")}</p></section>` : ""}
 <section class="panel commands">${panelHead("Stage history", "Full history, oldest to newest", pill(`${detail.logs.length} events`))}${detail.logs.map((l) => `<div class="log-entry"><time>${escape(formatTime(l.created_at))}</time><span>${escape(pretty(l.stage))}</span><div>${pill(l.status, ["failed", "failure"].includes(l.status) ? "red" : l.status === "success" ? "green" : "")}<p>${escape(l.message)}${l.duration_s ? ` · ${Number(l.duration_s).toFixed(1)}s` : ""}</p></div></div>`).join("") || empty("No stage history recorded.")}</section>${commandsPanel()}`);
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
        .reduce((n, m) => n + Number(m.views || 0), 0),
    ),
  }));
  drawLines(
    id,
    days.map((d) => d.slice(5)),
    series,
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
  ctx.font = "12px Segoe UI, Arial";
  ctx.fillStyle = themeColor("muted");
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
    ctx.strokeStyle = themeColor("line");
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
  const data = await response.json().catch(() => null);
  if (!response.ok || data === null) {
    const error = new Error(data?.error || `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}
function toast(message, { error = false, group, inverse, command, title } = {}) {
  const tone = error ? "error" : "success";
  const stack = document.querySelector("#toast");
  if (inverse) undoHistory.push({ inverse, command, title });

  let notice = notices.get(tone);
  if (!notice) {
    const entry = document.createElement("div");
    entry.className = "toast-entry toast-" + tone;
    entry.setAttribute("role", error ? "alert" : "status");
    const text = document.createElement("span");
    text.className = "toast-message";
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "toast-undo";
    undo.textContent = "Undo last";
    undo.onclick = undoLast;
    entry.append(text, undo);
    stack.append(entry);
    notice = { entry, text, undo, paused: false };
    notice.dismiss = () => { entry.remove(); notices.delete(tone); };
    notice.start = () => {
      notice.started = performance.now();
      notice.timer = setTimeout(notice.dismiss, notice.remaining);
    };
    const pause = () => {
      if (notice.paused) return;
      notice.paused = true;
      clearTimeout(notice.timer);
      notice.remaining = Math.max(0, notice.remaining - (performance.now() - notice.started));
    };
    const resume = () => {
      if (!notice.paused || entry.matches(":hover, :focus-within")) return;
      notice.paused = false;
      notice.start();
    };
    entry.addEventListener("mouseenter", pause);
    entry.addEventListener("mouseleave", resume);
    entry.addEventListener("focusin", pause);
    entry.addEventListener("focusout", () => queueMicrotask(resume));
    notices.set(tone, notice);
  }
  clearTimeout(notice.timer);
  notice.count = group && group === notice.group ? notice.count + 1 : 1;
  notice.group = group;
  notice.text.textContent = notice.count > 1 ? `${notice.count} ${group}` : message;
  notice.undo.hidden = error || !undoHistory.length;
  notice.remaining = error ? 10000 : 6000;
  if (!notice.paused) notice.start();
}

function actionError(action, title, reason) {
  const verb = actionVerbs[action] || pretty(action);
  return `Can’t ${verb}${title ? " “" + title + "”" : ""}: ${reason}`;
}

function undoLast() {
  undoQueue = undoQueue.then(async () => {
    const previous = undoHistory.pop();
    if (!previous) return;
    const { inverse, command, title } = previous;
    try {
      if (command?.status === "pending") await waitForCommand(command.id);
      await runAction(inverse[0], inverse[1], inverse[2] || {}, null, null, null, true);
    } catch (error) {
      toast(actionError("undo", title, error.message), { error: true });
    }
  });
}

function commandNotice(action, command, title, payload = {}, inverse, warning) {
  if (warning || ["refused", "failed"].includes(command.status)) {
    toast(actionError(action, title, warning || command.result), { error: true });
    return;
  }
  const pending = command.status === "pending";
  const isApproval = ["approve", "approve_versions", "aonly", "bonly"].includes(action);
  const group = isApproval ? (pending ? "approvals queued" : "approved") : undefined;
  const count = payload.count;
  const messages = {
    make: `${count} ${count === 1 ? "clip" : "clips"} queued`,
    clip_url: "Clip queued",
  };
  toast((isApproval ? (pending ? "Approval queued" : "1 approved") : messages[action]) || [title, command.result || "Change queued"].filter(Boolean).join(" · "),
    { group, inverse, command, title });
}

async function waitForCommand(id) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const fresh = await api("/api/state");
    const command = fresh.commands.find((entry) => entry.id === id);
    if (command?.status === "applied") return;
    if (command && ["failed", "refused"].includes(command.status)) throw new Error(command.result);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("The original change is still waiting for a runner boundary.");
}

function showCompletedControls() {
  for (const [id, { title, action }] of waitingCommands) {
    const entry = state.commands.find((command) => command.id === id);
    if (!entry || ["pending", "running"].includes(entry.status)) continue;
    waitingCommands.delete(id);
    if (["refused", "failed"].includes(entry.status)) commandNotice(action, entry, title);
  }
}

async function runAction(action, id, payload = {}, trigger, inlineError, reverseEdit, undoing = false) {
  const key = id || action;
  if (busyActions.has(key)) return false;
  busyActions.add(key);
  if (trigger) {
    trigger.disabled = true;
    trigger.classList.add("working");
  }
  const title = itemById(id)?.original_title || (action === "clip_url" ? payload.url : null);
  try {
    const plan = await api("/api/prepare", { action, item_id: id || null, payload });
    const { command, warning, undo } = await api("/api/confirm", { ticket: plan.ticket });
    if (["refused", "failed"].includes(command.status)) throw new Error(warning || command.result);
    if (["make", "clip_url", "remake_cut", "remake_hook"].includes(action)) jobId = command.id;
    const inverse = undo ? [undo.action, undo.item_id, undo.payload] : {
      approve: ["unapprove", id], aonly: ["unapprove", id], bonly: ["unapprove", id],
      pause: ["resume"], resume: ["pause"], up: ["down", id], down: ["up", id],
      position: ["position", id, { target: plan.payload?.undo_target }],
      unapprove: ["restore_approval", id, { command_id: command.id }],
    }[action] || reverseEdit;
    if (command.status === "pending") waitingCommands.set(command.id, { title, action });
    commandNotice(action, command, title, payload, undoing ? null : inverse, warning);
    if (isReviewPage() && id === reviewId &&
        ["approve", "approve_versions", "aonly", "bonly", "hookok", "reject", "remake_cut", "remake_hook", "edit_hook", "edit_caption_words", "edit_voiceover"].includes(action)) {
      advanceReview(id, command.id);
    }
    await refresh(true);
    return true;
  } catch (error) {
    if (contractActions.has(action) && isUnavailable(error)) {
      unavailable.add(action);
      if (trigger) trigger.hidden = true;
      return false;
    }
    if (inlineError) inlineError.innerHTML = resultMessage(error.message);
    toast(actionError(action, title, error.message), { error: true });
    if (location.pathname === "/queue" && ["position", "up", "down"].includes(action)) await refresh(true);
    return false;
  } finally {
    busyActions.delete(key);
    if (trigger) {
      trigger.disabled = false;
      trigger.classList.remove("working");
    }
  }
}

function triggerAction(node) {
  const { action, id } = node.dataset;
  if (["edit_hook", "edit_caption"].includes(action) && detail?.item.id === id) {
    openInlineEditor(action, id);
    return;
  }
  if (action === "reslot") {
    openInlineReslot(node, id);
    return;
  }
  if (["post_now", "delete"].includes(action)) {
    openAction(action, id, node);
    return;
  }
  runAction(action, id, {}, node);
}

function openInlineReslot(trigger, id) {
  if (document.querySelector("#inline-reslot")) return;
  const form = document.createElement("form");
  form.id = "inline-reslot";
  form.className = "inline-editor";
  form.innerHTML = `<label>Find a free slot after (UTC)<input type="datetime-local" required value="${new Date(Date.now() + 60000).toISOString().slice(0, 16)}"></label>
    <button class="button primary">Move to next free slot</button>
    <button class="button secondary" type="button">Cancel</button><p role="status"></p>`;
  const host = trigger.closest(".queue-card")?.querySelector(".reslot-host");
  if (host) host.append(form);
  else trigger.closest(".detail-actions").after(form);
  const field = form.querySelector("input");
  const cancel = () => { form.remove(); trigger.focus({ preventScroll: true }); };
  form.querySelector('[type="button"]').onclick = cancel;
  form.onkeydown = (event) => { if (event.key === "Escape") cancel(); };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const after = new Date(field.value + "Z").toISOString();
    if (await runAction("reslot", id, { after }, event.submitter, form.querySelector("p"))) cancel();
  };
  field.focus();
}

function openInlineEditor(action, id) {
  const caption = action === "edit_caption";
  const region = document.querySelector(caption ? ".caption-preview" : ".hook-display");
  if (!region || region.querySelector("form")) return;
  const previous = region.innerHTML;
  const initial = { body: detail.item.caption_body ?? detail.item.hook_text ?? "", hashtags: detail.caption_hashtags };
  region.innerHTML = `<form class="inline-editor"><label>${caption ? "Post text (Instagram caption)" : "Hook"}<textarea required maxlength="${caption ? 2200 : 220}">${escape(caption ? detail.item.caption_body ?? detail.item.hook_text : detail.item.hook_text)}</textarea></label>${caption ? `<label>Hashtags<input value="${escape(detail.caption_hashtags)}"></label>` : ""}<button class="button primary">Save</button> <button type="button" class="button secondary">Cancel</button><p role="status"></p></form>`;
  const form = region.querySelector("form");
  const field = form.querySelector("textarea");
  const cancel = () => { region.innerHTML = previous; };
  form.querySelector('[type="button"]').onclick = cancel;
  form.onkeydown = (event) => {
    if (event.key === "Escape") { event.preventDefault(); cancel(); }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const payload = caption ? { body: field.value, hashtags: form.querySelector("input").value } : { hook_text: field.value };
    if (await runAction(action, id, payload, form.querySelector("button"), form.querySelector("p"), caption ? ["edit_caption", id, initial] : null)) {
      if (form.isConnected) {
        cancel();
        if (isReviewPage()) await review(true);
        else await itemPage(id);
      }
    }
  };
  field.focus();
  field.select();
}

function connections() {
  const settingsPage = location.pathname === "/settings";
  const tones = { ok: "green", failed: "red", warning: "amber", not_set: "gray", unchecked: "gray" };
  const names = { ok: "OK", failed: "Failed", warning: "Warning", not_set: "Not set", unchecked: "Not checked" };
  const disabled = connectionsBusy ? " disabled" : "";
  patchMarkup(content, (settingsPage ? settingsTabs() : "") + heading(
    "Connections",
    "Checks run only when pressed, on this computer. Each has a 10-second timeout.",
    `<button class="button primary" data-connection="all"${disabled}>${connectionsBusy ? "Testing…" : "Test all (sends a real success ping)"}</button>`,
  ) + `<section class="panel">${panelHead("Service connections", "Test all checks services in parallel and sends a real Healthchecks success ping. Telegram messages require the separate button below.")}
    <div class="table-wrap"><table class="data-table connections-table"><thead><tr><th>Service</th><th>Status</th><th>Reason</th><th>Last check</th><th>Test</th></tr></thead><tbody>
    ${connectionRows.map((row) => `<tr><td><code>${escape(row.label)}</code></td><td>${pill(names[row.status] || row.status, tones[row.status] || "gray")}</td><td class="connection-reason">${escape(row.reason)}</td><td>${row.checked_at ? escape(formatTime(row.checked_at, { second: "2-digit" })) : "Never"}</td><td><button class="button secondary" data-connection="${escape(row.id)}"${disabled}>${row.id === "healthcheck" ? "Test (sends a real success ping)" : "Test"}</button></td></tr>`).join("")}
    </tbody></table></div><div class="detail-actions"><button class="button secondary" data-send-telegram${disabled}>Send test message</button><span class="reason">Sends one Telegram message to the configured chat. Never included in Test all.</span></div><p id="telegram-test-result" class="reason" role="status"></p></section>`);
}
async function testConnection(service, sendMessage = false) {
  if (connectionsBusy) return;
  connectionsBusy = true;
  connections();
  try {
    const data = await api(sendMessage ? "/api/connections/send-telegram-test" : "/api/connections/test", sendMessage ? {} : { service });
    if (sendMessage) {
      const text = `${data.result.status}: ${data.result.reason}`;
      toast(text, { error: data.result.status !== "ok" });
      connectionsBusy = false;
      connections();
      document.querySelector("#telegram-test-result").textContent = text;
    } else { connectionRows = data.connections; toast("Connection checks finished."); }
  } catch (error) {
    toast(error.message, { error: true });
  } finally {
    connectionsBusy = false;
    if (!sendMessage) connections();
    document.querySelectorAll("[data-connection], [data-send-telegram]").forEach((node) => node.disabled = false);
  }
}
function refreshRecentControls() {
  const panel = document.querySelector("#recent-controls");
  if (panel && commandsPanel()) {
    const template = document.createElement("template");
    template.innerHTML = commandsPanel();
    patchMarkup(panel, template.content.firstElementChild.innerHTML);
  } else if (panel) panel.remove();
  else content.insertAdjacentHTML("beforeend", commandsPanel());
}

function updateRunnerLabel() {
  const runnerDot = document.querySelector("#nav-runner");
  const running = state.lock.status === "alive" && state.lock.kind !== "dashboard";
  runnerDot.dataset.status = running ? "alive" : "stopped";
  document.querySelector("#nav-runner-label").textContent = !running ? "Runner stopped" :
    state.lock.kind === "make" ? "Make job running" : "Runner running";
}

async function refresh(manual = false) {
  try {
    state = await api("/api/state");
    showCompletedControls();
    document.querySelector("#refresh-status").textContent =
      "Updated " +
      new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    document.querySelector("#sidebar-profile").textContent =
      state.profile + " · " + state.timezone;
    const status = summaryStatus();
    document.querySelector("#mode-banner").textContent = [
      status.dry_run ? "Dry run" : "Live posting",
      "runner " + status.runner,
      status.next_post_at && "next post " + formatTime(status.next_post_at, { day: undefined, month: undefined }),
    ].filter(Boolean).join(" · ");
    document.querySelector("#nav-review").textContent = state.todo?.to_review ?? state.items.filter(isReviewItem).length;
    if (!logsChecked) {
      logsChecked = true;
      optionalGet("/api/logs?lines=1").then((result) => {
        document.querySelector("#nav-logs").hidden = !result;
      }).catch(() => {});
    }
    updateRunnerLabel();
    document.querySelector("#nav-queued").textContent = state.items.filter(
      (i) => i.stage === "queued",
    ).length;
    const path = location.pathname;
    const page = path.startsWith("/items/") ? "item" : isReviewPage() ? "review" :
      (path === "/" && new URLSearchParams(location.search).get("page") === "logs" ? "logs" : path.slice(1) || "overview");
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
        queue: "Queue",
        analytics: "Analytics",
        connections: "Connections",
        settings: "Settings",
        make: "Make",
        review: "Review",
        library: "Library",
        item: "Item detail",
        logs: "Logs",
      }[page];
    document.querySelector("#footer").textContent =
      `${state.timezone} · ${state.items.length} items · ${state.posts.length} platform posts · all data from the selected SQLite database`;
    if (page === "connections" || page === "settings") {
      await renderSettings();
    } else if (page === "logs") {
      await renderLogs();
    } else if (page === "queue") {
      await updateSchedule();
      queue();
    } else if (page === "review") {
      await review(false);
    } else if (page === "item") {
      if (!content.querySelector(".inline-editor") && !content.querySelector(".editor[open]") && (manual || !detail || itemById(detail.item.id)?.stage !== detail.item.stage || ["posted", "measured", "cleaned"].includes(detail.item.stage))) await itemPage();
      else refreshRecentControls();
    } else if (page === "make") {
      make();
      await discovered();
    } else if (page === "library") {
      await library();
    } else if (
      !document.querySelector("dialog[open]") && page !== "library"
    )
      (({ overview, queue, analytics, make, library })[page] || overview)();
  } catch (error) {
    document.querySelector("#refresh-status").textContent = "Update failed";
    toast(error.message, { error: true });
  }
}
async function openAction(action, id, trigger, payload = {}) {
  if (dialog.open || busyActions.has(id)) return;
  pendingAction = { action, item_id: id || null, payload };
  confirmation = null;
  document.querySelector("#dialog-title").textContent = labels[action];
  const subtitle = document.querySelector("#dialog-item");
  subtitle.textContent = itemById(id)?.original_title || "";
  subtitle.hidden = !subtitle.textContent;
  const fields = document.querySelector("#action-fields");
  const summary = document.querySelector("#confirm-content");
  const error = document.querySelector("#dialog-error");
  const submit = document.querySelector("#submit-action");
  error.textContent = "";
  fields.hidden = true;
  fields.replaceChildren();
  summary.hidden = true;
  submit.disabled = true;
  submit.textContent = "Confirm";
  dialog.showModal();
  const request = pendingAction;
  trigger.classList.add("working");
  try {
    const plan = await api("/api/prepare", request);
    if (!dialog.open || request !== pendingAction) return;
    confirmation = plan;
    summary.textContent = plan.description;
    summary.hidden = false;
    submit.disabled = false;
  } catch (failure) {
    if (contractActions.has(action) && isUnavailable(failure)) {
      unavailable.add(action);
      trigger.hidden = true;
      dialog.close();
    } else if (dialog.open && request === pendingAction) error.textContent = failure.message;
  } finally {
    trigger.classList.remove("working");
  }
}
document.addEventListener("click", (event) => {
  const connectionButton = event.target.closest("[data-connection], [data-send-telegram]");
  if (connectionButton) {
    testConnection(connectionButton.dataset.connection, connectionButton.hasAttribute("data-send-telegram"));
    return;
  }
  const runnerButton = event.target.closest("[data-runner]");
  if (runnerButton) { changeRunner(runnerButton.dataset.runner); return; }
  const bulkButton = event.target.closest("[data-bulk]");
  if (bulkButton) { bulkAction(bulkButton.dataset.bulk); return; }
  const buttonNode = event.target.closest("[data-action]");
  if (buttonNode) triggerAction(buttonNode);
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
      if (!confirmation) return;
      const result = await api("/api/confirm", { ticket: confirmation.ticket });
      dialog.close();
      if (result.command.status === "pending") waitingCommands.set(result.command.id, { title: confirmation.title, action: pendingAction.action });
      if (pendingAction.action === "delete" && result.command.status !== "refused" &&
          result.command.status !== "failed" && isReviewPage()) {
        advanceReview(pendingAction.item_id, result.command.id);
        await review();
      }
      commandNotice(pendingAction.action, result.command, confirmation.title, {}, null, result.warning);
      await refresh(true);
    } catch (error) {
      document.querySelector("#dialog-error").innerHTML = resultMessage(error.message);
    } finally {
      submit.disabled = false;
    }
  });
document.addEventListener("change", (event) => {
  if (event.target.id === "job-select") { jobId = event.target.value; updateMakeProgress(); document.querySelector("#job-select")?.focus({ preventScroll: true }); }
  if (event.target.matches("[data-library-filter]")) updateLibraryFilter(event.target);
  if (event.target.id === "stage-filter") {
    filters.stage = event.target.value;
    history.replaceState(null, "", localLink("/queue"));
    queue();
  }
});
document.addEventListener("input", (event) => {
  if (event.target.name === "q" && event.target.matches("[data-library-filter]")) updateLibraryFilter(event.target);
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
mockReady.then(() => refresh(true)).catch((error) => toast(error.message, { error: true }));
setInterval(() => {
  if (!dialog.open && !document.hidden) refresh();
}, 30000);

function make() {
  if (!document.querySelector("#make-form")) {
    content.innerHTML = heading("Make", "") +
      `<section class="panel"><p id="make-runner"></p><form id="make-form">
        <div class="grid-half"><div><label>Clips <input name="count" type="number" min="1" max="10" value="2" required></label>
        <button class="button primary" name="task" value="make" formnovalidate>Make</button></div>
        <div><label>Source URL <input name="url" type="url" required></label>
        <p id="source-type" role="status"></p>
        <div class="grid-half"><label>Start <input name="start" placeholder="mm:ss"></label>
        <label>End <input name="end" placeholder="mm:ss"></label></div>
        <button class="button primary" name="task" value="clip_url">Clip it</button></div></div>
        <p id="make-error" role="status"></p></form></section><section class="panel" id="make-progress"></section>`;
    const form = document.querySelector("#make-form");
    form.elements.url.oninput = () => {
      const value = form.elements.url.value.trim();
      const output = document.querySelector("#source-type");
      document.querySelector("#make-error").textContent = "";
      if (!value) { output.textContent = ""; return; }
      let kind;
      try {
        const url = new URL(value);
        const host = url.hostname.replace(/^(www|m)\./, "");
        if (url.protocol === "https:" && !url.username && !url.password && !url.port) {
          if (host === "youtu.be" && /^\/[\w-]{11}\/?$/.test(url.pathname)) kind = "YouTube video";
          if (host === "youtube.com" && /^\/shorts\/[\w-]{11}\/?$/.test(url.pathname)) kind = "YouTube Short";
          if (host === "youtube.com" && /^\/watch\/?$/.test(url.pathname) && /^[\w-]{11}$/.test(url.searchParams.get("v") || "")) kind = "YouTube video";
          if ((host === "clips.twitch.tv" && /^\/[\w-]+\/?$/.test(url.pathname)) || (host === "twitch.tv" && /^\/\w+\/clip\/[\w-]+\/?$/.test(url.pathname))) kind = "Twitch clip";
          if (host === "twitch.tv" && /^\/videos\/\d+\/?$/.test(url.pathname)) kind = "Twitch VOD";
        }
      } catch {}
      output.textContent = kind || "Enter a valid YouTube or Twitch clip or VOD URL.";
    };
    form.onkeydown = (event) => {
      if (event.key !== "Enter" || event.target.tagName !== "INPUT") return;
      event.preventDefault();
      form.requestSubmit(form.querySelector(`[value="${event.target.name === "count" ? "make" : "clip_url"}"]`));
    };
    form.onsubmit = (event) => {
      event.preventDefault();
      const action = event.submitter.value;
      const { count, url, start, end } = form.elements;
      const payload = action === "make" ? { count: Number(count.value) } :
        { url: url.value.trim(), start: start.value, end: end.value };
      const error = document.querySelector("#make-error");
      error.textContent = "";
      runAction(action, null, payload, event.submitter, error);
    };
  }
  document.querySelector("#make-runner").textContent = state.lock.status === "alive" && state.lock.kind === "loop"
    ? `Runs at the next discover boundary. Estimated wait: about ${state.interval}s, plus any remaining time in the current cycle.`
    : "A background worker will run this job."
  updateMakeProgress();
}

function updateMakeProgress() {
  const jobs = state.commands.filter((c) => ["make", "clip_url", "remake_cut", "remake_hook"].includes(c.action));
  const current = jobs.find((c) => c.id === jobId) || jobs[0];
  const items = current ? state.items.filter((i) => i.creation_job === current.id) : [];
  const progress = document.querySelector("#make-progress");
  if (progress.contains(document.activeElement) && document.activeElement.id !== "job-select") return;
  patchMarkup(progress, panelHead("Job progress", current ? (current.result || commandState(current)) : "Start a job to track each clip.",
    jobs.length > 1 ? `<select id="job-select" aria-label="Choose job">${jobs.map((job) =>
      `<option value="${escape(job.id)}" ${job.id === current.id ? "selected" : ""}>${escape(labels[job.action])} · ${formatTime(job.created_at)} · ${commandState(job)}</option>`).join("")}</select>` : "") +
    items.map((item) => {
      const elapsed = Math.max(0, Math.floor((Date.parse(current.completed_at || new Date().toISOString()) - Date.parse(current.created_at)) / 1000));
      return `<div class="health-row"><a href="${itemLink(item.id)}">${escape(item.original_title)}</a>
        <span>${isReviewItem(item) ? `<a href="${reviewLink(item.id)}">Ready for review</a>` : pill(pretty(item.creation_phase || item.stage))}
        · ${elapsed}s ${escape(item.error_message || item.rejection_reason || "")}
        ${item.actions.includes("retry") ? button("retry", item.id, "Retry") : ""}</span></div>`;
    }).join(""));
}

const isReviewItem = (item) => ["awaiting_approval", "rendered", "needs_review"].includes(item.stage);
function relativeAge(time) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(time)) / 60000));
  if (!Number.isFinite(minutes)) return "";
  if (minutes < 60) return minutes ? minutes + "m ago" : "Just now";
  if (minutes < 1440) return Math.floor(minutes / 60) + "h ago";
  return Math.floor(minutes / 1440) + "d ago";
}
function inboxItems() {
  for (const [id, commandId] of inboxPending) {
    const command = state.commands.find((entry) => entry.id === commandId);
    if (command && !["pending", "running"].includes(command.status)) inboxPending.delete(id);
  }
  return state.items.filter((item) => !inboxPending.has(item.id) && (reviewTab === "approve"
    ? (isReviewItem(item) && item.stage !== "needs_review") || item.id === reviewId && ["approved", "queued"].includes(item.stage)
    : item.stage === ({ decision: "needs_review", failed: "failed", rejected: "rejected" })[reviewTab]));
}

function advanceReview(id, commandId) {
  const items = inboxItems();
  const index = items.findIndex((item) => item.id === id);
  reviewId = (items[index + 1] || items[index - 1])?.id;
  inboxPending.set(id, commandId);
}

async function review(force = false) {
  const items = inboxItems();
  if (!document.querySelector("#review-inbox")) {
    content.innerHTML = '<div class="review-toolbar">' + heading("Review", "") +
      reviewTabs() + '<button class="button secondary" id="review-help" aria-label="Keyboard shortcuts">?</button></div><div class="review-layout"><aside id="review-inbox" aria-label="Review inbox"></aside><section id="review-selected"></section></div>' +
      '<dialog id="review-shortcuts"><form method="dialog"><h2>Keyboard shortcuts</h2><p>J/K or arrows · Next / previous</p><p>Space · Play or pause previews</p><dl>' +
      Object.entries(keys).map(([key, action]) => `<dt>${key.toUpperCase()}</dt><dd>${labels[action]}</dd>`).join("") +
      '<dt>1 / 2 / 3</dt><dd>Switch preset on new clips</dd><dt>Z</dt><dd>Undo</dd></dl><button class="button secondary">Close</button></form></dialog>';
    document.querySelector("#review-help").onclick = () => document.querySelector("#review-shortcuts").showModal();
  }
  updateCutRate();
  const inbox = document.querySelector("#review-inbox");
  const pane = document.querySelector("#review-selected");
  const item = items.find((entry) => entry.id === reviewId) || (!reviewId ? items[0] : null);
  if (item) reviewId = item.id;
  const list = items.map((entry) => `<a class="inbox-entry" href="${reviewLink(entry.id)}" data-review-item="${escape(entry.id)}" ${entry.id === reviewId ? 'aria-current="true"' : ""}>
    <img width="64" height="36" loading="lazy" alt="" src="${mediaURL("/media/" + encodeURIComponent(entry.id) + "/thumb")}">
    <span><strong>${escape(entry.hook_text || entry.original_title)}</strong>
    <small title="${escape(formatTime(entry.created_at))}">${escape(relativeAge(entry.created_at))}</small>
    <span class="chips">${pill(entry.authenticity || "unverified")}${entry.stage === "needs_review" ? pill("Needs review", "amber") : ""}${entry.cut_signal ? pill(pretty(entry.cut_signal)) : ""}</span></span></a>`).join("");
  if (inbox.dataset.markup !== list) {
    patchMarkup(inbox, list);
    inbox.dataset.markup = list;
  }
  if (!item && !reviewId) {
    if (!pane.dataset.item && pane.childElementCount) return;
    delete pane.dataset.item;
    history.replaceState(null, "", reviewURL());
    pane.innerHTML = empty('All caught up') + '<a href="/make">Make clips</a>';
    return;
  }
  const actionSignature = JSON.stringify([item?.actions, item?.hook_text, item?.error_message, item?.stage_updated_at]);
  if (pane.dataset.item === reviewId && !force &&
      (pane.querySelector(".inline-editor, .editor[open]") || pane.dataset.actions === actionSignature)) return;
  const requested = reviewId;
  const sequence = ++reviewRequest;
  try {
    const next = await loadDetail(requested);
    if (sequence !== reviewRequest || requested !== reviewId) return;
    if (detail?.item.id !== requested) selectedVersion = undefined;
    detail = next;
    const current = next.item;
    history.replaceState(null, "", reviewURL(requested));
    pane.dataset.item = requested;
    pane.dataset.actions = actionSignature;
    const versions = versionPanel(next);
    patchMarkup(pane, `${versions}
      <section class="panel"><h2>${escape(current.original_title)}</h2><div class="hook-display">${escape(current.hook_text)}</div>
      <p>${escape(current.voiceover_script)}</p>
      ${current.error_message || current.rejection_reason || next.review.reason ? `<p class="reason">${escape(current.error_message || current.rejection_reason || next.review.reason)}</p>` : ""}
      <p><a href="${itemLink(requested)}">Open item details</a></p>
      ${next.media.cut ? `<details><summary>Watch the cut</summary><video controls preload="none" src="${mediaURL("/media/" + encodeURIComponent(requested) + "/cut")}"></video></details>` : ""}
      <h3>Post text (Instagram caption)</h3><div class="caption-preview">${escape(next.caption)}</div></section>
      <div class="detail-actions review-actions">${approvalButtons(next)}${["approve", "aonly", "bonly", "hookok", "reject", "remake_cut", "remake_hook", "edit_hook", "edit_caption", "retry", "delete"]
        .filter((action) => next.actions.includes(action))
        .map((action) => {
          const shortcut = Object.keys(keys).find((key) => keys[key] === action);
          return button(action, requested, labels[action] + (shortcut ? " [" + shortcut.toUpperCase() + "]" : ""));
        }).join("")}</div>`);
  } catch (error) {
    if (sequence === reviewRequest) pane.textContent = error.message;
  }
}

document.addEventListener("click", (event) => {
  const entry = event.target.closest("[data-review-item]");
  if (!entry || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  reviewId = entry.dataset.reviewItem;
  review(true);
});

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat ||
      event.target.closest?.("input, textarea, select, [contenteditable], .inline-editor") || document.querySelector("dialog[open]")) return;
  const key = event.key.toLowerCase();
  if (key === "z" && undoHistory.length) { event.preventDefault(); undoLast(); return; }
  if (!isReviewPage()) return;
  if (["j", "k", "arrowdown", "arrowup", "arrowright", "arrowleft"].includes(key)) {
    event.preventDefault();
    const items = inboxItems();
    const index = items.findIndex((item) => item.id === reviewId);
    const step = ["j", "arrowdown", "arrowright"].includes(key) ? 1 : -1;
    if (items[index + step]) { reviewId = items[index + step].id; review(true); }
  } else if (key === " " && !event.target.closest?.("button, a")) {
    event.preventDefault();
    event.stopPropagation();
    const players = [...document.querySelectorAll("#review-selected video.phone-preview")];
    const play = players.every((player) => player.paused);
    for (const player of players) play ? player.play().catch(() => {}) : player.pause();
  } else if (key === "?") {
    document.querySelector("#review-shortcuts")?.showModal();
  } else if (/^[123]$/.test(key) && detailVersions(detail || { item: {} }).some(({ variant }) => variant.startsWith("p"))) {
    const control = document.querySelector(`#review-selected [data-version="p${key}"]`);
    if (control) { event.preventDefault(); control.click(); }
  } else if (keys[key]) {
    const control = document.querySelector(`#review-selected [data-action="${keys[key]}"]`) || (key === "a" ? document.querySelector('#review-selected [data-extra="approve_versions"]') : null);
    if (control && !control.disabled) { event.preventDefault(); control.click(); }
  }
}, { capture: true });

function updateLibraryFilter(field) {
  const query = new URLSearchParams(location.search);
  if (["from", "to"].includes(field.name)) document.querySelector("#library-range").value = "";
  if (field.value) query.set(field.name, field.value);
  else query.delete(field.name);
  query.delete("page");
  history.replaceState(null, "", "/library?" + query);
  library();
}

function updateBulkBar() {
  const bar = document.querySelector("#library-bulk");
  bar.hidden = !selectedItems.size;
  document.querySelector("#selected-count").textContent = selectedItems.size + " selected";
  for (const button of bar.querySelectorAll("[data-bulk]")) {
    button.disabled = ![...selectedItems].some((id) => itemById(id)?.actions.includes(button.dataset.bulk));
  }
}

async function library() {
  const query = new URLSearchParams(location.search);
  const value = (name) => query.get(name) || "";
  const platform = (item) => item.source_platform || item.source;
  const select = (name, label, options) => `<label>${label}<select data-library-filter name="${name}">
    ${options.map(([key, text]) => `<option value="${escape(key)}" ${key === value(name) ? "selected" : ""}>${escape(text)}</option>`).join("")}</select></label>`;
  const choices = (values) => [...new Set(values.filter(Boolean))].sort().map((entry) => [entry, pretty(entry)]);
  if (!document.querySelector("#library-results")) {
    content.innerHTML = heading("Library", "") + `<div class="toolbar library-filters">
      <label>Search<input type="search" data-library-filter name="q" value="${escape(value("q"))}"></label>
      ${select("stage", "Stage", [["", "Any"], ...choices(["rejected", ...state.stages.map((stage) => stage.stage)])])}
      ${select("platform", "Platform", [["", "Any"], ...choices(["twitch", "youtube", ...state.items.map(platform)])])}
      ${select("authenticity", "Source status", [["", "Any"], ["official", "Official"], ["unverified", "Unverified"]])}
      ${select("signal", "Cut signal", [["", "Any"], ...choices(state.items.map((item) => item.cut_signal))])}
      ${select("posted", "Posted", [["", "Any"], ["yes", "Posted"], ["no", "Unposted"]])}
      <label>Date range<select id="library-range"><option value="">Custom dates</option>
      <option value="last-week">Last week</option><option value="all">All dates</option></select></label>
      <label>From<input type="date" data-library-filter name="from" value="${escape(value("from"))}"></label>
      <label>To<input type="date" data-library-filter name="to" value="${escape(value("to"))}"></label>
      ${select("size", "Per page", [["", "25"], ["50", "50"], ["100", "100"]])}</div>
      <div class="chips library-quick-filters" aria-label="Quick filters">
      <button class="button secondary" data-library-chip="stage" data-value="rejected">Rejected</button>
      <button class="button secondary" data-library-chip="platform" data-value="twitch">Twitch</button>
      <button class="button secondary" data-library-chip="range" data-value="last-week">Last week</button></div>
      <section class="panel" id="library-results"></section>
      <div id="library-bulk" class="library-bulk" hidden><span id="selected-count"></span>
      <button class="button secondary" data-bulk="reject">Reject</button>
      <button class="button danger" data-bulk="delete">Delete</button>
      <button class="button secondary" id="clear-selection">Clear</button></div>`;
    document.querySelector("#library-range").onchange = (event) => {
      const range = event.target.value;
      if (!range) return;
      const params = new URLSearchParams(location.search);
      const from = document.querySelector('[name="from"]');
      const to = document.querySelector('[name="to"]');
      if (range === "last-week") {
        const end = new Date(dayKey(new Date().toISOString()) + "T12:00:00Z");
        end.setUTCDate(end.getUTCDate() - (end.getUTCDay() || 7));
        const start = new Date(end);
        start.setUTCDate(start.getUTCDate() - 6);
        from.value = start.toISOString().slice(0, 10);
        to.value = end.toISOString().slice(0, 10);
        params.set("from", from.value);
        params.set("to", to.value);
      } else {
        from.value = to.value = "";
        params.delete("from");
        params.delete("to");
      }
      params.delete("page");
      history.replaceState(null, "", "/library?" + params);
      library();
    };
    for (const chip of document.querySelectorAll("[data-library-chip]")) {
      chip.onclick = () => {
        const name = chip.dataset.libraryChip;
        const field = name === "range" ? document.querySelector("#library-range") : document.querySelector(`[name="${name}"]`);
        field.value = chip.getAttribute("aria-pressed") === "true" ? (name === "range" ? "all" : "") : chip.dataset.value;
        field.dispatchEvent(new Event("change", { bubbles: true }));
      };
    }
    document.querySelector("#clear-selection").onclick = () => { selectedItems.clear(); libraryAnchor = null; library(); };
  }
  for (const chip of document.querySelectorAll("[data-library-chip]")) {
    const name = chip.dataset.libraryChip;
    const value = name === "range" ? document.querySelector("#library-range").value : query.get(name);
    chip.setAttribute("aria-pressed", String(value === chip.dataset.value));
  }
  const sequence = ++libraryRequest;
  let result;
  try {
    result = await api("/api/library?" + query);
  } catch (error) {
    if (sequence === libraryRequest) toast(error.message, { error: true });
    return;
  }
  if (sequence !== libraryRequest) return;
  const { page, pages, total } = result;
  const items = result.ids.map(itemById).filter(Boolean);
  const pageLink = (target) => {
    const params = new URLSearchParams(query);
    params.set("page", target);
    return "/library?" + escape(params.toString());
  };
  patchMarkup(document.querySelector("#library-results"), `<div class="toolbar">
      <span>${total} matching · Page ${page} of ${pages}</span>
      ${page > 1 ? `<a data-library-page href="${pageLink(page - 1)}">Previous</a>` : ""}
      ${page < pages ? `<a data-library-page href="${pageLink(page + 1)}">Next</a>` : ""}</div>
    <div class="table-wrap"><table class="data-table"><thead><tr><th scope="col">Select</th><th scope="col">Clip</th><th scope="col">Source</th><th scope="col">Stage</th></tr></thead>
    <tbody>${items.map((item) => `<tr data-row="${escape(item.id)}"><td>${item.actions.some((action) => ["delete", "reject"].includes(action)) ?
      `<input type="checkbox" data-select-item value="${escape(item.id)}" aria-label="Select ${escape(item.original_title)}" ${selectedItems.has(item.id) ? "checked" : ""}>` : ""}
    </td><td><a class="library-clip" href="${itemLink(item.id)}"><img class="library-thumb" src="${mediaURL("/media/" + encodeURIComponent(item.id) + "/thumb")}" alt="" loading="lazy" width="64" height="36">${escape(item.original_title)}</a></td>
    <td>${escape(item.author_name)} · ${escape(platform(item))}</td><td>${pill(pretty(item.stage))}</td></tr>`).join("")}</tbody></table></div>`);
  updateBulkBar();
}

document.addEventListener("click", (event) => {
  if (event.target.matches("[data-select-item]")) {
    const rows = [...document.querySelectorAll("[data-select-item]")];
    const item = event.target;
    const index = rows.indexOf(item);
    const anchor = rows.findIndex((row) => row.value === libraryAnchor);
    const range = event.shiftKey && anchor >= 0 ? rows.slice(Math.min(anchor, index), Math.max(anchor, index) + 1) : [item];
    for (const row of range) {
      row.checked = item.checked;
      if (item.checked) selectedItems.add(row.value);
      else selectedItems.delete(row.value);
    }
    libraryAnchor = item.value;
    updateBulkBar();
  }
  const link = event.target.closest("[data-library-page]");
  if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    history.replaceState(null, "", link.href);
    library();
  }
});

async function bulkAction(action) {
  const ids = [...selectedItems].filter((id) => itemById(id)?.actions.includes(action));
  if (!ids.length) { toast("Select items eligible for that action.", { error: true }); return; }
  const count = `${ids.length} ${ids.length === 1 ? "clip" : "clips"}`;
  const question = action === "delete"
    ? `Deletes the video files for ${count} for good. The ${ids.length === 1 ? "item stays" : "items stay"} in history so discovery never picks ${ids.length === 1 ? "this clip" : "these clips"} again. This can't be undone.`
    : `${labels[action]} for ${count}?`;
  const bulkDialog = document.querySelector("#bulk-dialog");
  document.querySelector("#bulk-title").textContent = action === "delete" ? "Delete selected clips" : "Reject selected clips";
  document.querySelector("#bulk-description").textContent = question;
  bulkDialog.returnValue = "";
  if (action === "delete") {
    bulkDialog.showModal();
    await new Promise((resolve) => bulkDialog.addEventListener("close", resolve, { once: true }));
    if (bulkDialog.returnValue !== "confirm") return;
  }
  const counts = {};
  for (const item_id of ids) {
    try {
      const { ticket } = await api("/api/prepare", { action, item_id });
      const { command } = await api("/api/confirm", { ticket });
      const status = commandState(command);
      const title = itemById(item_id)?.original_title || item_id;
      if (["refused", "failed"].includes(status)) commandNotice(action, command, title);
      if (status === "queued") waitingCommands.set(command.id, { title, action });
      counts[status] = (counts[status] || 0) + 1;
      if (["applied", "queued"].includes(status)) selectedItems.delete(item_id);
    } catch (error) {
      toast(actionError(action, itemById(item_id)?.original_title || item_id, error.message), { error: true });
    }
  }
  const successes = Object.entries(counts).filter(([status]) => ["applied", "queued"].includes(status));
  if (successes.length) toast(successes.map(([status, n]) => `${n} ${status === "applied" ? (action === "delete" ? "deleted" : "rejected") : "queued"}`).join(" · "));
  await refresh(true);
}

async function changeRunner(operation) {
  const stage = state.lock.stage || "starting";
  if (busyActions.has("runner")) return;
  busyActions.add("runner");
  try {
    const result = await api("/api/runner", { operation, stage });
    toast(result.message);
    await refresh(true);
  } catch (error) {
    toast(error.message, { error: true });
  } finally {
    busyActions.delete("runner");
  }
}

function settingsForm() {
  const flags = { instagram: "Instagram posting", facebook: "Facebook posting", telegram: "Telegram approval cards",
    youtube: "YouTube discovery", twitch: "Twitch discovery" };
  return heading("Settings", "Changes apply at the start of the runner’s next cycle.") +
    `<form id="settings-form" class="panel commands">
    ${panelHead("Posting and discovery", "Times use each profile’s configured timezone. Separate multiple slots with commas.")}
    <div class="grid-half">${Object.entries(preferences.slots).map(([name, slots]) =>
      `<label>Posting slots · ${escape(name)}<input data-profile="${escape(name)}" value="${escape(slots.join(", "))}" required></label>`).join("")}</div>
    <div class="settings-switches">${Object.entries(flags).map(([key, text]) =>
      `<label><input type="checkbox" name="${key}" ${preferences[key] ? "checked" : ""}> ${text}</label>`).join("")}</div>
    <div class="grid-half"><label>Ready buffer<input type="number" name="buffer" min="1" max="50" value="${preferences.buffer}" required></label>
    <label>Official-channel weight<input type="number" name="official_weight" min="0" max="5" step="any" value="${preferences.official_weight}" required></label>
    <label>Heatmap weight<input type="number" name="heatmap_weight" min="0" max="2" step="any" value="${preferences.heatmap_weight}" required></label></div>
    <dl class="settings-status"><dt>Dry run</dt><dd>${pill(state.dry_run ? "Enabled" : "Disabled", state.dry_run ? "amber" : "gray")}</dd></dl>
    <div class="detail-actions"><button class="button primary">Save settings</button></div>
    </form>`;
}

document.addEventListener("submit", async (event) => {
  if (event.target.id !== "settings-form") return;
  event.preventDefault();
  const form = event.target;
  const submit = event.submitter;
  if (submit.disabled) return;
  submit.disabled = true;
  submit.classList.add("working");
  const slots = Object.fromEntries([...form.querySelectorAll("[data-profile]")].map((field) =>
    [field.dataset.profile, field.value.split(",").map((s) => s.trim())]));
  const values = { slots };
  for (const field of form.querySelectorAll("[name]"))
    values[field.name] = field.type === "checkbox" ? field.checked : Number(field.value);
  try {
    const result = await api("/api/settings", values);
    toast(result.message);
    await refresh(true);
  } catch (error) {
    toast(error.message, { error: true });
  } finally {
    submit.disabled = false;
    submit.classList.remove("working");
  }
});

setInterval(async () => {
  if (location.pathname !== "/make" || document.hidden || !document.querySelector("#make-progress")) return;
  try {
    state = await api("/api/state");
    showCompletedControls();
    updateRunnerLabel();
    updateMakeProgress();
  } catch (error) {
    document.querySelector("#make-runner").textContent = error.message;
  }
}, 2500);
