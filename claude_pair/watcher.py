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
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.rule import Rule

DEFAULT_MODEL = "claude-opus-4-8"

CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-pair"
)
VIM_STATE_FILE = CACHE_DIR / "vim_state.json"
VIM_STATE_MAX_AGE = 120  # seconds before vim state is considered stale
INBOX_DIR = CACHE_DIR / "inbox"  # `claude-pair say` drops messages here
LAST_SUGGESTION_FILE = CACHE_DIR / "last_suggestion.txt"
LAST_CODE_FILE = CACHE_DIR / "last_code.txt"  # just the fenced code blocks
SUGGESTION_LOG = CACHE_DIR / "suggestions.log"

SYSTEM_PROMPT = """\
You are an expert pair programmer quietly looking over the user's shoulder. \
Each user message is a snapshot of their terminal pane (and, when they are in \
vim, the region of the file around their cursor, including unsaved edits). \
Snapshots arrive whenever the screen changes and then goes briefly quiet — so \
you often see half-typed commands and code mid-edit. The watcher follows the \
user's active tmux pane, so consecutive snapshots may come from different \
panes or windows — the pane id is in the <terminal> tag. A pane switch is \
not itself worth commenting on.

The user works in fish shell and vim on Linux. Tailor suggestions accordingly \
(fish syntax, not bash; vim-native ways of doing things).

Respond with exactly SKIP unless you have something genuinely worth \
interrupting for, such as:
- a typo or bug in a command they are still typing, before they run it
- a command that just failed, with the likely fix
- a destructive or dangerous command about to be run
- a real bug, or a clearly better approach, in code visible in the editor
- a meaningfully faster way to do what they are obviously trying to do
- a quick implementation of code they are trying to write

The user can also talk to you directly:
- A <user_message> block in a snapshot is the user addressing you. Always \
answer it — never SKIP a snapshot that contains one. Be concise but complete; \
you may exceed the bullet limit for a real question.
- A shell comment addressed to you in the terminal (like \
`# claude: how do I undo the last commit?`) is also a direct question. Answer \
it the first time you see it; if an earlier reply of yours already answered \
it, SKIP.

Rules:
- Most snapshots deserve SKIP. Routine, correct activity needs no comment. \
Half-finished work is not a problem to fix; only speak if the part already \
written is wrong or headed somewhere bad.
- Never repeat or rephrase a suggestion you already made (your earlier replies \
are in this conversation). If the situation hasn't changed, SKIP.
- Don't guess at intent you can't see. If a command is ambiguous but \
plausible, SKIP.
- When you do speak: at most 3 short bullets ("- "), most important first. \
Simple markdown only: bullets, `inline code`, and fenced code blocks with a \
language tag (```fish, ```python, ```vim) for commands, snippets, and \
implementations. No headers, no tables, no bold walls of text. Keep prose \
lines under ~52 characters where possible — the output pane is narrow. A \
one-line command fix can just be a fenced command.
- Fences are the deliverable: anything the user might paste into their \
editor or run goes inside a fenced block, ready to use as-is; explanation \
stays outside. The user has commands that insert your latest fenced code \
directly at their cursor, so never put prose, placeholders you haven't \
flagged, or "..." elisions inside a fence.
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


def resolve_active_pane(own_pane: str | None) -> str | None:
    """The active pane of the active window in this session, if it isn't us."""
    result = subprocess.run(
        ["tmux", "list-panes", "-s", "-F", "#{pane_id} #{pane_active} #{window_active}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "1" and parts[2] == "1":
            return parts[0] if parts[0] != own_pane else None
    return None


def read_vim_state() -> dict | None:
    try:
        stat = VIM_STATE_FILE.stat()
        if time.time() - stat.st_mtime > VIM_STATE_MAX_AGE:
            return None
        return json.loads(VIM_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def poll_inbox() -> list[str]:
    """Consume messages dropped by `claude-pair say`."""
    try:
        files = sorted(INBOX_DIR.glob("msg-*.txt"))
    except OSError:
        return []
    messages = []
    for path in files:
        try:
            text = path.read_text().strip()
            path.unlink()
        except OSError:
            continue
        if text:
            messages.append(text)
    return messages


def start_stdin_reader(inbox: "queue.Queue[str]") -> None:
    """Lines typed into the watcher pane become direct messages."""

    def reader() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    inbox.put(line)
        except (OSError, ValueError):
            pass

    threading.Thread(target=reader, daemon=True).start()


def build_snapshot(
    pane_text: str,
    vim_state: dict | None,
    user_messages: list[str] | None = None,
    pane: str = "",
) -> str:
    parts = []
    for msg in user_messages or []:
        parts.append(f"<user_message>\n{msg}\n</user_message>")
    parts.append(f'<terminal pane="{pane}">\n{pane_text}\n</terminal>')
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
        self.console = Console(highlight=False)

    def banner(self, text: str) -> None:
        self.console.print(text, style="dim")

    def divider(self) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.console.print()
        self.console.print(
            Rule(title=f"[bold cyan]✻[/] [dim]{stamp}[/]", style="cyan", align="left")
        )

    def stream(self, text: str) -> None:
        # plain passthrough (dry-run output)
        sys.stdout.write(text)
        sys.stdout.flush()

    def live_suggestion(self, refresh_per_second: int = 8) -> Live:
        """A live-updating region the suggestion streams into as markdown."""
        return Live(
            console=self.console,
            refresh_per_second=refresh_per_second,
            vertical_overflow="visible",
        )

    def note(self, text: str) -> None:
        self.console.print(text, style="yellow")

    def tick(self) -> None:
        # quiet heartbeat for SKIP responses
        self.console.print("·", style="dim", end="")


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
        live: Live | None = None
        try:
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
                    buffered += text
                    if live is None:
                        stripped = buffered.lstrip()
                        if stripped and not "SKIP".startswith(stripped[:4].upper()):
                            # definitely not a SKIP — start rendering it
                            self.printer.divider()
                            live = self.printer.live_suggestion()
                            live.start()
                    if live is not None:
                        live.update(
                            Markdown(buffered.strip(), code_theme=self.args.theme)
                        )
                final = stream.get_final_message()
        finally:
            if live is not None:
                live.stop()

        reply = "".join(
            block.text for block in final.content if block.type == "text"
        ).strip()

        if final.stop_reason == "refusal":
            self.printer.note("[claude declined to comment on this snapshot]")
        elif live is None:
            self.printer.tick()
        else:
            self._save_suggestion(reply)

        # keep the assistant turn (including SKIP) so the model knows what it
        # already said and doesn't repeat itself
        self.messages.append({"role": "assistant", "content": reply or "SKIP"})

    @staticmethod
    def _save_suggestion(reply: str) -> None:
        """Persist the suggestion for `claude-pair last` and :ClaudeLast."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{stamp}]\n{reply}\n"
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            LAST_SUGGESTION_FILE.write_text(entry)
            LAST_CODE_FILE.write_text(extract_code(reply))
            with SUGGESTION_LOG.open("a") as log:
                log.write(entry + "\n")
        except OSError:
            pass  # persistence is best-effort; never break the watcher


def extract_code(reply: str) -> str:
    """The contents of all fenced code blocks, blank-line separated."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in reply.splitlines():
        if line.lstrip().startswith("```"):
            if in_fence:
                blocks.append("\n".join(current))
                current = []
            in_fence = not in_fence
            continue
        if in_fence:
            current.append(line)
    if in_fence and current:  # unclosed fence (response cut short)
        blocks.append("\n".join(current))
    blocks = [b for b in blocks if b.strip()]
    return "\n\n".join(blocks) + "\n" if blocks else ""


# ---------------------------------------------------------------------------
# main loop


def watch(args: argparse.Namespace) -> None:
    printer = Printer()
    mode = "pinned to" if args.pin else "following active pane, starting at"
    printer.banner(
        f"claude-pair {mode} {args.target} "
        f"(model={args.model}, effort={args.effort}, debounce={args.debounce}s)"
    )
    printer.banner(
        "talk to me: type here + Enter, run `claude-pair say ...`, "
        "or type `# claude: ...` in your shell"
    )
    if not args.dry_run and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        printer.note(
            "note: ANTHROPIC_API_KEY is not set; relying on an "
            "`ant auth login` profile if one exists"
        )

    suggester = None if args.dry_run else Suggester(args, printer)

    stdin_inbox: "queue.Queue[str]" = queue.Queue()
    start_stdin_reader(stdin_inbox)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    poll_inbox()  # discard messages queued before we started

    own_pane = os.environ.get("TMUX_PANE")
    target = args.target

    last_hash = None
    last_change_at = None
    analyzed_hash = None
    last_call_at = 0.0

    while True:
        if not args.pin:
            active = resolve_active_pane(own_pane)
            if active and active != target:
                target = active
                printer.banner(f"→ following {target}")
                # new pane: start change-detection fresh
                last_hash = None
                last_change_at = None
                analyzed_hash = None

        try:
            pane_text = capture_pane(target, args.scrollback)
        except RuntimeError as exc:
            if not args.pin:
                # the watched pane went away; fall back until a new one is active
                printer.banner(f"→ {target} closed; waiting for an active pane")
                last_hash = None
                time.sleep(args.interval)
                continue
            printer.note(f"\nclaude-pair: {exc} (pane closed?) — exiting")
            return

        digest = hashlib.sha256(pane_text.encode()).hexdigest()
        now = time.monotonic()
        if digest != last_hash:
            last_hash = digest
            last_change_at = now

        # direct messages jump the queue: no debounce, no cooldown
        direct = poll_inbox()
        while not stdin_inbox.empty():
            direct.append(stdin_inbox.get_nowait())

        settled = last_change_at is not None and now - last_change_at >= args.debounce
        cooled = now - last_call_at >= args.cooldown
        pane_is_new = digest != analyzed_hash

        if direct or (pane_is_new and settled and cooled):
            analyzed_hash = digest
            last_call_at = now
            snapshot = build_snapshot(pane_text, read_vim_state(), direct, pane=target)
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


def say(words: list[str]) -> None:
    """`claude-pair say <message>` — send a direct message to the watcher."""
    message = " ".join(words).strip()
    if not message:
        sys.exit("usage: claude-pair say <message>")
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    (INBOX_DIR / f"msg-{time.time_ns()}.txt").write_text(message)


def last(argv: list[str]) -> None:
    """`claude-pair last` — print the most recent suggestion (rendered).

    `claude-pair last --code` prints just the fenced code, raw — suitable
    for piping (e.g. `claude-pair last --code | fish_clipboard_copy`).
    """
    if "--code" in argv:
        try:
            code = LAST_CODE_FILE.read_text()
        except OSError:
            code = ""
        if not code.strip():
            sys.exit("claude-pair: no code in the last suggestion")
        sys.stdout.write(code)
        return
    try:
        text = LAST_SUGGESTION_FILE.read_text()
    except OSError:
        sys.exit("claude-pair: no suggestion yet")
    console = Console(highlight=False)
    lines = text.splitlines()
    if lines and lines[0].startswith("["):
        console.print(lines[0], style="dim")
        text = "\n".join(lines[1:])
    console.print(Markdown(text.strip(), code_theme="monokai"))


def update() -> None:
    """`claude-pair update` — pull the repo, reinstall, refresh vim-plug."""
    repo = Path(__file__).resolve().parent.parent
    console = Console(highlight=False)
    if not (repo / ".git").is_dir():
        sys.exit(
            f"claude-pair: no git repo at {repo} (not an editable install?) — "
            "update however you installed it"
        )

    console.print(f"→ git pull [dim]({repo})[/]", style="bold")
    pull = subprocess.run(
        ["git", "-C", str(repo), "pull", "--ff-only"],
        capture_output=True,
        text=True,
    )
    output = (pull.stdout + pull.stderr).strip()
    console.print(output, style=None if pull.returncode == 0 else "red")
    if pull.returncode != 0:
        sys.exit(1)

    if "Already up to date" not in output:
        console.print("→ pip install -e .", style="bold")
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-e", str(repo)]
        )
        if pip.returncode != 0:
            console.print("pip install failed — fix manually", style="red")

    # vim-plug users have their own clone of this repo; refresh it too.
    # `silent!` makes this a no-op for people who don't use vim-plug.
    if shutil.which("vim") and sys.stdout.isatty():
        console.print("→ vim +PlugUpdate", style="bold")
        subprocess.run(["vim", "+silent! PlugUpdate --sync", "+qa"])
    else:
        console.print(
            "skipping vim +PlugUpdate (no vim or not a terminal)", style="dim"
        )
    console.print("done ✻", style="bold green")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "say":
        say(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "last":
        last(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("update", "--update"):
        update()
        return

    parser = argparse.ArgumentParser(
        prog="claude-pair",
        description="Claude pair programmer watching your tmux pane. "
        "Run with no --target inside tmux to open a watcher side pane. "
        "`claude-pair say <message>` talks to a running watcher.",
    )
    parser.add_argument(
        "--target", help="tmux pane to start watching (e.g. %%3). Omit to auto-split."
    )
    parser.add_argument(
        "--pin",
        action="store_true",
        help="stay on the launch/--target pane instead of following the "
        "active pane as you move around",
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
        "--theme",
        default="monokai",
        help="pygments theme for code blocks (e.g. monokai, dracula, ansi_dark)",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="pane poll interval, seconds"
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=0.25,
        help="quiet time after a change before asking Claude, seconds",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=2.0,
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
