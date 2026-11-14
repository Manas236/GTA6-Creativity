"""Create or complete .env from .env.example, asking for secrets in the terminal.

Run by setup.bat and update.bat; you don't need to run it yourself.

    python scripts/env_wizard.py              # setup: create .env, or ask only for required vars
                                              # that are missing or empty
    python scripts/env_wizard.py --new-only   # update: ask only for vars that .env.example has
                                              # and .env doesn't have at all yet

Existing values are always kept. Secret values are read with getpass (no echo) and are never
printed. .env is written atomically (temp file + os.replace).

Exit codes: 0 every required var is set, 2 some required var is still empty,
3 no interactive terminal to ask in, 130 cancelled (what was entered so far is saved).
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
ENV = ROOT / ".env"

KEY_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
TAG_LINE = re.compile(r"^#\s*\[(required|optional)((?:\s*,\s*\w+)*)\]\s*(.*)$", re.IGNORECASE)

# Light format checks so an obvious paste mistake is caught before it is saved.
VALIDATORS: dict[str, tuple[re.Pattern[str], str]] = {
    "TELEGRAM_BOT_TOKEN": (re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$"), "a token like 123456789:AA..."),
    "TELEGRAM_CHAT_ID": (re.compile(r"^-?\d+$"), "a number"),
    "IG_USER_ID": (re.compile(r"^\d+$"), "a number (the account ID, not the @handle)"),
    "FB_PAGE_ID": (re.compile(r"^\d+$"), "a number"),
    "HEALTHCHECK_URL": (re.compile(r"^https?://\S+$"), "a URL starting with https://"),
    "DASHBOARD_PORT": (re.compile(r"^(102[4-9]|10[3-9]\d|1[1-9]\d\d|[2-9]\d{3}|[1-5]\d{4}|6[0-4]\d{3}|65[0-4]\d\d|655[0-2]\d|6553[0-5])$"),
                       "a port number between 1024 and 65535"),
}


@dataclass
class Var:
    key: str
    default: str
    required: bool = False
    secret: bool = False
    hint: str = ""
    comments: list[str] = field(default_factory=list)


def parse_example(path: Path | None = None) -> list[Var]:
    """Every KEY= line of .env.example with its tag line ([required|optional, secret] hint)."""
    path = path or EXAMPLE
    out: list[Var] = []
    block: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            block.append(line)
            continue
        m = KEY_LINE.match(line)
        if m:
            var = Var(key=m.group(1), default=m.group(2).strip(), comments=list(block))
            for c in reversed(block):
                t = TAG_LINE.match(c)
                if t:
                    flags = {f.strip().lower() for f in t.group(2).split(",") if f.strip()}
                    var.required = t.group(1).lower() == "required"
                    var.secret = "secret" in flags
                    var.hint = t.group(3).strip()
                    break
            out.append(var)
        block = []
    return out


def read_env(path: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """(lines, {key: value}) of an existing .env, parsed the same way pipeline/config.py does."""
    path = path or ENV
    if not path.exists():
        return [], {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = KEY_LINE.match(s)
        if m:
            values[m.group(1)] = m.group(2).strip().strip("'\"")
    return lines, values


def missing_required(values: dict[str, str], spec: list[Var]) -> list[str]:
    return [v.key for v in spec if v.required and not values.get(v.key, "").strip()]


def clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def ask(var: Var) -> str | None:
    """Ask for one value. Returns the value, or None when the user leaves it empty."""
    label = "required" if var.required else "optional, Enter skips"
    print()
    print(f"{var.key}  ({label})")
    if var.hint:
        print(f"  {var.hint}")
    reader = getpass.getpass if var.secret else input
    prompt = "  Paste it (hidden): " if var.secret else "  Value: "
    empty_once = False
    while True:
        value = clean(reader(prompt))
        if not value:
            if not var.required or empty_once:
                if var.required:
                    print(f"  Left empty for now. Run setup.bat again when you have {var.key}.")
                return None
            print("  This one is required. Paste it now, or press Enter again to leave it empty for now.")
            empty_once = True
            continue
        if any(ch.isspace() for ch in value):
            print("  That contains spaces or line breaks, which a key never has. Try again.")
            continue
        check = VALIDATORS.get(var.key)
        if check and not check[0].match(value):
            print(f"  That doesn't look like {check[1]}. Try again, or press Enter to skip.")
            continue
        if var.secret:
            print("  Saved (not shown).")
        return value


def render_new(spec: list[Var], values: dict[str, str]) -> str:
    """A fresh .env: .env.example's layout with the values filled in."""
    out = ["# Written by setup.bat (scripts/env_wizard.py) from .env.example.",
           "# Private: never commit, paste or share this file. Re-run setup.bat to fill in missing values.",
           ""]
    started = False
    for raw in EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = KEY_LINE.match(raw.strip())
        if m:
            started = True
            out.append(f"{m.group(1)}={values.get(m.group(1), '')}")
        elif started or not raw.startswith("#"):
            started = True  # skip only the template's own header comment block
            out.append(raw)
    return "\n".join(out).rstrip("\n") + "\n"


def render_update(lines: list[str], spec: list[Var], changes: dict[str, str]) -> str:
    """The existing .env with changed values replaced in place and new keys appended."""
    out = list(lines)
    done: set[str] = set()
    for i, line in enumerate(out):
        m = KEY_LINE.match(line.strip())
        if m and m.group(1) in changes and m.group(1) not in done:
            out[i] = f"{m.group(1)}={changes[m.group(1)]}"
            done.add(m.group(1))
    by_key = {v.key: v for v in spec}
    for key, value in changes.items():
        if key in done:
            continue
        var = by_key.get(key)
        if out and out[-1].strip():
            out.append("")
        if var:
            tag = next((c for c in reversed(var.comments) if TAG_LINE.match(c)), None)
            if tag:
                out.append(tag)
        out.append(f"{key}={value}")
    return "\n".join(out).rstrip("\n") + "\n"


def is_interactive() -> bool:
    """True only for a real terminal. On Windows the NUL device also reports isatty(), so ask
    the console API instead: GetConsoleMode only succeeds on an actual console input handle."""
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt
        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:  # noqa: BLE001
        return False


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--new-only", action="store_true",
                        help="only ask for vars that .env doesn't have at all yet (update.bat)")
    args = parser.parse_args(argv)

    if not EXAMPLE.exists():
        print(f"[FAIL] {EXAMPLE.name} is missing; can't build .env.")
        return 1
    spec = parse_example()
    lines, values = read_env()
    creating = not ENV.exists()

    if creating:
        to_ask = [v for v in spec if not v.default]
        intro = "Creating .env. Paste each value and press Enter; secrets stay hidden as you paste."
    elif args.new_only:
        to_ask = [v for v in spec if v.key not in values and not v.default]
        intro = "New settings since your last update:"
    else:
        to_ask = [v for v in spec if v.required and not values.get(v.key, "").strip()]
        intro = "Your .env is missing some required values:"

    # Vars with a safe default (e.g. DASHBOARD_PORT) are filled in without asking.
    changes: dict[str, str] = {v.key: v.default for v in spec if v.default and v.key not in values}

    interactive = is_interactive()
    cancelled = False
    if to_ask and not interactive:
        print("[WARN] No interactive terminal, so the env wizard can't ask for values.")
        if creating:
            # Leave .env absent: the next double-click of start-dashboard.bat or setup.bat asks.
            print("       .env not created. Double-click setup.bat to fill it in.")
            return 3
        print("       Double-click setup.bat to fill them in.")
        to_ask = []
    if to_ask:
        print(intro)
        print("Each prompt says where to get the value (SETUP.md has step-by-step guides).")
        print("Secrets stay hidden while you paste them and are never printed. Numeric IDs are shown so you can check them.")
        try:
            for var in to_ask:
                value = ask(var)
                changes[var.key] = value if value is not None else values.get(var.key, "")
        except (KeyboardInterrupt, EOFError):
            cancelled = True
            print("\n  Cancelled. Saving what you entered so far.")

    if creating or changes:
        final = dict(values)
        final.update(changes)
        text = render_new(spec, final) if creating else render_update(lines, spec, changes)
        write_atomic(ENV, text)
        values = final
        print(f"{'Created' if creating else 'Updated'} .env")

    missing = missing_required(values, spec)
    if cancelled:
        return 130
    if not missing:
        print("env OK")
        return 0
    print("[WARN] Required values still empty in .env: " + ", ".join(missing))
    print("       The dashboard works without them; the pipeline needs them. Run setup.bat again to add them.")
    return 2 if interactive else 3


if __name__ == "__main__":
    sys.exit(main())
