"use strict";

(() => {
  const actualFetch = window.fetch.bind(window);
  const iso = (delta = 0) => new Date(Date.now() + delta).toISOString();
  const stamp = iso();
  const buildItem = (id, title, stage, actions, extra = {}) => ({
    id, original_title: title, stage, actions, author_name: "Vice City Replay", source: "youtube",
    created_at: iso(-3600000), hook_text: title, authenticity: "unverified", cut_signal: "heatmap", ...extra,
  });
  const legacy = ["approve", "aonly", "bonly", "reject", "remake_hook", "remake_cut", "edit_hook", "edit_caption", "delete"];
  const modern = ["approve_versions", "edit_caption_words", "edit_voiceover", "reject", "remake_hook", "edit_hook", "edit_caption", "delete"];
  const queueActions = ["post_now", "reslot", "unapprove", "up", "down"];
  const items = [
    buildItem("neon", "Did the trailer hide a second getaway car?", "awaiting_approval", modern),
    buildItem("legacy", "A tiny detail on the Ocean Drive sign", "awaiting_approval", legacy),
    buildItem("third", "The traffic lights change off camera", "rendered", modern),
    buildItem("decision", "Is that a radio teaser or background noise?", "needs_review", ["hookok", "edit_hook", "reject"]),
    buildItem("failed", "A close-up from the marina", "failed", ["retry", "delete"], { error_message: "No clean ending found.", error_stage: "cut" }),
    buildItem("rejected", "A duplicate rooftop shot", "rejected", ["delete"]),
    buildItem("posted", "The palm trees move like this for a reason", "posted", ["snapshot_now"]),
    buildItem("ready", "The getaway starts at the marina", "approved", queueActions, { chosen_variant: "p2" }),
    buildItem("queued", "One frame that gives away the location", "queued", queueActions, { chosen_variant: "b" }),
  ];
  const date = iso().slice(0, 10);
  const nextSlot = date + "T12:30:00Z";
  const scheduled = new Map([[nextSlot, "queued"]]);
  const posts = [{
    id: "post-reel", post_id: "post-reel", item_id: "posted", platform: "instagram", variant: "p2",
    status: "live", posted_at: iso(-4 * 86400000), remote_url: "https://www.instagram.com/reel/mock-preview/",
    missed_windows: [72],
  }];
  const metrics = [
    { hours: 24, late: false, views: 8512, likes: 612, comments: 27, shares: 148, saves: 33, reach: 7240 },
    { hours: 72, late: true, views: 12341, likes: 931, comments: 56, shares: 240, saves: 61, reach: 10814 },
  ].map((metric, index) => ({ ...metric, post_id: "post-reel", item_id: "posted", platform: "instagram", variant: "p2", fetched_at: iso(index - 1000) }));
  const voiceover = { script: "Did the trailer hide a second getaway car?", voice: "af_heart", speed: 1.08 };
  const settings = {
    posting: { profile: "local", slots: { local: ["18:00", "21:00"], server: ["18:00", "21:00"] }, min_gap_hours: 3, daily_post_target: 2,
      platforms: { instagram: true, facebook: true }, hashtags: ["#GTA6", "#FelonyFlamingo"], caption_templates: { youtube: "{hook}\n{credit}", twitch: "{hook}\n{credit}" } },
    look: { versions: ["P1", "P2", "P3"], sfx: { enabled_categories: ["impact", "whoosh", "riser"], max_per_clip: 5, min_gap_s: 0.6 },
      music: { enabled: true }, hook: { font_size: 150, min_font_size: 115, warn_below: 130 }, caption: { font_size: 68 } },
    sources: { youtube: { enabled: true, queries: ["GTA 6 trailer details"] }, twitch: { enabled: true, enable_from: "2026-11-19", lookback_hours: 48, min_views: 75 },
      official_weight: 2, official_channels: ["Rockstar Games"], heatmap_weight: 0.8, max_source_minutes: 25 },
    approval: { telegram: true, switch_to_auto_after_days: 7, auto_quality_gate: true },
    voiceover: { voice: "af_heart", speed: 1.08 }, retention: { posted_days: 30, failed_days: 7 },
    golive: { dry_run: true, dry_run_mode: "simulate" },
  };
  const wordOverrides = new Map();
  const versions = (id) => ["p1", "p2", "p3"].map((variant, index) => ({
    variant, preset: variant.toUpperCase(), label: ["Clean", "Punchy", "Full hype"][index], url: "/media/" + id + "/" + variant,
    meta: { preset: variant.toUpperCase(), hook_text: items.find((item) => item.id === id).hook_text,
      hook_font_px: [141, 118, 134][index], hook_lines: ["Did the trailer hide", "a second getaway car?"], hook_below_floor: false,
      sfx_cues: [{ t: 0, category: "impact", file: "impact_03.wav", trigger: "hook" }, { t: 1.4, category: "whoosh", file: "whoosh_02.wav", trigger: "cut" }],
      music_track: "ocean-drive.wav", caption_words: ["Did", "the", "trailer", "hide", "a", "second", "getaway", "car?"].map((text, i) => ({ i, text: wordOverrides.get(id)?.get(i) ?? text, start: +(1.2 + i * 0.3).toFixed(1), end: +(1.5 + i * 0.3).toFixed(1) })),
      duration_s: 23.4, rendered_at: stamp },
  }));
  const discoveredItems = Array.from({ length: 10 }, (_, index) => ({
    item_id: "discovered-" + (index + 1), title: ["Vice City by night", "Trailer frame breakdown", "The marina chase", "Hidden street signs", "Radio chatter", "Beach traffic", "A Leonida news bulletin", "Downtown reflections", "A second police cruiser", "Spot the rooftop"][index],
    source: index % 3 === 0 ? "twitch" : "youtube", score: (98 - index * 4) / 100,
    age_h: index + 2, url: "https://example.com/clip/" + (index + 1),
  }));
  const lines = ["INFO  Runner started in dry run", "INFO  Discovered 10 candidate clips", "INFO  Rendered P1, P2 and P3 previews", "WARNING  Missed analytics window · 72 h", "INFO  Saved a late analytics snapshot"];
  const tickets = new Map(), commands = [];
  const actions = new Set([...legacy, ...modern, ...queueActions, "retry", "hookok", "snapshot_now",
    "move_to_slot", "run_stage", "make_from_discovered", "go_live", "go_dry", "restore_approval",
    "edit_caption_words", "edit_voiceover", "make", "clip_url", "position", "pause", "resume"]);
  let runner = "running";
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

  window.fetch = async (resource, options = {}) => {
    const url = new URL(resource instanceof Request ? resource.url : resource, location.origin);
    if (url.origin !== location.origin) return actualFetch(resource, options);
    const path = url.pathname;
    const body = options.body ? JSON.parse(options.body) : {};
    if (path.startsWith("/media/")) return actualFetch(path.endsWith("/thumb") ? "/static/thumbnail.svg" : "/static/mock-preview.mp4");
    if (path === "/api/state") {
      const toReview = items.filter(({ stage }) => ["awaiting_approval", "rendered"].includes(stage)).length;
      const stages = [...new Set(items.map(({ stage }) => stage))].map((stage) => ({ stage, count: items.filter((item) => item.stage === stage).length }));
      return json({ csrf: "mock", profile: "local", timezone: "Asia/Kolkata", slots: settings.posting.slots.local, dry_run: settings.golive.dry_run,
        status_line: { ...settings.golive, runner, next_post_at: [...scheduled.keys()].sort()[0], problems: 1 },
        todo: { to_review: toReview, needs_decision: 1, failed: 1, next_post_at: nextSlot }, items, stages, metrics, commands,
        posts: [...posts, ...[...scheduled].map(([slot_datetime, item_id]) => ({ slot_datetime, item_id, platform: "instagram", status: "pending", variant: "p2" }))],
        analytics_warnings: [{ item_id: "posted", post_id: "post-reel", window_h: 72, missed_at: stamp, late_snapshot_taken: true }],
        disk: { media_bytes: 1024 ** 2 * 464 }, lock: { status: runner === "running" ? "alive" : "stopped", kind: "loop" },
        runtime: {}, ready: 2, target: 6, approval_mode: "manual", interval: 60, cut_stats: { rate: 0.1, no_clean_ending: 1, attempts: 10 }, demo: true });
    }
    if (path.startsWith("/api/items/")) {
      const item = items.find(({ id }) => id === decodeURIComponent(path.split("/").at(-1)));
      if (!item) return json({ error: "Item not found" }, 404);
      return json({ item, actions: item.actions, binding: {}, media: { a: item.id === "legacy", b: item.id === "legacy", cut: true },
        versions: ["legacy", "queued", "decision", "failed", "rejected"].includes(item.id) ? [] : versions(item.id),
        voiceover, caption: item.caption_body ?? item.hook_text + " · #GTA6", caption_hashtags: item.caption_hashtags ?? "#GTA6", reviews: [], logs: [], review: { reason: item.error_message },
        posts: item.stage === "posted" ? posts.map((post) => ({ ...post, metrics })) : [] });
    }
    if (path === "/api/settings") return json(settings);
    if (path.startsWith("/api/settings/") && options.method === "POST") {
      settings[path.split("/").at(-1)] = body;
      return json({ message: "Mock settings saved." });
    }
    if (path === "/api/schedule") {
      const start = new Date(url.searchParams.get("from") + "T12:00:00Z");
      const slots = [];
      for (let day = 0; day < Number(url.searchParams.get("days")); day++) {
        const key = new Date(+start + day * 86400000).toISOString().slice(0, 10);
        for (const time of ["12:30", "15:30"]) {
          const at = key + "T" + time + ":00Z";
          const item_id = scheduled.get(at) || null;
          slots.push({ at, profile: "local", item_id, title: items.find((item) => item.id === item_id)?.original_title || null });
        }
      }
      return json({ slots });
    }
    if (path === "/api/logs") return json({ lines: lines.slice(-Number(url.searchParams.get("lines") || 200)) });
    if (path === "/api/discovered") return json(discoveredItems);
    if (path === "/api/library") {
      const filtered = items.filter((item) => (!url.searchParams.get("stage") || item.stage === url.searchParams.get("stage")) &&
        item.original_title.toLowerCase().includes((url.searchParams.get("q") || "").toLowerCase()));
      return json({ ids: filtered.map(({ id }) => id), page: 1, pages: 1, total: filtered.length });
    }
    if (path === "/api/runner") {
      runner = body.operation === "start" ? "running" : "stopped";
      return json({ message: "Mock runner " + runner + "." });
    }
    if (path === "/api/connections") return json({ connections: ["Instagram", "Facebook", "Telegram", "Gemini"].map((label) => ({
      id: label.toLowerCase(), label, status: "unchecked", reason: "Mock connection", checked_at: null,
    })) });
    if (path.startsWith("/api/connections/")) return json({ result: { status: "ok", reason: "Simulated locally." }, connections: [] });
    if (path === "/api/prepare") {
      if (!actions.has(body.action)) return json({ error: "Unknown action." }, 409);
      const ticket = "mock-" + (tickets.size + 1);
      const title = items.find((item) => item.id === body.item_id)?.original_title;
      tickets.set(ticket, body);
      return json({ ticket, title, description: "Confirm " + body.action.replaceAll("_", " ") + " in mock mode.", payload: body.payload || {} });
    }
    if (path === "/api/confirm") {
      const plan = tickets.get(body.ticket);
      if (!plan) return json({ error: "Unknown ticket" }, 400);
      const { action, item_id, payload = {} } = plan;
      const id = body.ticket;
      const previous = commands.find((command) => command.id === id);
      if (previous) return json({ command: previous });
      const item = items.find((entry) => entry.id === item_id);
      let undo;
      if (["approve_versions", "approve", "aonly", "bonly"].includes(action) && item) {
        item.stage = "approved";
        item.chosen_variant = payload.variants?.[0] || (action === "aonly" ? "a" : "b");
        item.actions = queueActions;
        undo = { action: "unapprove", item_id, payload: {} };
      } else if (action === "unapprove" && item) {
        item.stage = "awaiting_approval";
        item.actions = item.id === "legacy" ? legacy : modern;
      } else if (action === "move_to_slot" && item) {
        const prior = [...scheduled].find(([, entry]) => entry === item_id)?.[0];
        if (prior) scheduled.delete(prior);
        if (payload.at) scheduled.set(payload.at, item_id);
        undo = { action, item_id, payload: { at: prior || null } };
      } else if (action === "edit_hook" && item) {
        undo = { action, item_id, payload: { hook_text: item.hook_text } };
        item.hook_text = payload.hook_text ?? item.hook_text;
      } else if (action === "edit_caption" && item) {
        undo = { action, item_id, payload: { body: item.caption_body ?? item.hook_text, hashtags: item.caption_hashtags ?? "#GTA6" } };
        item.caption_body = payload.body;
        item.caption_hashtags = payload.hashtags;
      } else if (action === "edit_caption_words" && item) {
        if (!wordOverrides.has(item_id)) wordOverrides.set(item_id, new Map());
        for (const { i, text } of payload.words) wordOverrides.get(item_id).set(i, text);
      } else if (action === "snapshot_now" && item_id === "posted") {
        metrics.push({ ...metrics.at(-1), views: metrics.at(-1).views + 25, fetched_at: iso(), late: true });
      } else if (["go_live", "go_dry"].includes(action)) {
        settings.golive = { dry_run: action === "go_dry", dry_run_mode: action === "go_dry" ? "simulate" : null };
      } else if (action === "reject" && item) {
        item.stage = "rejected";
        item.actions = ["delete"];
      } else if (action === "make_from_discovered") {
        const index = discoveredItems.findIndex((entry) => entry.item_id === item_id);
        if (index >= 0) discoveredItems.splice(index, 1);
      } else if (action === "delete" && item) {
        items.splice(items.indexOf(item), 1);
      } else if (action === "edit_voiceover") {
        Object.assign(voiceover, payload);
      }
      lines.push("INFO  Mock action applied · " + action);
      const command = { id, action, item_id, status: "applied", result: "Mock action applied.", created_at: iso() };
      commands.unshift(command);
      return json({ command, undo });
    }
    if (path.startsWith("/api/")) return json({ error: "Unknown endpoint" }, 404);
    return actualFetch(resource, options);
  };
})();
