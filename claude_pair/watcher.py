"""Watch a tmux pane and stream pair-programming suggestions from Claude.

Run `claude-pair` inside tmux: it splits off a side pane and watches the pane
you were in. The watcher polls `tmux capture-pane` (which sees partial,
un-executed command lines), debounces changes, and asks Claude for a
suggestion. The model answers SKIP for anything not worth interrupting for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_MODEL = "claude-opus-4-8"

VIM_STATE_FILE = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "claude-pair"
    / "vim_state.json"
)
VIM_STATE_MAX_AGE = 120  # seconds before vim state is considered stale

SYSTEM_PROMPT = """\
You are an expert pair programmer quietly looking over the user's shoulder. \
Each user message is a snapshot of their terminal pane (and, when they are in \
vim, the region of the file around their cursor, including unsaved edits). \
Snapshots arrive whenever the screen changes and then goes briefly quiet — so \
you often see half-typed commands and code mid-edit.

The user works in fish shell and vim on Linux. Tailor suggestions accordingly \
(fish syntax, not bash; vim-native ways of doing things).

Respond with exactly SKIP unless you have something genuinely worth \
interrupting for, such as:
- a typo or bug in a command they are still typing, before they run it
- a command that just failed, with the likely fix
- a destructive or dangerous command about to be run
- a real bug, or a clearly better approach, in code visible in the editor
- a meaningfully faster way to do what they are obviously trying to do

Rules:
- Most snapshots deserve SKIP. Routine, correct activity needs no comment. \
Half-finished work is not a problem to fix; only speak if the part already \
written is wrong or headed somewhere bad.
- Never repeat or rephrase a suggestion you already made (your earlier replies \
are in this conversation). If the situation hasn't changed, SKIP.
- Don't guess at intent you can't see. If a command is ambiguous but \
plausible, SKIP.
- When you do speak: at most 3 short bullets, most important first. Plain \
text, no markdown headers or code fences. Keep lines under ~56 characters \
where possible — the output pane is narrow. A one-word command fix can just \
be the corrected command.
"""


# ---------------------------------------------------------------------------
# tmux + vim context gathering


def capture_pane(target: str, scrollback: int) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", f"-{scrollback}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "tmux capture-pane failed")
    return result.stdout.rstrip()


def read_vim_state() -> dict | None:
    try:
        stat = VIM_STATE_FILE.stat()
        if time.time() - stat.st_mtime > VIM_STATE_MAX_AGE:
            return None
        return json.loads(VIM_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def build_snapshot(pane_text: str, vim_state: dict | None) -> str:
    parts = [f"<terminal>\n{pane_text}\n</terminal>"]
    if vim_state and vim_state.get("context"):
        first = vim_state.get("first_line", 1)
        lines = vim_state["context"]
        numbered = "\n".join(
            f"{first + i:>5} {line}" for i, line in enumerate(lines)
        )
        parts.append(
            "<vim file={file} filetype={ft} cursor_line={line} mode={mode} "
            "unsaved_changes={mod}>\n{body}\n</vim>".format(
                file=json.dumps(vim_state.get("file", "")),
                ft=vim_state.get("filetype", ""),
                line=vim_state.get("line", 0),
                mode=vim_state.get("mode", ""),
                mod=bool(vim_state.get("modified")),
                body=numbered,
            )
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# output formatting


class Printer:
    def __init__(self) -> None:
        self.color = sys.stdout.isatty()

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def banner(self, text: str) -> None:
        print(self._c("2", text), flush=True)

    def divider(self) -> None:
        width = shutil.get_terminal_size((60, 20)).columns
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"── {stamp} " + "─" * max(0, width - len(stamp) - 4)
        print("\n" + self._c("36", line), flush=True)

    def stream(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def note(self, text: str) -> None:
        print(self._c("33", text), flush=True)

    def tick(self) -> None:
        # quiet heartbeat for SKIP responses
        sys.stdout.write(self._c("2", "·"))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Claude


class Suggester:
    def __init__(self, args: argparse.Namespace, printer: Printer) -> None:
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.args = args
        self.printer = printer
        self.messages: list[dict] = []

    def _trim_history(self) -> None:
        # keep the last N user/assistant pairs; history must start with "user"
        max_msgs = self.args.history * 2
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]
            while self.messages and self.messages[0]["role"] != "user":
                self.messages.pop(0)

    def suggest(self, snapshot: str) -> None:
        self.messages.append({"role": "user", "content": snapshot})
        self._trim_history()
        try:
            self._call()
        except self.anthropic.RateLimitError as exc:
            retry_after = int(exc.response.headers.get("retry-after", "30"))
            self.printer.note(f"\n[rate limited; pausing {retry_after}s]")
            self.messages.pop()  # snapshot not answered; drop it
            time.sleep(retry_after)
        except self.anthropic.APIStatusError as exc:
            self.printer.note(f"\n[api error {exc.status_code}: {exc.message}]")
            self.messages.pop()
            time.sleep(5)
        except self.anthropic.APIConnectionError:
            self.printer.note("\n[connection error; will retry on next change]")
            self.messages.pop()
            time.sleep(5)

    def _call(self) -> None:
        buffered = ""
        speaking = False
        with self.client.messages.stream(
            model=self.args.model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": self.args.effort},
            cache_control={"type": "ephemeral"},
            system=SYSTEM_PROMPT,
            messages=self.messages,
        ) as stream:
            for text in stream.text_stream:
                if speaking:
                    self.printer.stream(text)
                    continue
                buffered += text
                stripped = buffered.lstrip()
                if stripped and not "SKIP".startswith(stripped[:4].upper()):
                    # definitely not a SKIP — start showing it
                    speaking = True
                    self.printer.divider()
                    self.printer.stream(stripped)
            final = stream.get_final_message()

        reply = "".join(
            block.text for block in final.content if block.type == "text"
        ).strip()

        if final.stop_reason == "refusal":
            self.printer.note("\n[claude declined to comment on this snapshot]")
        elif not speaking:
            self.printer.tick()
        else:
            self.printer.stream("\n")

        # keep the assistant turn (including SKIP) so the model knows what it
        # already said and doesn't repeat itself
        self.messages.append({"role": "assistant", "content": reply or "SKIP"})


# ---------------------------------------------------------------------------
# main loop


def watch(args: argparse.Namespace) -> None:
    printer = Printer()
    printer.banner(
        f"claude-pair watching {args.target} "
        f"(model={args.model}, effort={args.effort}, debounce={args.debounce}s)"
    )
    if not args.dry_run and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        printer.note(
            "note: ANTHROPIC_API_KEY is not set; relying on an "
            "`ant auth login` profile if one exists"
        )

    suggester = None if args.dry_run else Suggester(args, printer)

    last_hash = None
    last_change_at = None
    analyzed_hash = None
    last_call_at = 0.0

    while True:
        try:
            pane_text = capture_pane(args.target, args.scrollback)
        except RuntimeError as exc:
            printer.note(f"\nclaude-pair: {exc} (pane closed?) — exiting")
            return

        digest = hashlib.sha256(pane_text.encode()).hexdigest()
        now = time.monotonic()
        if digest != last_hash:
            last_hash = digest
            last_change_at = now

        settled = last_change_at is not None and now - last_change_at >= args.debounce
        cooled = now - last_call_at >= args.cooldown
        if digest != analyzed_hash and settled and cooled:
            analyzed_hash = digest
            last_call_at = now
            snapshot = build_snapshot(pane_text, read_vim_state())
            if args.dry_run:
                printer.divider()
                printer.stream(snapshot + "\n")
            else:
                suggester.suggest(snapshot)

        time.sleep(args.interval)


# ---------------------------------------------------------------------------
# launcher: split a side pane and re-run ourselves inside it


def launch_split(args: argparse.Namespace, extra_argv: list[str]) -> None:
    if not os.environ.get("TMUX"):
        sys.exit("claude-pair: run this inside a tmux session")
    target = subprocess.run(
        ["tmux", "display-message", "-p", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    inner = [sys.executable, "-m", "claude_pair", "--target", target, *extra_argv]
    # keep the pane open briefly on crash so the error is readable
    cmd = " ".join(shlex.quote(part) for part in inner) + " || sleep 15"
    subprocess.run(
        ["tmux", "split-window", "-dh", "-l", str(args.width), cmd],
        check=True,
    )
    print(f"claude-pair: watching pane {target} in a new side pane")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-pair",
        description="Claude pair programmer watching your tmux pane. "
        "Run with no --target inside tmux to open a watcher side pane.",
    )
    parser.add_argument(
        "--target", help="tmux pane to watch (e.g. %%3). Omit to auto-split."
    )
    parser.add_argument(
        "--model", default=os.environ.get("CLAUDE_PAIR_MODEL", DEFAULT_MODEL)
    )
    parser.add_argument(
        "--effort",
        default="low",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="reasoning effort per suggestion (default: low, for snappiness)",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="pane poll interval, seconds"
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=1.5,
        help="quiet time after a change before asking Claude, seconds",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=4.0,
        help="minimum seconds between Claude calls",
    )
    parser.add_argument(
        "--scrollback",
        type=int,
        default=50,
        help="extra scrollback lines to include beyond the visible pane",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=8,
        help="snapshot/reply pairs of conversation memory to keep",
    )
    parser.add_argument(
        "--width", type=int, default=60, help="width of the auto-split side pane"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print snapshots instead of calling the API (for testing)",
    )
    args = parser.parse_args()

    if args.target:
        try:
            watch(args)
        except KeyboardInterrupt:
            print("\nclaude-pair: bye")
    else:
        # forward every flag except --width to the inner invocation
        extra: list[str] = []
        skip_next = False
        for token in sys.argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if token == "--width":
                skip_next = True
                continue
            if token.startswith("--width="):
                continue
            extra.append(token)
        launch_split(args, extra)


if __name__ == "__main__":
    main()
