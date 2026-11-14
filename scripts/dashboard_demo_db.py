"""Create an isolated dashboard demo: python scripts/dashboard_demo_db.py /tmp/reels-demo.db.

Refuses production filenames, existing files and symlinks. Never reads credentials or calls APIs.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import (
    analytics,
    binding,
    clock,
    config,
    db,
    posting,
    render,
    scheduler,
    transcribe,
)
from dashboard.store import initialize

TITLES = [
    "Vice City after dark",
    "Lucia and Jason on the road",
    "A closer look at the waterfront",
    "What makes Leonida feel alive?",
    "Small details in the new city",
    "The streets beyond Ocean Drive",
    "Inside the latest trailer",
    "A different side of Vice City",
]


def create_media(root):
    from PIL import Image, ImageDraw, ImageFont

    root.mkdir(parents=True, exist_ok=False)
    font_path = config.ROOT_DIR / "assets/fonts/Montserrat-Bold.ttf"
    for variant in ("A", "B"):
        image = Image.new("RGB", (360, 640))
        draw = ImageDraw.Draw(image)
        for y in range(640):
            mix = y / 640
            draw.line(
                (0, y, 360, y),
                fill=(int(57 + mix * 88), int(33 + mix * 26), int(91 - mix * 18)),
            )
        draw.ellipse((190, 220, 330, 360), fill="#f6a58f")
        rng = random.Random(6)
        for x in range(0, 380, 27):
            top = rng.randint(320, 470)
            draw.rectangle((x, top, x + 24, 640), fill="#171929")
            for window in range(top + 12, 620, 22):
                draw.rectangle((x + 7, window, x + 11, window + 6), fill="#b8799c")
        font = ImageFont.truetype(str(font_path), 29)
        small = ImageFont.truetype(str(font_path), 12)
        draw.text((26, 44), "DEMO FOOTAGE", font=small, fill="#f5b1c9")
        draw.text(
            (25, 100), "VICE CITY\nAFTER DARK?", font=font, fill="#fff3f6", spacing=9
        )
        draw.text(
            (25, 585), f"VARIANT {variant} / LOCAL PREVIEW", font=small, fill="#ffffff"
        )
        poster = root / f"{variant}.png"
        image.save(poster)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-loop",
                "1",
                "-i",
                str(poster),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(root / f"{variant}.mp4"),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    return root


def build_demo(path, media=True):
    path = Path(path).absolute()
    if (
        path.name.lower() == "pipeline.db"
        or path.resolve() == db.DEFAULT_DB_PATH.resolve()
    ):
        raise ValueError(
            "Refusing pipeline.db. Choose a new demo filename outside the repository."
        )
    if path.exists() or path.is_symlink():
        raise ValueError("Refusing to overwrite an existing database or symlink.")
    if path.resolve().is_relative_to(config.ROOT_DIR.resolve()):
        raise ValueError("Demo data must be outside the repository.")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents two invocations from seeding the same file.
    with path.open("xb"):
        pass
    initialize(path)
    media_root = path.parent / (path.stem + "-media")
    if media:
        create_media(media_root)
    else:
        media_root.mkdir(exist_ok=False)
        for variant in ("A", "B"):
            (media_root / f"{variant}.mp4").write_bytes(b"demo-test-media")
    cfg = config.get_config(reload=True)
    now = clock.utcnow()
    rng = random.Random(236)
    counts = {
        "discovered": 36,
        "downloaded": 4,
        "cut": 3,
        "needs_review": 3,
        "rendered": 2,
        "awaiting_approval": 1,
        "approved": 2,
        "queued": 4,
        "posted": 3,
        "measured": 8,
        "cleaned": 10,
        "rejected": 4,
        "failed": 5,
    }
    index = 0
    words = [
        {"word": w, "start": n * 0.6, "end": n * 0.6 + 0.5}
        for n, w in enumerate(
            "Lucia and Jason drive through Vice City after dark".split()
        )
    ]
    for stage in db.STAGES:
        for local in range(counts[stage]):
            index += 1
            item_id = f"demo-{index:03d}"
            source = "twitch" if index % 3 == 0 else "youtube"
            title = TITLES[index % len(TITLES)]
            db.insert_discovered_item(
                {
                    "id": item_id,
                    "source": source,
                    "source_id": item_id,
                    "source_url": "https://example.com/demo/" + item_id,
                    "author_name": ["Leonida Lens", "City Watch", "Frame by Frame"][
                        index % 3
                    ],
                    "original_title": title,
                    "raw_velocity": 3.5,
                    "discovery_score": rng.uniform(0.25, 0.95),
                },
                path,
            )
            created = clock.iso(now - timedelta(days=rng.randint(1, 22)))
            fields = {
                "created_at": created,
                "cut_start": 34.5,
                "cut_end": 54.5,
                "cut_duration": 20,
                "cut_path": str(media_root / "A.mp4"),
                "download_path": str(media_root / "A.mp4"),
                "hook_text": "VICE CITY AFTER DARK?",
                "voiceover_script": "Lucia and Jason drive through Vice City after dark.",
                "hook_style": ["question", "curiosity", "contrast"][index % 3],
                "hook_grounded": 1,
                "authenticity": "official" if index % 5 == 0 else "unverified",
                "style_preset": "P0",
                "render_a_path": str(media_root / "A.mp4"),
                "render_b_path": str(media_root / "B.mp4"),
                "voiceover_audio_path": str(media_root / "B.mp4"),
                "chosen_variant": "A" if index % 2 else "B",
            }
            segment = {"id": item_id, **fields}
            fields["hook_binding"] = binding.hook_record(
                segment,
                fields["hook_text"],
                fields["voiceover_script"],
                binding.transcript_text(words),
                "segment_check",
            )
            fields["voiceover_binding"] = binding.voiceover_record(
                segment, fields["voiceover_script"], fields["voiceover_audio_path"]
            )
            if stage in ("discovered", "downloaded", "cut", "needs_review"):
                fields.update(render_a_path=None, render_b_path=None)
            if stage == "needs_review":
                fields.update(
                    hook_grounded=0,
                    hook_binding=binding.with_check(
                        fields["hook_binding"], "needs_review"
                    ),
                    hook_review=json.dumps(
                        {
                            "reason": "Hook needs review against the cut transcript.",
                            "transcript_excerpt": binding.transcript_text(words),
                        }
                    ),
                )
            if stage == "failed":
                error_stage = ["download", "cut", "render", "posting", "publishing"][
                    local
                ]
                fields.update(
                    error_stage=error_stage,
                    error_message={
                        "download": "Source download timed out",
                        "cut": "no_clean_ending: no usable sentence window",
                        "render": "ffmpeg verification failed",
                        "posting": "Meta permission error; inspect remote state",
                        "publishing": "Remote state unknown after lost reply",
                    }[error_stage],
                )
            if stage == "rejected":
                fields["rejection_reason"] = (
                    "duplicate_cut"
                    if local % 2
                    else "auto_quality_gate: source_text_collision"
                )
            db.transition_stage(item_id, stage, fields, path)
            item = db.get_item(item_id, path)
            render.store_cut_words(
                item_id, item, words, transcribe.cache_key(cfg), path
            )
            db.log_stage(
                item_id,
                "discover",
                "success",
                "Candidate entered the discovery pool",
                db_path=path,
            )
            if stage != "discovered":
                db.log_stage(
                    item_id,
                    "download",
                    "success",
                    "Source downloaded and dedupe checked",
                    db_path=path,
                )
            if stage not in ("discovered", "downloaded"):
                failure = stage == "failed" and fields.get("error_stage") == "cut"
                db.log_stage(
                    item_id,
                    "cut",
                    "failure" if failure else "success",
                    (
                        fields.get("error_message")
                        if failure
                        else "Sentence-aligned cut selected"
                    ),
                    db_path=path,
                )
            db.log_stage(
                item_id,
                fields.get("error_stage") or stage,
                "failure" if stage == "failed" else "success",
                fields.get("error_message") or f"Item moved to {stage}",
                db_path=path,
            )
            if stage in ("queued", "posted", "measured", "cleaned"):
                if stage == "queued":
                    slot = clock.iso(scheduler.next_free_slot(now, cfg, path))
                else:
                    slot = clock.iso(
                        now - timedelta(days=1 + index % 21, hours=index % 6)
                    )
                rows = posting.ensure_post_rows(
                    item, slot_datetime=slot, cfg=cfg, db_path=path
                )
                for row in rows:
                    if stage == "queued":
                        posting.update_post(
                            row["id"],
                            db_path=path,
                            variant="?",
                            variant_decided_at=None,
                        )
                    else:
                        posting.set_status(
                            row["id"],
                            "live",
                            db_path=path,
                            posted_at=slot,
                            remote_id=f"demo-remote-{row['id']}",
                            variant=fields["chosen_variant"],
                        )
                        for hours in (24, 72):
                            if clock.parse(slot) + timedelta(hours=hours) > now:
                                continue
                            views = int(
                                rng.randint(800, 22000) * (1 if hours == 24 else 2.1)
                            )
                            analytics.record_snapshot(
                                row["id"],
                                hours,
                                {
                                    "views": views,
                                    "likes": views // 40,
                                    "comments": views // 230,
                                    "shares": views // 130,
                                },
                                {},
                                path,
                            )
                            with db.db(path) as conn:
                                conn.execute(
                                    "UPDATE metrics SET fetched_at=? WHERE post_id=? AND snapshot_hours=?",
                                    (
                                        clock.iso(
                                            clock.parse(slot) + timedelta(hours=hours)
                                        ),
                                        row["id"],
                                        hours,
                                    ),
                                )
    analytics.rebuild_ab_learning(cfg, path)
    db.kv_set("dashboard:demo", "true", path)
    db.kv_set(
        "dashboard:runtime",
        json.dumps(
            {
                "at": db.now(),
                "stage": "idle",
                "dry_run": True,
                "profile": "local",
                "gemini_blocked_until": None,
            }
        ),
        path,
    )
    print(f"Created {index} demo items at {path}")
    print(f"python -m dashboard --db {path} --media-root {media_root}")
    return path, media_root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    build_demo(args.path)
