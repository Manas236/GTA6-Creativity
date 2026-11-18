"use strict";

const mockMode = new URLSearchParams(location.search).get("mock") === "1";
const themePicker = document.querySelector("#theme");
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  themePicker.value = theme;
  try { localStorage.setItem("dashboard-theme", theme); } catch {}
}
try { applyTheme(localStorage.getItem("dashboard-theme") || "light"); } catch { applyTheme("light"); }
function redrawTheme() {
  if (location.pathname === "/analytics" && typeof state !== "undefined" && state) analytics();
}
themePicker.addEventListener("change", () => { applyTheme(themePicker.value); redrawTheme(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawTheme);

const versionNames = { p1: "P1 Clean", p2: "P2 Punchy", p3: "P3 Full hype", a: "Captions version", b: "Voiceover version" };
const versionName = (value) => versionNames[String(value).toLowerCase()] || "Version selected at posting";
function localLink(path) {
  if (!mockMode) return path;
  const url = new URL(path, location.origin);
  url.searchParams.set("mock", "1");
  return url.pathname + url.search + url.hash;
}
document.addEventListener("click", (event) => {
  const link = event.target.closest("a[href^='/']");
  if (link && mockMode) link.href = localLink(link.getAttribute("href"));
}, true);

function mediaURL(path) {
  if (!mockMode) return path;
  return path.endsWith("/thumb") ? "/static/thumbnail.svg" : "/static/mock-preview.mp4";
}
function fromLink(fallback = "/library") {
  const from = new URLSearchParams(location.search).get("from");
  return localLink(/^\/(?:library|review|failures|queue|analytics|make)?(?:\?|$)/.test(from || "") ? from : fallback);
}

function nodeKey(node) {
  return node.nodeType === 1 ? node.id || node.dataset.row || node.dataset.queueItem || node.dataset.reviewItem || "" : "";
}

// Keep existing controls and media elements alive when adjacent data changes.
function patchMarkup(target, markup) {
  const template = document.createElement("template");
  template.innerHTML = markup;
  function patch(parent, source) {
    if (parent.classList?.contains("reslot-host") && parent.firstChild) return;
    let cursor = parent.firstChild;
    for (const next of source.childNodes) {
      const key = nodeKey(next);
      const match = key ? [...parent.childNodes].find((node) => nodeKey(node) === key) : cursor;
      if (!match || nodeKey(match) !== key || match.nodeName !== next.nodeName) {
        parent.insertBefore(next.cloneNode(true), cursor);
        continue;
      }
      if (match !== cursor) parent.insertBefore(match, cursor);
      if (next.nodeType !== 1) {
        if (match.nodeValue !== next.nodeValue) match.nodeValue = next.nodeValue;
      } else if (!match.matches("video, .inline-editor, .editor[open]") && !match.contains(document.activeElement)) {
        for (const { name } of [...match.attributes]) {
          if (name !== "open" && !next.hasAttribute(name)) match.removeAttribute(name);
        }
        for (const { name, value } of next.attributes) {
          if (match.getAttribute(name) !== value) match.setAttribute(name, value);
        }
        patch(match, next);
      } else if (!match.matches("video, .inline-editor, .editor[open], input, textarea, select")) {
        patch(match, next);
      }
      cursor = match.nextSibling;
    }
    while (cursor) {
      const following = cursor.nextSibling;
      cursor.remove();
      cursor = following;
    }
  }
  patch(target, template.content);
}

const unavailable = new Set();
const isUnavailable = (error) => error.status === 404 || /unknown action/i.test(error.message);
async function optionalGet(url) {
  const key = url.split("?")[0];
  if (unavailable.has(key)) return null;
  try { return await api(url); }
  catch (error) {
    if (!isUnavailable(error)) throw error;
    unavailable.add(key);
    return null;
  }
}
const actionChecks = new Map();
async function actionAvailable(action, id = null, payload = {}) {
  if (unavailable.has(action)) return false;
  if (!actionChecks.has(action)) {
    actionChecks.set(action, api("/api/prepare", { action, item_id: id, payload }).then(() => true, (error) => {
      if (isUnavailable(error)) unavailable.add(action);
      return !isUnavailable(error);
    }));
  }
  return actionChecks.get(action);
}
function extendedButton(action, id, text, payload = {}) {
  return unavailable.has(action) ? "" : `<button class="button secondary" data-extra="${action}" data-id="${escape(id)}" data-payload="${escape(JSON.stringify(payload))}">${escape(text)}</button>`;
}

let selectedVersion;
function detailVersions(data) {
  if (data.versions?.length) return data.versions.filter((version) => versionNames[version.variant]);
  return ["a", "b"].filter((key) => data.media?.[key]).map((variant) => ({
    variant, url: "/media/" + encodeURIComponent(data.item.id) + "/" + variant, meta: {},
  }));
}
function versionPanel(data) {
  const versions = detailVersions(data);
  if (!versions.length) return empty("Render previews will appear when available.");
  const variant = versions.find((entry) => entry.variant === selectedVersion) || versions.find((entry) => entry.variant === "p2") || versions[0];
  selectedVersion = variant.variant;
  return `<section class="panel version-panel" data-row="${escape(data.item.id)}">
    <div class="tabs" aria-label="Render versions">${versions.map(({ variant }) => `<button class="button secondary" data-version="${variant}" aria-pressed="${variant === selectedVersion}">${versionName(variant)}</button>`).join("")}</div>
    <div class="version-body"><video class="phone-preview" controls preload="metadata" aria-label="${versionName(selectedVersion)}" ${mockMode ? 'poster="/static/thumbnail.svg"' : ""} src="${escape(mediaURL(variant.url))}"></video>
    <div id="render-meta">${versionMeta(data, variant)}</div></div>
  </section>`;
}
function versionMeta(data, variant) {
  const meta = variant.meta || {};
  const words = meta.caption_words || [];
  const warning = preferences?.look?.hook?.warn_below ?? 130;
  const id = data.item.id;
  const actions = data.actions || [];
  return (meta.preset ? `<h3>How it was made</h3><div class="chips">
      ${pill(versionName(variant.variant))}${meta.hook_font_px == null ? "" : pill(`Hook · ${meta.hook_font_px} px${meta.hook_font_px < warning ? " · below warning threshold" : ""}`, meta.hook_font_px < warning ? "amber" : "")}
      ${(meta.sfx_cues || []).map(({ t, category }) => pill(`${t}s · ${category}`)).join("")}
      ${pill(meta.music_track || "Music off")}${pill(`${words.length} caption words`)}</div>` : "") +
    (actions.includes("edit_caption_words") && words.length ? `<details class="editor"><summary>Captions in video</summary>
      <form data-extra-form="edit_caption_words" data-id="${escape(id)}"><div class="caption-words">${words.map(({ i, text, start, end }) => `<label data-row="${i}"><span>${start}s–${end}s</span><input aria-label="Word ${Number(i) + 1}" data-original="${escape(text)}" name="${i}" value="${escape(text)}" required></label>`).join("")}</div><button class="button secondary">Save caption words</button></form></details>` : "") +
    (actions.includes("edit_voiceover") && data.voiceover ? `<details class="editor"><summary>Voiceover</summary><form data-extra-form="edit_voiceover" data-id="${escape(id)}">
      <label>Script<textarea name="script" required>${escape(data.voiceover.script)}</textarea></label><label>Voice<input name="voice" value="${escape(data.voiceover.voice)}"></label>
      <label>Speed<input name="speed" type="number" step="any" min="0.1" value="${escape(data.voiceover.speed)}"></label>
      <button class="button secondary">Save voiceover</button></form></details>` : "");
}
function approvalButtons(data) {
  if (!detailVersions(data).some(({ variant }) => variant.startsWith("p")) || !data.actions.includes("approve_versions")) return "";
  return extendedButton("approve_versions", data.item.id, "Approve this version", { variants: [selectedVersion] }) +
    extendedButton("approve_versions", data.item.id, "Approve all", { variants: detailVersions(data).map(({ variant }) => variant) });
}
document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-version]");
  if (control && detail) {
    const panel = control.closest(".version-panel");
    const variant = detailVersions(detail).find((entry) => entry.variant === control.dataset.version);
    if (!variant || variant.variant === selectedVersion) return;
    selectedVersion = variant.variant;
    const video = panel.querySelector("video");
    if (video) {
      const time = video.currentTime;
      const paused = video.paused;
      video.onloadedmetadata = () => {
        video.currentTime = Math.min(time, video.duration || time);
        if (!paused) video.play().catch(() => {});
      };
      video.src = mediaURL(variant.url);
      video.setAttribute("aria-label", versionName(selectedVersion));
    }
    for (const tab of panel.querySelectorAll("[data-version]")) tab.setAttribute("aria-pressed", String(tab === control));
    panel.querySelector("#render-meta").innerHTML = versionMeta(detail, variant);
    const approve = document.querySelector('[data-extra="approve_versions"]');
    if (approve) approve.dataset.payload = JSON.stringify({ variants: [selectedVersion] });
  }
  const extra = event.target.closest("[data-extra]");
  if (!extra) return;
  const { extra: action, id, payload } = extra.dataset;
  if (["go_live", "go_dry"].includes(action)) openAction(action, id, extra, JSON.parse(payload));
  else runAction(action, id, JSON.parse(payload), extra);
});
document.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-extra-form]");
  if (!form) return;
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  const action = form.dataset.extraForm;
  const payload = action === "edit_caption_words"
    ? { words: [...form.querySelectorAll("[data-original]")].filter((field) => field.value !== field.dataset.original).map((field) => ({ i: Number(field.name), text: field.value })) }
    : { script: data.script, ...(data.voice ? { voice: data.voice } : {}), ...(data.speed ? { speed: Number(data.speed) } : {}) };
  if (payload.words && !payload.words.length) return;
  runAction(action, form.dataset.id, payload, event.submitter);
});

const contractActions = new Set(["approve_versions", "edit_caption_words", "edit_voiceover", "move_to_slot",
  "snapshot_now", "run_stage", "make_from_discovered", "go_live", "go_dry"]);
async function loadDetail(id) {
  const data = await api("/api/items/" + encodeURIComponent(id));
  const versions = detailVersions(data).filter(({ variant }) => variant.startsWith("p"));
  const captions = versions.flatMap(({ meta }) => meta?.caption_words || []);
  const editable = versions.length && !data.item.preview_only && ["awaiting_approval", "rendered", "needs_review"].includes(data.item.stage);
  const candidates = [
    [editable && data.item.stage !== "needs_review", "approve_versions", { variants: [versions[0]?.variant] }],
    [editable && captions.length, "edit_caption_words", { words: captions.slice(0, 1).map(({ i, text }) => ({ i, text })) }],
    [editable && data.voiceover, "edit_voiceover", { script: data.voiceover?.script }],
    [data.posts?.some((post) => post.remote_url), "snapshot_now", {}],
  ];
  await Promise.all(candidates.map(async ([applicable, action, payload]) => {
    if (applicable && !data.actions.includes(action) && await actionAvailable(action, id, payload)) data.actions.push(action);
  }));
  if (versions.length && !preferences) preferences = await optionalGet("/api/settings");
  return data;
}

function postedDetails(data) {
  const posts = data.posts?.filter((post) => post.remote_url || post.posted_at || post.metrics?.length);
  if (!posts?.length) return "";
  const id = data.item.id;
  return `<section class="panel commands"><h2>Published posts</h2>
    ${data.actions.includes("snapshot_now") ? extendedButton("snapshot_now", id, "Snapshot now") : ""}
    ${posts.map((post) => `<article><h3>${escape(pretty(post.platform))} · ${escape(versionName(post.variant))}</h3>
      ${/^https?:\/\//i.test(post.remote_url || "") ? `<a class="button secondary" target="_blank" rel="noopener noreferrer" href="${escape(post.remote_url)}">Open Reel ↗</a>` : ""}
      ${post.metrics?.length ? metricTable(post.metrics) : "<p>No snapshots recorded.</p>"}
      ${post.missed_windows?.length ? `<p>Missed windows · ${post.missed_windows.map(Number).join(", ")} h</p>` : ""}
      </article>`).join("")}</section>`;
}

const reviewLabels = { approve: "To approve", decision: "Needs a decision", failed: "Failed", rejected: "Rejected" };
const isReviewPage = () => ["/review", "/failures"].includes(location.pathname);
const requestedTab = new URLSearchParams(location.search).get("tab");
let reviewTab = reviewLabels[requestedTab] ? requestedTab : location.pathname === "/failures" ? "failed" : "approve";
function reviewURL(id) {
  const query = new URLSearchParams();
  if (id) query.set("item", id);
  if (reviewTab !== "approve") query.set("tab", reviewTab);
  return localLink("/review" + (query.size ? "?" + query : ""));
}
function reviewTabs() {
  return `<div class="tabs" aria-label="Review inbox tabs">${Object.entries(reviewLabels).map(([tab, text]) =>
    `<a href="${localLink("/review?tab=" + tab)}" class="button secondary" aria-current="${tab === reviewTab ? "page" : "false"}">${text}</a>`).join("")}</div><div id="cut-rate"></div>`;
}
function updateCutRate() {
  const stats = state.cut_stats;
  if (reviewTab !== "failed" || !stats) return;
  patchMarkup(document.querySelector("#cut-rate"), `<p class="notice">No clean ending · ${Math.round(stats.rate * 100)}% · ${stats.no_clean_ending} of ${stats.attempts} cut attempts in the last 24 hours</p>`);
}

let weekSlots = null;
let weekOffset = 0;
const calendarDay = (offset) => {
  const date = new Date(dayKey(new Date().toISOString()) + "T12:00:00Z");
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
};
async function updateSchedule() {
  const result = await optionalGet("/api/schedule?from=" + calendarDay(weekOffset) + "&days=7");
  weekSlots = result?.slots ?? null;
  const emptySlot = weekSlots?.find(({ item_id }) => !item_id);
  const clip = queueItems()[0]?.item;
  if (emptySlot && clip) await actionAvailable("move_to_slot", clip.id, { at: emptySlot.at });
}
function calendarMarkup() {
  if (!weekSlots || queueCompact) return "";
  const controls = '<div class="toolbar"><button class="button secondary" data-week="-7">Previous week</button><button class="button secondary" data-week="7">Next week</button></div>';
  return `<section class="panel commands"><h2>Week calendar</h2>${controls}<div class="week-wrap"><div class="week">
    ${Array.from({ length: 7 }, (_, index) => {
      const day = calendarDay(weekOffset + index);
      const heading = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).format(new Date(day + "T12:00Z"));
      const slots = weekSlots.filter(({ at }) => dayKey(at) === day);
      return `<section><h3>${heading}</h3>${slots.map((slot) => {
        const time = formatTime(slot.at, { day: undefined, month: undefined });
        return `<article class="calendar-slot" data-row="${escape(slot.at)}" ${slot.item_id || unavailable.has("move_to_slot") ? "" : `data-slot="${escape(slot.at)}"`}>
          <time>${escape(time)}</time>${slot.item_id ? `<a href="${itemLink(slot.item_id)}">${escape(slot.title || slot.item_id)}</a>` : '<span class="muted">Empty slot</span>'}</article>`;
      }).join("") || '<p class="muted">No slots</p>'}</section>`;
    }).join("")}</div></div></section>`;
}
document.addEventListener("click", async (event) => {
  const control = event.target.closest("[data-week]");
  if (!control) return;
  weekOffset += Number(control.dataset.week);
  try { await updateSchedule(); queue(); }
  catch (error) { toast(error.message, { error: true }); }
});
document.addEventListener("dragover", (event) => {
  const slot = event.target.closest("[data-slot]");
  if (!slot || !draggedItem || itemById(draggedItem)?.stage !== "approved") return;
  event.preventDefault();
  document.querySelector(".drop-target")?.classList.remove("drop-target");
  slot.classList.add("drop-target");
});
document.addEventListener("drop", (event) => {
  const slot = event.target.closest("[data-slot]");
  if (!slot || !draggedItem || itemById(draggedItem)?.stage !== "approved") return;
  event.preventDefault();
  runAction("move_to_slot", draggedItem, { at: slot.dataset.slot });
});

let logsChecked = false;
let logsPaused = false;
async function renderLogs() {
  if (logsPaused) return;
  const logs = await optionalGet("/api/logs?lines=200");
  if (!logs) { overview(); return; }
  const stages = (await Promise.all(["analytics", "cleanup"].map(async (stage) =>
    await actionAvailable("run_stage", null, { stage }) ? extendedButton("run_stage", "", `Run ${stage} now`, { stage }) : ""))).join("");
  patchMarkup(content, heading("Logs", "", stages + '<button class="button secondary" id="pause-logs">Pause</button>') +
    `<pre id="log-lines" tabindex="0">${escape(logs.lines.join("\n") || "No log lines recorded.")}</pre>`);
  const view = document.querySelector("#log-lines");
  view.scrollTop = view.scrollHeight;
}
document.addEventListener("click", (event) => {
  if (event.target.id !== "pause-logs") return;
  logsPaused = !logsPaused;
  event.target.textContent = logsPaused ? "Resume" : "Pause";
  if (!logsPaused) renderLogs().catch((error) => toast(error.message, { error: true }));
});
async function discovered() {
  const items = await optionalGet("/api/discovered");
  if (!items) return;
  const canMake = items.length && await actionAvailable("make_from_discovered", items[0].item_id);
  let section = document.querySelector("#discovered");
  if (!section) {
    section = document.createElement("section");
    section.id = "discovered";
    section.className = "panel commands";
    content.append(section);
  }
  patchMarkup(section, `<h2>Discovered</h2>${items.map((item) => `<div class="todo-row" data-row="${escape(item.item_id)}">
      <div><strong>${escape(item.title)}</strong><p class="muted">${escape(item.source)} · ${number(item.age_h)} h old · score ${number(item.score)}</p></div>
      ${canMake ? extendedButton("make_from_discovered", item.item_id, "Make this one") : ""}</div>`).join("") || empty("No discovered clips are waiting.")}`);
}

const settingsLabels = {
  posting: "Posting", look: "Look and sound", sources: "Sources", approval: "Approval",
  voiceover: "Voiceover", retention: "Retention", connections: "Connections",
};
const settingFields = {
  posting: { profile: "Profile", min_gap_hours: "Minimum gap (hours)", daily_post_target: "Posts per day", "platforms.instagram": "Instagram", "platforms.facebook": "Facebook", hashtags: "Hashtags" },
  look: { versions: "Versions to render", "sfx.enabled_categories": "SFX categories", "sfx.max_per_clip": "Maximum cues per clip", "sfx.min_gap_s": "Minimum gap between cues (seconds)", "music.enabled": "Music", "hook.font_size": "Hook size (px)", "hook.min_font_size": "Hook floor (px)", "hook.warn_below": "Hook warning threshold (px)", "caption.font_size": "Caption size (px)" },
  sources: { "youtube.enabled": "YouTube", "youtube.queries": "YouTube queries", "twitch.enabled": "Twitch", "twitch.enable_from": "Twitch start date", "twitch.lookback_hours": "Twitch lookback (hours)", "twitch.min_views": "Twitch minimum views", official_weight: "Official source weight", official_channels: "Official channels", heatmap_weight: "Heatmap weight", max_source_minutes: "Maximum source length (minutes)" },
  approval: { telegram: "Telegram approvals", switch_to_auto_after_days: "Switch to automatic approval after (days)", auto_quality_gate: "Automatic approval quality gate" },
  voiceover: { voice: "Voice", speed: "Speed" },
  retention: { posted_days: "Keep posted clips (days)", failed_days: "Keep failed clips (days)" },
};
const settingQuery = new URLSearchParams(location.search).get("group");
const settingsGroup = location.pathname === "/connections" ? "connections" :
  Object.hasOwn(settingsLabels, settingQuery) ? settingQuery : "posting";
function settingsTabs() {
  const groups = Object.entries(settingsLabels).filter(([group]) => group === "connections" || preferences?.[group]);
  if (!preferences?.posting) groups.unshift(["posting", "Posting"]);
  return heading("Settings", "") + `<div class="tabs" aria-label="Settings sections">${groups.map(([group, text]) =>
    `<a class="button secondary" href="${localLink("/settings?group=" + group)}" aria-current="${settingsGroup === group ? "page" : "false"}">${text}</a>`).join("")}</div>`;
}
function settingValue(group, path) {
  return path.split(".").reduce((node, key) => node?.[key], group);
}
function settingField(path, label, value) {
  const name = escape(path);
  if (path === "profile") return `<label>${label}<select name="${name}">${["local", "server"].map((option) => `<option ${value === option ? "selected" : ""}>${option}</option>`).join("")}</select></label>`;
  if (path === "versions") return `<fieldset><legend>${label}</legend>${["P1", "P2", "P3"].map((preset) => `<label class="check-field"><input data-preset type="checkbox" value="${preset}" ${value?.includes(preset) ? "checked" : ""}>${versionName(preset)}</label>`).join("")}</fieldset>`;
  if (typeof value === "boolean") return `<label class="check-field"><input name="${name}" type="checkbox" ${value ? "checked" : ""}> ${label}</label>`;
  if (Array.isArray(value)) return `<label>${label}<textarea name="${name}" data-list aria-describedby="lines-tip">${escape(value.join("\n"))}</textarea></label>`;
  const type = path === "twitch.enable_from" ? "date" : typeof value === "number" ? "number" : "text";
  const startDate = path === "twitch.enable_from" && value
    ? `<small>Turns on automatically on ${escape(new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }).format(new Date(value + "T00:00Z")))}</small>` : "";
  return `<label>${label}<input name="${name}" type="${type}" ${type === "number" ? 'step="any" required' : ""} value="${escape(value)}">${startDate}</label>`;
}
function slotChip(value = "") {
  return `<span class="slot-chip"><input type="time" aria-label="Posting time" value="${escape(value)}" required><button type="button" class="icon-button" data-remove-slot aria-label="Remove posting time">×</button></span>`;
}
function groupForm() {
  const group = preferences[settingsGroup];
  const slots = Object.entries(group.slots || {}).map(([profile, times]) =>
    `<fieldset data-slot-profile="${escape(profile)}"><legend>${escape(pretty(profile))} posting slots</legend><div class="slot-chips">${times.map(slotChip).join("")}</div><button type="button" class="button secondary" data-add-slot>Add time</button></fieldset>`).join("");
  const templateSources = [...new Set([...Object.keys(group.caption_templates || {}), "youtube", "twitch"])];
  const templates = settingsGroup === "posting" ? `<fieldset><legend>Caption templates</legend>${templateSources.map((source) =>
    `<label>${escape(pretty(source))}<textarea data-caption-source="${escape(source)}">${escape(group.caption_templates?.[source])}</textarea></label>`).join("")}</fieldset>` : "";
  const disk = settingsGroup === "retention" && state.disk ? `<p>Media disk usage · ${number(Math.round(state.disk.media_bytes / 1048576))} MB</p>` : "";
  const mode = preferences.golive || summaryStatus();
  const golive = settingsGroup === "posting" ? `<section class="panel todo-row"><div><h2>Publishing mode</h2><p>${mode.dry_run ? "Dry run is on" : "Live posting is on"}${mode.dry_run_mode ? " · " + escape(pretty(mode.dry_run_mode)) : ""}</p></div>
    ${extendedButton(mode.dry_run ? "go_live" : "go_dry", "", mode.dry_run ? "Go live" : "Back to dry run", mode.dry_run ? { dry_run_mode: null } : {})}</section>` : "";
  return golive + `<form id="group-settings" class="panel commands" data-group="${settingsGroup}"><h2>${settingsLabels[settingsGroup]}</h2>${disk}<p id="lines-tip" class="muted">Enter one value per line in lists.</p>
    <div class="settings-fields">${Object.entries(settingFields[settingsGroup]).map(([path, label]) => settingField(path, label, settingValue(group, path))).join("")}</div>
    ${slots}${templates}<button class="button primary" ${unavailable.has("/api/settings/" + settingsGroup) ? "hidden" : ""}>Save ${settingsLabels[settingsGroup].toLowerCase()} settings</button></form>`;
}
async function renderSettings() {
  if (connectionsBusy || content.querySelector("form")?.contains(document.activeElement) ||
      content.querySelector("form[data-dirty]")) return;
  if (settingsGroup === "connections") {
    connectionRows = (await api("/api/connections")).connections;
    connections();
    return;
  }
  preferences = await api("/api/settings");
  if (settingsGroup === "posting" && preferences.golive) {
    await actionAvailable(preferences.golive.dry_run ? "go_live" : "go_dry", null,
      preferences.golive.dry_run ? { dry_run_mode: null } : {});
  }
  patchMarkup(content, settingsTabs() + (preferences[settingsGroup] ? groupForm() : settingsForm()));
}
document.addEventListener("input", (event) => {
  const form = event.target.closest("#group-settings, #settings-form");
  if (form) form.dataset.dirty = "";
});
document.addEventListener("click", (event) => {
  const add = event.target.closest("[data-add-slot]");
  if (add) {
    const row = add.closest("fieldset").querySelector(".slot-chips");
    row.insertAdjacentHTML("beforeend", slotChip());
    row.lastElementChild.querySelector("input").focus();
    add.form.dataset.dirty = "";
  }
  const remove = event.target.closest("[data-remove-slot]");
  if (remove) {
    remove.form.dataset.dirty = "";
    remove.closest(".slot-chip").remove();
  }
});
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("#group-settings");
  if (!form) return;
  event.preventDefault();
  const group = form.dataset.group;
  const values = structuredClone(preferences[group]);
  for (const field of form.querySelectorAll("[name]")) {
    const [outer, inner] = field.name.split(".");
    const value = field.type === "checkbox" ? field.checked : field.type === "number" ? Number(field.value) :
      field.hasAttribute("data-list") ? field.value.split("\n").map((line) => line.trim()).filter(Boolean) :
      field.type === "date" ? field.value || null : field.value;
    if (inner) (values[outer] ||= {})[inner] = value;
    else values[outer] = value;
  }
  if (group === "look") values.versions = [...form.querySelectorAll("[data-preset]:checked")].map((field) => field.value);
  if (group === "posting") {
    values.slots = Object.fromEntries([...form.querySelectorAll("[data-slot-profile]")].map((row) =>
      [row.dataset.slotProfile, [...row.querySelectorAll("input")].map((field) => field.value)]));
    values.caption_templates = Object.fromEntries([...form.querySelectorAll("[data-caption-source]")].map((field) =>
      [field.dataset.captionSource, field.value]));
  }
  event.submitter.disabled = true;
  try {
    const response = await api("/api/settings/" + group, values);
    delete form.dataset.dirty;
    preferences[group] = values;
    toast(response.message || "Settings saved.");
  } catch (error) {
    if (isUnavailable(error)) {
      unavailable.add("/api/settings/" + group);
      event.submitter.hidden = true;
    } else toast(error.message, { error: true });
  } finally {
    event.submitter.disabled = false;
  }
});
