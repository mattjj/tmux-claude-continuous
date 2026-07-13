# claude-pair

A Claude pair programmer that looks over your shoulder in tmux. It watches the
pane you're working in — commands, output, even half-typed un-executed command
lines — and streams suggestions into a side pane when (and only when) it has
something worth saying. With the vim plugin installed it also sees the code
around your cursor, including unsaved edits.

```
┌─────────────────────────────┬──────────────────────┐
│ your shell / vim            │ claude-pair          │
│                             │                      │
│ $ git push --force origin m │ ── 14:02:11 ──────── │
│                             │ - --force on main?   │
│                             │   consider           │
│                             │   --force-with-lease │
└─────────────────────────────┴──────────────────────┘
```

## How it works

- Polls `tmux capture-pane` about once a second (visible screen + a little
  scrollback, so it sees the command line as you type it).
- When the pane content changes and then goes quiet for ~1.5s (debounced),
  it sends a snapshot to Claude, with a rolling conversation history so it
  remembers what it already told you.
- Claude is instructed to reply `SKIP` unless it spots something genuinely
  useful — a typo in the command you're about to run, a fix for the error
  that just scrolled by, a dangerous command, a bug in the code on screen.
  SKIPs render as a quiet `·`; real suggestions stream in under a timestamp.
- Prompt caching keeps the repeated context cheap; only new snapshot content
  is billed at full input price.

## Install

```sh
pip install -e .           # from this repo; installs the `claude-pair` command
```

Authentication: set `ANTHROPIC_API_KEY`, or log in once with
[`ant auth login`](https://platform.claude.com/docs/en/api/sdks/cli) — the SDK
picks up the profile automatically.

```fish
set -Ux ANTHROPIC_API_KEY sk-ant-...
```

### Vim plugin

Copy or symlink `vim/plugin/claude_pair.vim` into your plugin directory, or
point your plugin manager at this repo:

```vim
" vim-plug
Plug 'you/tmux-claude-continuous', {'rtp': 'vim'}
```

The plugin writes cursor/file/buffer state to
`~/.cache/claude-pair/vim_state.json` on `CursorHold`, `InsertLeave`,
`BufEnter`, and `BufWritePost`. It makes no network calls. For fresher state
while you pause mid-edit, lower `updatetime`:

```vim
set updatetime=1000
let g:claude_pair_context_lines = 60   " lines of buffer context (default 60)
```

`:ClaudePairToggle` turns state-writing off/on.

## Use

Inside tmux, in the pane you want watched:

```sh
claude-pair
```

That splits a 60-column side pane (focus stays where you are) and starts
watching. Ctrl-C in the side pane (or just kill the pane) stops it.

### Talking to it

Three ways to ask it something directly (direct messages are always answered
— no `SKIP` — and skip the debounce/cooldown):

- **Type in the watcher pane.** Jump into the side pane, type a message, hit
  Enter.
- **`claude-pair say "why did that fail?"`** from any pane or script. Messages
  land within a second.
- **A shell comment in your work pane.** Type `# claude: how do I undo the
  last commit?` at the prompt — in fish that's a no-op comment, but the
  watcher sees it on screen and answers it once.

### Recalling the last suggestion

Every real suggestion (not the `SKIP`s) is saved to
`~/.cache/claude-pair/last_suggestion.txt`, with a running history in
`~/.cache/claude-pair/suggestions.log`. To pull it up without looking at the
side pane:

- **In vim:** `:ClaudeLast`, mapped to `<leader>cl` by default — opens the
  suggestion in a small scratch split (`q` closes it). Set
  `let g:claude_pair_default_mappings = 0` to opt out of the mapping, or map
  `<Plug>(ClaudePairLast)` yourself.
- **In fish (or any shell):** `claude-pair last`. A `claude-last` wrapper
  function ships in `fish/functions/` — copy or symlink it into
  `~/.config/fish/functions/` if you want the shorter name.

Useful flags (pass them to `claude-pair`; they're forwarded to the watcher):

| flag | default | meaning |
|---|---|---|
| `--model` | `claude-opus-4-8` | any Claude model id (`CLAUDE_PAIR_MODEL` env var also works) |
| `--effort` | `low` | reasoning effort per suggestion; raise for deeper reviews |
| `--debounce` | `0.25` | seconds of quiet after a change before asking Claude |
| `--cooldown` | `2` | minimum seconds between API calls |
| `--scrollback` | `50` | extra history lines beyond the visible screen |
| `--history` | `8` | snapshot/reply pairs Claude remembers |
| `--width` | `60` | side pane width |
| `--dry-run` | | print the snapshots instead of calling the API |

A tmux binding makes it one keystroke:

```tmux
# ~/.tmux.conf — prefix + P starts the pair programmer for the current pane
bind P run-shell "tmux send-keys 'claude-pair' Enter"
```

## Cost note

Default settings call the API on every pause in activity, with Opus. Prompt
caching keeps repeated context ~10x cheaper, but if you leave it running all
day and want it cheaper still, drop the model
(`--model claude-sonnet-5` or `claude-haiku-4-5`) or raise
`--debounce`/`--cooldown`.

## Troubleshooting

- **`run this inside a tmux session`** — the launcher needs `$TMUX`; start
  tmux first.
- **No vim context in suggestions** — check that
  `~/.cache/claude-pair/vim_state.json` updates while you edit (the watcher
  ignores it after 2 minutes of staleness), and that the plugin loaded
  (`:echo g:loaded_claude_pair`).
- **Too chatty / too quiet** — chattiness lives in the system prompt in
  `claude_pair/watcher.py` (`SYSTEM_PROMPT`); tune the "worth interrupting
  for" list to taste.
