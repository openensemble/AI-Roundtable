# AI Roundtable

A lightweight local web app for a shared roundtable between **you, Claude Code, OpenAI
Codex, and Grok Build 4.5.** Choose any two assistants or enable all three. They reply
in sequence and share one transcript, so each sees—and can build on—the messages that
came before its turn.

## Run

Linux / macOS:

```bash
python3 server.py            # then open http://127.0.0.1:8765/
python3 server.py 9000       # custom port
```

Windows PowerShell:

```powershell
py -3 server.py              # then open http://127.0.0.1:8765/
py -3 server.py 9000         # custom port
```

No dependencies or copied API keys—the app reuses your existing `claude`, `codex`, and
`grok` CLI logins. All three commands must be on `PATH` to enable every participant; a
missing CLI is called out in the page and only that participant is disabled.

## Platform support

- **Linux, macOS, and native Windows:** supported when Python and the provider CLIs are on
  `PATH`. On Windows, use the providers' native PowerShell installers; the app prefers their
  `.exe` launchers. An npm `.cmd` install is accepted only when its standard same-stem `.ps1`
  shim is present; the app launches it through the included UTF-8 PowerShell bridge and never
  passes provider arguments through `cmd.exe`.
- **Windows isolation caveat:** Codex provides a native Windows sandbox. Claude and Grok run
  natively but do not provide an equivalent OS-level sandbox there, so the app uses their
  permission modes plus explicit read/edit instructions. Use **WSL 2** (with the app and all
  CLIs installed inside WSL) when you need stronger isolation for those providers.
- Provider processes stream through portable pipe-reader threads. Stop/barge-in terminates the
  complete POSIX process group; on native Windows it uses the built-in `taskkill /T /F` process-tree
  operation, with parent-only termination as a last resort if Windows denies that operation. Grok
  receives its prompt through a short-lived UTF-8 file rather than Unix-only `/dev/stdin`.

Official Windows installers (PowerShell):

```powershell
irm https://claude.ai/install.ps1 | iex
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
irm https://x.ai/cli/install.ps1 | iex
```

## How it works

- The app owns **one shared transcript**. Each turn, the whole labeled conversation is
  handed to whichever agent is replying, so everyone has full context.
- Replies **stream live**—Claude and Grok emit token fragments; Codex's CLI returns its
  message as a block, which the app reveals with the same typewriter effect.
- **Activity panes**—three boxes on the left show live thinking and available tool activity
  while each assistant works: streaming thinking (italic), tool calls
  (`$ git log`, `Read file.py`, `Web search: …`), and each result (`→ exit 0 …`, red `✗` on
  failure). Grok Build 0.2.x exposes thinking and reply tokens—but not tool events—in its
  streaming JSON, so its pane shows thinking only. Panes clear when you switch conversations
  and keep the last 400 lines.
- Send a message → the enabled assistants reply **in order**, so the second sees the first's
  new reply and the third sees both. The **first** menu chooses the starting assistant;
  `alternate` rotates the starting position each round.

### Choosing and addressing participants
- Use the **Claude**, **Codex**, and **Grok** chips to run any pair, all three, just one, or
  none. This makes Claude+Grok and Codex+Grok conversations work the same way as the original
  Claude+Codex pairing.
- Start your message with a **name**: `Claude, what do you think of this?` or
  `Grok, how would you build it?`—only that one replies. (`@claude`, `@codex`, and `@grok`
  work too.) Name a pair, such as `Claude and Grok, compare these options`, to target exactly
  those two even when all three chips are enabled. Name all three or use no name to ask the
  full enabled table. Non-targeted assistants remain silent but see the resulting messages.

### Auto-riff & barging in
- **Auto ⟳**—the enabled assistants keep taking ordered rounds on their own.
- **Barge in** — while they're talking, just start typing and hit Enter; the current reply
  is **interrupted** (its process is killed so it stops generating) and they **wait for you**.
  The **Stop** button (the Send button turns red while they're active) interrupts without
  sending. `Esc` also interrupts.
- **Riff ▸** — kick off an AI round with no new prompt from you (one round, or continuous
  if Auto is on).

### Saved conversations
- Every conversation is **saved to disk on the server** (`conversations/*.json`), so it
  survives a browser-data clear, reload, or opening the app from another browser.
- The **☰** button opens the history drawer: click a conversation to switch to it,
  **＋ New** to start a fresh one, **double-click** a title to rename, **🗑** to delete,
  and the **★** to **pin** — pinned conversations sort to the top of the list.
  Titles auto-derive from your first message. On launch, your last-open conversation reopens.
- Data dir is overridable with `ROUNDTABLE_DATA`.

### Your display name
- Use the **👤 name field** beside the workspace box and press **Save**. The name is used for
  human message labels, Markdown exports, and the shared prompt seen by every assistant.
- The setting is stored locally in `config.json`. That file is Git-ignored, and conversations
  store the stable role `user` instead of copying your name into every message, so changing it
  also relabels existing threads. A fresh public checkout defaults to **You**.

### Which folder the assistants can see
- The **📁 folder box** at the bottom-right sets the **working directory** all assistants read from—
  point it at the project you want to discuss (`/path/to/project`, `~` is expanded). A green
  **✓** / red **✗** validates the path live as you type.
- **Read-only by default**: Codex uses its `read-only` sandbox; Grok requests its `read-only`
  sandbox where the OS supports it; Claude uses its permission controls plus an explicit read-only
  instruction (and native Windows uses Claude's `plan` mode). Grok memory and subagents are disabled
  because this app supplies the complete shared transcript itself. On native Windows, heed the
  isolation caveat above. The choice is remembered across reloads; the initial folder is
  `ROUNDTABLE_CWD` (the app folder by default).
- **✎ Edit toggle** (amber chip in the header) lets the assistants edit files and run commands
  (git, tests, builds) when the conversation calls for it. Grok uses its `workspace` sandbox,
  restricting writes to the selected folder and its own runtime data. Claude uses
  `bypassPermissions`; Codex uses its dangerous bypass flag, so those two are **not OS-sandboxed**
  in Edit mode. The amber highlight is a reminder that commands and edits are live.

### Models & effort
- **Claude model** — Opus 4.8 (deep default), Fable 5, Sonnet 5 (fast), Haiku 4.5 (quick).
  Supported effort levels are discovered from Claude Code's no-turn SDK initialization response
  when the server starts, with a matching built-in fallback if the CLI is unavailable.
- **Codex model** — populated live from the Codex CLI's own model cache
  (`~/.codex/models_cache.json`), including each model's supported levels and native default.
  The blank model choice uses your `~/.codex/config.toml` model and validated effort; choosing
  a model explicitly makes `default` use that model's own default instead, preventing a stronger
  global setting from leaking into a smaller model.
- **Grok model**—populated from Grok Build's cache (`~/.grok/models_cache.json`); the installed
  build currently exposes `grok-4.5`. Claude and Grok bubbles are tagged with their model.
- **Effort** is set **independently per assistant**, and every menu tracks the selected model.
  Opus 4.8, Fable 5, and Sonnet 5 expose `low → max` plus `ultracode` (xhigh effort with
  dynamic-workflow orchestration); Haiku 4.5 exposes only `default` because it does not apply
  Claude effort controls. Codex options come from its model cache (gpt-5.5 → `xhigh`,
  gpt-5.6-sol → `ultra`, etc.), and Grok 4.5 exposes `low`, `medium`, and `high`. These map to
  `claude --effort …`, Codex's `model_reasoning_effort`, and `grok --reasoning-effort …`; the
  server validates every requested or inherited value against the selected model, clamps only to
  an advertised lower level, and never invents capabilities for an unknown model.
- Model and effort choices are remembered across reloads. **⭳** exports the thread to Markdown.
- **⟳ Restart** restarts the server from the page (re-execs in place—reloading `server.py` and
  every provider's model/effort catalog on the same port). The page waits for it to come back and
  reloads. Saved conversations are untouched. Handy after editing the code.

## Config (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `ROUNDTABLE_PORT` | `8765` | Port |
| `ROUNDTABLE_CWD` | this folder | Directory agents may **read** files from |
| `ROUNDTABLE_CLAUDE_MODEL` | `claude-opus-4-8` | Default Claude model |
| `ROUNDTABLE_CODEX_MODEL` | codex default | Force a Codex model |
| `ROUNDTABLE_GROK_MODEL` | `grok-4.5` | Default Grok model |
| `ROUNDTABLE_CONFIG` | `config.json` in this folder | Local app config (including your display name) |
| `CODEX_HOME` | `~/.codex` | Codex config and model-cache directory |
| `GROK_HOME` | `~/.grok` | Grok config and model-cache directory |

With **Edit off**, the roundtable is intended for inspection and discussion. Turn Edit on only
when you want live agent changes in the selected workspace.
