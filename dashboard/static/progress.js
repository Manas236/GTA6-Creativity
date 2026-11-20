"use strict";
// Posting progress panel. Opens after a confirmed Post now, and from the Queue, Library and item
// pages. Everything shown comes from /api/items/<id>/posting (post_events, the command row, the
// runner lock and the posts rows); it polls every 2 seconds until the post is live or failed.

const progressDialog = document.querySelector("#progress-dialog");
const progressStates = {
  waiting: ["Waiting", "gray"],
  running: ["Running", "blue"],
  done: ["Done", "green"],
  failed: ["Failed", "red"],
  skipped: ["Not sent (dry run)", "amber"],
};
let progressItem = null;
let progressRun = null;
let progressTimer = null;
let progressData = null;
let progressRequest = 0;

const hasProgress = (id) => Boolean(state?.progress_items?.includes(id));
function progressLink(id, text = "Posting progress") {
  return hasProgress(id) ? `<button type="button" class="button secondary" data-progress="${escape(id)}">${escape(text)}</button>` : "";
}

// Server messages carry UTC ISO times (kept in Copy details); the panel shows the dashboard's time zone.
function localTimes(text) {
  return String(text || "").replace(/\b(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)( UTC)?/g,
    (_, iso) => formatTime(iso, { second: "2-digit" }));
}

function progressClock(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: state?.timezone, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(new Date(value));
}

function progressSpan(stage) {
  if (!stage.started_at) return "";
  const end = stage.finished_at ? progressClock(stage.finished_at) : stage.state === "running" ? "now" : "";
  return progressClock(stage.started_at) + (end ? " → " + end : "");
}

function progressExtra(stage, data) {
  const detail = stage.detail || {};
  if (stage.key === "waiting" && stage.state === "running" && detail.waited_s != null)
    return `<span class="small">Waited ${Number(detail.waited_s)} s${detail.runner?.alive ? "" : " · no runner is running"}</span>`;
  if (stage.key === "upload" && detail.percent != null && stage.state !== "skipped") {
    const sent = detail.bytes_sent != null ? `${(detail.bytes_sent / 1048576).toFixed(1)} of ${(detail.bytes_total / 1048576).toFixed(1)} MB · ` : "";
    return `<progress max="100" value="${Number(detail.percent)}" aria-label="Upload progress"></progress><span class="small">${sent}${Number(detail.percent).toFixed(0)}%</span>`;
  }
  if (stage.key === "processing" && detail.polls)
    return `<span class="small">Status polls: ${Number(detail.polls)} · last status from Meta: ${escape(detail.meta_status || "unknown")}</span>`;
  if (stage.key === "live" && data.permalink)
    return `<a class="button secondary" href="${escape(data.permalink)}" target="_blank" rel="noopener noreferrer">Open on Instagram ↗</a>`;
  return "";
}

function progressMarkup(data) {
  const failedLabel = data.stages.find((stage) => stage.key === data.failed_step)?.label || "posting";
  const outcome = data.outcome === "live" ? pill("Live", "green") : data.outcome === "failed" ? pill("Failed", "red")
    : data.outcome === "dry_run" ? pill("Dry run finished", "amber") : pill("In progress", "blue");
  const when = data.started_at ? formatTime(data.started_at, { second: "2-digit" }) : "";
  const runs = data.runs?.length > 1 ? `<label class="progress-runs">Attempt <select id="progress-run">${data.runs.map((run) =>
    `<option value="${run.index}" ${run.index === data.run_index ? "selected" : ""}>${run.index + 1} · ${escape(formatTime(run.started_at))} · ${escape(run.outcome ? pretty(run.outcome) : "in progress")}</option>`).join("")}</select></label>` : "";
  return `${data.dry_run ? '<div class="progress-dry" role="note">DRY RUN, nothing will reach Instagram</div>' : ""}
    <div class="progress-head" id="progress-head"><div><h2 id="progress-title">${escape(data.title || data.item_id)}</h2>
    <p class="muted">${escape(data.kind_label)} · post ${escape(data.post_id ?? "not created yet")} · ${escape(pretty(data.platform))}${when ? " · started " + escape(when) : ""}</p></div>${outcome}</div>
    ${runs}
    ${(data.notices || []).map((text, index) => `<p class="notice" data-row="notice-${index}">${escape(localTimes(text))}</p>`).join("")}
    <ol class="progress-steps" id="progress-steps">${data.stages.map((stage, index) => {
      const [label, tone] = progressStates[stage.state] || [stage.state, "gray"];
      return `<li class="progress-step state-${escape(stage.state)}" data-row="${escape(stage.key)}">
        <span class="progress-index" aria-hidden="true">${stage.state === "done" ? "✓" : stage.state === "failed" ? "!" : index + 1}</span>
        <div class="grow"><div class="progress-line"><strong>${escape(stage.label)}</strong>${pill(label, tone)}<time>${escape(progressSpan(stage))}</time></div>
        ${stage.message ? `<p>${escape(localTimes(stage.message))}</p>` : ""}${progressExtra(stage, data)}</div></li>`;
    }).join("")}</ol>
    ${data.outcome === "failed" ? `<section class="progress-error" id="progress-error" role="alert"><h3>Failed at: ${escape(failedLabel)}</h3>
      <p class="error-text">${escape(data.error_text || "No error text was recorded.")}</p>
      ${data.retry ? `<p><strong>Will it be retried?</strong> ${escape(localTimes(data.retry.text))}</p>` : ""}</section>` : ""}`;
}

async function loadProgress() {
  clearTimeout(progressTimer);
  if (!progressItem || !progressDialog.open) return;
  const sequence = ++progressRequest;
  const status = document.querySelector("#progress-status");
  try {
    const query = progressRun == null ? "" : "?run=" + encodeURIComponent(progressRun);
    const data = await api("/api/items/" + encodeURIComponent(progressItem) + "/posting" + query);
    if (sequence !== progressRequest || !progressDialog.open) return;
    progressData = data;
    patchMarkup(document.querySelector("#progress-body"), progressMarkup(data));
    const live = data.active && progressRun == null;
    status.textContent = live ? "Live view: updates every 2 seconds." : data.active ? "Showing an earlier attempt." : "Finished. Reopen any time from the clip's row.";
    if (live) progressTimer = setTimeout(loadProgress, 2000);
  } catch (error) {
    if (sequence !== progressRequest) return;
    status.textContent = error.message;
    if (error.status !== 404 && progressDialog.open) progressTimer = setTimeout(loadProgress, 5000);
  }
}

function setProgressURL(id) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("progress", id);
  else url.searchParams.delete("progress");
  history.replaceState(history.state, "", url.pathname + url.search + url.hash);
}

function openProgress(id) {
  if (!id) return;
  progressItem = id;
  progressRun = null;
  progressData = null;
  document.querySelector("#progress-body").innerHTML = '<p class="muted">Loading posting progress…</p>';
  document.querySelector("#progress-status").textContent = "";
  if (!progressDialog.open) progressDialog.showModal();
  setProgressURL(id);
  loadProgress();
}

async function copyProgress() {
  const text = progressData?.copy_text;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    progressDialog.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast("Posting details copied. Paste them to Claude.");
}

progressDialog.addEventListener("close", () => {
  clearTimeout(progressTimer);
  progressItem = null;
  progressRequest += 1;
  setProgressURL(null);
});
document.querySelector("#close-progress").onclick = () => progressDialog.close();
document.querySelector("#done-progress").onclick = () => progressDialog.close();
document.querySelector("#copy-progress").onclick = copyProgress;
document.addEventListener("change", (event) => {
  if (event.target.id !== "progress-run") return;
  const index = Number(event.target.value);
  progressRun = index === progressData?.runs?.length - 1 ? null : index;
  loadProgress();
});
document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-progress]");
  if (!control) return;
  event.preventDefault();
  event.stopPropagation();
  openProgress(control.dataset.progress);
});

// A refresh, or a link with ?progress=<item>, shows the same panel again.
const requestedProgress = new URLSearchParams(location.search).get("progress");
if (requestedProgress) {
  mockReady.then(async () => {
    for (let tries = 0; !state && tries < 100; tries += 1) await new Promise((resolve) => setTimeout(resolve, 100));
    openProgress(requestedProgress);
  }).catch(() => {});
}
