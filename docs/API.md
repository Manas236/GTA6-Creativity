# Dashboard HTTP API

The dashboard listens on loopback. Use the same `localhost` or `127.0.0.1` origin throughout. Responses disable HTTP caching. Call `GET /api/state` first and send its `csrf` value as `X-CSRF-Token` on every POST, together with the same session cookie. Other origins, non-loopback hosts and missing CSRF tokens return 403.

## Reads

| Method and path | Response |
| --- | --- |
| `GET /`, `/queue`, `/analytics`, `/failures`, `/make`, `/review`, `/library`, `/settings`, `/connections`, `/items/<id>` | Dashboard HTML shell. |
| `GET /static/<file>` | Dashboard assets. |
| `GET /api/state` | Pipeline stage counts, items, posts, metrics, recent commands, run state, scheduling rules, and CSRF token. Includes `status_line`, `todo`, `analytics_warnings` and `disk.media_bytes`. |
| `GET /api/items/<id>` | Item, posts, social caption, review details, bindings, available actions, media availability, `versions` and `voiceover`. A missing item returns 404. |
| `GET /api/library` | Filtered paginated item IDs with `total`, `page` and `pages`. See the Library filters for source, stage, dates and search. |
| `GET /api/settings` | Settings groups listed below, plus transitional flat settings fields used by the existing dashboard. |
| `GET /api/schedule?from=YYYY-MM-DD&days=7` | `{"slots":[{"at","profile","item_id","title"}]}`. Defaults to today in the active profile. Includes empty slots with null item IDs and titles. Invalid input returns “Choose a YYYY-MM-DD date and 1–60 days.” |
| `GET /api/discovered` | Unclaimed, releasable discovered items as `[{"item_id","title","source","score","age_h","url"}]`. |
| `GET /api/logs?lines=200` | `{"lines":[...]}` from the newest `dashboard-*.log` under the media logs directory, with credentials redacted. Returns an empty list if no log exists. Invalid line counts return “Choose 1–1,000 log lines.” |
| `GET /api/connections` | Available checks and cached results, with service ID, label, status, reason and timestamp. |
| `GET /media/<id>/<variant>` | Local video with range support for `a`, `b`, `cut`, `p1`, `p2` and `p3`. Use `thumb` for a thumbnail. Missing or unusable videos return 404. |

`status_line` has `dry_run`, `dry_run_mode`, `runner` (`running`, `stopped` or `stale`), `next_post_at` and `problems`. `todo` carries `to_review`, `needs_decision`, `failed` and `next_post_at`. Each analytics warning has `item_id`, `post_id`, `window_h`, `missed_at` and `late_snapshot_taken`.

Each version contains its lowercase `variant`, uppercase `preset`, `label`, `url` and `meta`. Missing or invalid sidecars produce `meta: {}`. The voiceover object exposes `script`, `voice` and `speed`. Each post has `post_id`, `platform`, `variant`, `posted_at`, `remote_url`, `metrics` and `missed_windows`. Metrics contain `hours`, `age_hours`, `window_h`, `late`, `views`, `likes`, `comments`, `shares`, `saves` and `reach`. Late snapshots report actual age in `hours` and retain the scheduled window in `window_h` when applicable.

## Settings routes

`POST /api/settings/<group>` accepts a partial JSON object for that group and returns `{"message": "..."}`. The write backs up the existing local YAML and atomically replaces the overlay. An omitted field keeps its prior value. `POST /api/settings` accepts the complete legacy form returned by the compatibility fields, with `slots`, `instagram`, `facebook`, `telegram`, `buffer`, `youtube`, `twitch`, `official_weight` and `heatmap_weight`.

| Group | Fields and constraints |
| --- | --- |
| `posting` | `profile` is `local` or `server`. `slots` maps profiles to unique `HH:MM` times. `min_gap_hours` is 3–24. `daily_post_target` is an integer from 1 to 8. `platforms` contains Instagram and Facebook toggles. `hashtags` holds up to 30 tags beginning with `#`. `caption_templates` maps sources to strings using `{hook_text}`, `{author_name}`, `{channel}` and `{hashtags}`. |
| `look` | `versions` contains one or more unique presets from P1, P2 and P3. `sfx` has `enabled_categories`, `max_per_clip` (0–20) and `min_gap_s` (0–60). `music.enabled` is a boolean. `hook` has `font_size`, `min_font_size` and `warn_below`. `caption` has `font_size`. Font sizes are integers from 12 to 400, and the minimum cannot exceed the hook font size. |
| `sources` | `youtube` has `enabled` and `queries`. `twitch` has `enabled`, `enable_from` (ISO date or null), `lookback_hours` (1–168) and `min_views` (0–1 billion). Also accepts `official_weight` (0–5), `official_channels`, `heatmap_weight` (0–2) and `max_source_minutes` (0.1–1,440, applied to the YouTube duration cap). |
| `approval` | `telegram` toggles Telegram review while approval is manual. `switch_to_auto_after_days` is 0–365 or null. Zero and null keep manual approval. `auto_quality_gate` toggles the automatic quality checks. |
| `voiceover` | `voice` is a Kokoro voice ID. `speed` is 0.5–2. |
| `retention` | `posted_days` and `failed_days` are 0–3,650. |
| `golive` | Read-only group with `dry_run` and `dry_run_mode`. Use a go-live or go-dry ticket to change them. |

Unknown groups return “Choose a configurable settings group.” Unknown or malformed fields return “Submit only known fields for this settings group.” Bad values report the field and valid range or format.

## Prepare and confirm

Send `{"action":"…","item_id":"…","payload":{}}` to `POST /api/prepare`. Omit `item_id` for global actions. The response includes `ticket`, `description`, `title`, `action` and the normalized `payload`. Send `{"ticket":"…"}` to `POST /api/confirm` within 15 minutes. A valid confirmation returns 202 and `command`. A replay reads the same command, whose status may have progressed since the first response.

A command can be `pending`, `running`, `applied`, `refused` or `failed`. Poll `GET /api/state` for completion when a runner owns the lock. The `result` field carries the message. When confirmation applies a reversible action immediately, it can return `undo: {"action","item_id","payload"}`. Pass that object back through prepare and confirm. A refused prepare returns a 409 JSON error. Invalid request bodies return 400. Expired or invalid tickets return 409. Database contention returns 503 with “Database is busy or unavailable. Refresh and retry; no action was confirmed.”

| Action | Payload | Effect and common refusals |
| --- | --- | --- |
| `approve`, `aonly`, `bonly` | `{}` | Legacy approval for both, A or B. A missing render returns “Render A/B is missing. Re-render before approval.” Preset items return “Choose a preset version.” |
| `approve_versions` | `{"variants":["p2"]}` | Approves selected available preset renders. Invalid choices return “Choose one or more available preset versions.” A missing file returns “A selected render is missing. Re-render before approval.” |
| `reject`, `hookok` | `{}` | Rejects an item or accepts a flagged hook/source overlap for another render. A stale decision returns “This decision no longer applies. Refresh the item.” |
| `edit_hook` | `{"hook_text":"…"}` | Edits an unposted clip, invalidates the previous hook binding and rerenders after validation. Use 1–220 characters on one line. |
| `edit_caption` | `{"body":"…","hashtags":"#tag"}` | Saves the publishing caption. Maximum 2,200 characters including credit and 30 hashtags. |
| `edit_caption_words` | `{"words":[{"i":3,"text":"ZELNICK"}]}` | Merges indexed overrides and starts a render job. Invalid words return “Enter indexed caption words with 1–80 characters each.” |
| `edit_voiceover` | `{"script":"…","voice":"am_adam","speed":1.1}` | Starts TTS and rerenders for review. Voice and speed are optional. Invalid values return “Use a 1–300 character script, a Kokoro voice and speed between 0.5 and 2.” |
| `unapprove`, `restore_approval` | `{}` and `{"command_id":"…"}` | Returns an unstarted item to review or restores its prior approval. Restoring a queued item requires that its old slot is still usable. |
| `up`, `down`, `position` | `{}` for up/down or `{"target":"item_id"}` | Reorders the scheduled queue. Refuses if the item is already at that end, the queue changed or posting started. |
| `reslot`, `move_to_slot` | `{"after":"ISO"}` or `{"at":"ISO"}` | Finds the next slot for a queued item or schedules at an exact future configured slot. Exact moves return “That slot is unavailable under the posting gap or daily target.” for conflicts. Include a timezone. |
| `post_now` | `{}` | Schedules an approved item at the earliest permitted time. Can start the loop. Refuses if posting is paused, platforms are disabled or the confirmed slot changed. |
| `pause`, `resume` | `{}` | Toggles dashboard Meta pause. A Meta cooldown can remain after resume. |
| `make`, `clip_url` | `{"count":1}` or `{"url":"…","start":0,"end":20}` | Starts a creation job for 1–10 clips or one source URL. Rejects duplicates or invalid trim hints. |
| `make_from_discovered` | `{}` | Claims and runs an existing discovered item. Otherwise returns “Choose an unclaimed discovered item.” |
| `remake_cut`, `remake_hook` | `{}` | Starts a creation job with a different cut or regenerated hook. Requires a usable source or cut. |
| `retry`, `delete` | `{}` | Retries eligible download/cut/render failures or removes disposable local media while preserving the source history. Already deleted items cannot be deleted again. |
| `snapshot_now` | `{}` | Reads metrics for live posts of the item. Outside a scheduled window, records an ad hoc late snapshot. Missing live posts return “This item has no live posts.” |
| `run_stage` | `{"stage":"analytics"}` or `{"stage":"cleanup"}` | Queues a nonblocking stage pass. Invalid values return “Choose analytics or cleanup.” An active loop drains the job at its analytics boundary. |
| `go_live` | `{"dry_run_mode":null}` | Writes live mode to local configuration. Requires an enabled platform and passing relevant Meta checks from the last 15 minutes. Refusals include “Enable at least one posting platform before going live.” and “Run a successful Meta connection test first (valid for 15 minutes).” |
| `go_dry` | `{}` | Sets `app.dry_run: true` and `posting.dry_run_mode: plan_only` in the local configuration. |

## Existing direct controls

| Method and path | Request and effect |
| --- | --- |
| `POST /api/runner` | `{"operation":"start"\|"stop"\|"force","stage":"…" }`. Force requires a stop request at least two minutes old and a matching stage. Publishing and remote runners cannot be force-stopped. |
| `POST /api/connections/test` | `{"service":"all"}` or one registered service ID. Invalid selections return “Choose a known connection check.” Concurrent requests return “A connection test is already running.” |
| `POST /api/connections/send-telegram-test` | Explicitly sends a Telegram test message and returns its result. Missing credentials return a `not_set` result. |

The settings and existing direct controls use CSRF-protected POST routes. The command actions above use signed tickets.
