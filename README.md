<img src="manager/web/favicon.svg" width="56" align="left" alt="">

# Hangar

Start, stop, switch, update and watch the local LLM engines on one Windows PC with one GPU, from anywhere. You can drive
it from a web page (works on a phone), an MCP server (for Claude or any other MCP client), a REST API, and the `hangar`
command line.

- **Engines are plugins.** llama.cpp (`llama-server`) and [Strata](https://github.com/Niko1221/Strata) are built in,
  and a generic `command` plugin runs any program that listens on a port. Adding an engine means writing one file in
  `manager/engines/`.
- **Resource-aware.** Before anything starts, the manager checks free VRAM and RAM against what the profile needs.
  It starts from an estimate and, after each run, uses the peak it measured. Each profile has a policy for what
  happens when something else is running: `exclusive` stops the others first, `coexist` runs alongside them if it
  fits, `refuse` won't start. Switching waits for the GPU memory to be released, checks the new engine is healthy, and
  restores what ran before if the start fails.
- **One action at a time.** Every start, stop, update or rollback is a job in a single queue, so remote requests
  never collide.
- **Updates.** llama.cpp updates from the official GitHub builds; the old build is kept for one-click rollback.
  Strata updates through its own `UPDATE.bat`. By default each engine checks for updates before it starts, at most
  every 6 hours, and a failed update never stops the model from starting.
- **Stats.** Live tok/s, tokens, requests, VRAM/RAM/CPU for whatever is running, plus totals for today, this week,
  this month, this year and all time. Both engines report the same `llamacpp:*` counters.
- **Models.** A library folder (`E:\models`, one folder per model) and a downloader for **Hugging Face** (search, pick
  a quant), **ModelScope**, the **Ollama library** (its model files are plain GGUFs) and **any URL**. Interrupted
  downloads resume, and aria2c is used for parallel connections when it is installed. A finished download can become
  a llama.cpp profile in one click.
- **Profiles.** Defined in `config.yaml` or managed from the UI (`profiles.yaml`). Discovered automatically for
  Strata (one per `strata-*.json`). A *custom run* starts a profile once with a different context or extra
  arguments, without saving it.
- **Housekeeping.** Optional stop-when-idle per profile (frees the GPU), restart on crash (at most 3 an hour), and
  engines that were already running get picked up when the manager starts, including ones started by hand.

## Install

One line, from a terminal. It downloads Hangar into `~/hangar`, makes its Python environment and `config.yaml`, puts the
`hangar` command on your PATH and starts it; on Windows it also turns on start with Windows. Running it again updates
in place.

```sh
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/jobski/hangar/main/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/jobski/hangar/main/install.ps1 | iex
```

Options are environment variables: `HANGAR_DIR` (install folder), `HANGAR_REF` (branch or tag, default `main`),
`HANGAR_NO_START=1`, `HANGAR_NO_PATH=1`, and on Windows `HANGAR_NO_AUTOSTART=1`.

**Uninstall.** `sh ~/hangar/uninstall.sh` on macOS/Linux, `& "$HOME\hangar\uninstall.ps1"` on Windows (or pipe the
`uninstall.*` URL the same way). It stops the engines it runs and the manager, removes start with Windows (or an old
macOS LaunchAgent), the command and the PATH entry, and deletes the folder. `config.yaml`, `profiles.yaml` and `data/` (token, stats) are copied to
`~/hangar-backup-<date>` first unless you pass `--purge` (`-Purge`, or `HANGAR_PURGE=1`); `--yes` (`-Yes`) skips the
question. Model files and engine folders are never touched, and another Hangar install on the same machine is left
alone.

## Set up by hand

```powershell
cd E:\llm-manager
.\scripts\setup.ps1              # .venv, requirements, config.yaml from the example, prints the token
notepad config.yaml              # paths, ports, API keys, profiles
.\scripts\start.ps1              # runs in the background on port 9000 (-Foreground to watch it)
.\hangar.cmd autostart on         # start it whenever you sign in to Windows
```

### Start with Windows

Hangar can start itself whenever you sign in to Windows. Turn it on or off with the **Start with Windows** switch on
the Overview page, with `hangar autostart on|off` (no argument shows the current state), with the MCP tool
`autostart_windows`, or with `POST /api/autostart {"enabled": true}`. `scripts\install-autostart.ps1 [-Remove]` does
the same without the manager running.

It puts a `Hangar.lnk` shortcut in your Startup folder (`shell:startup`) that runs `scripts\start.ps1` with no
window. No admin rights are needed. Only the manager starts; engines still start on demand, or when a client asks for a
profile. Because it is a sign-in shortcut, Hangar runs once you sign in, not before. If you move the folder, switch it
off and on again so the shortcut points at the new place (the page warns when it is stale).

Open `http://<pc>:9000` and paste the token from `data\token.txt`. Opening `http://<pc>:9000/?token=<token>` once
also signs a device in.

### macOS (Apple Silicon)

Hangar runs on a Mac too. On Apple Silicon the GPU shares the machine's memory, so Hangar shows what Metal may use
(about 75% of RAM on 32 GB+ Macs, or `iogpu.wired_limit_mb` if you have raised it) as GPU memory, with the GPU load
from macOS's own statistics (no sudo; there is no temperature reading).

```sh
git clone https://github.com/jobski/hangar.git ~/hangar && cd ~/hangar
scripts/setup.sh                 # .venv, requirements, config.yaml from config.example.mac.yaml, prints the token
open -e config.yaml              # paths, ports, profiles
scripts/start.sh                 # runs in the background on port 9000 (--foreground to watch it)
./hangar update llamacpp         # installs llama.cpp's Metal build into ~/llama.cpp, and keeps it updated
```

For a coding assistant, llama.cpp (`llama-server` with Metal) is the best fit: Hangar installs and updates it, reads
its token counts and speeds, and agents get tool calling (`--jinja`) and prompt caching. `config.example.mac.yaml`
has a Qwen3-Coder 30B-A3B profile; download the GGUF from the Models page (e.g. `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`,
`Q4_K_M`, about 18 GB) and it suits a 32 GB+ Mac.

**No start at login on macOS.** Endpoint security tools such as SentinelOne quarantine LaunchAgents written by
scripts, so Hangar does not make one: after a restart, run `scripts/start.sh` (or add it to Login Items yourself).
If an earlier version left `~/Library/LaunchAgents/dev.hangar.manager.plist` behind, `hangar autostart off` removes it.

**MLX.** The `mlx` engine runs mlx-lm's server (`mlx_lm.server`) the way you would by hand, one profile per model:
`model:` is a Hugging Face repo id such as `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` or a local folder, and
every MLX model already in your Hugging Face cache shows up as a profile by itself. Update installs the newest mlx-lm
from PyPI into the engine's Python (rollback reinstalls the one before). It is often a little faster at generating
than llama.cpp, but mlx-lm has no metrics page or API key, so Hangar shows it running, its memory and its log, not
token counts, and anyone who can reach its port can use it (keep `host: 127.0.0.1` if only this Mac needs it).

### Memory budget

Tell Hangar how much of a machine its engines may use, VRAM and RAM together — say 15 GB on a Mac you also work on.
Set it with the **Budget** button on the Overview's Machine panel, `hangar budget 15` (`hangar budget off` to clear),
the MCP tool `memory_budget`, or `resources.memory_budget_gb` in `config.yaml`. Then:

- starting a profile that would go over it (counting what keeps running next to it) is refused, with how much it
  needs; force still starts it;
- a profile made from a downloaded model gets the largest context that fits: Hangar reads the model's layers and KV
  heads from the GGUF header, so it knows what each 1K tokens of context costs with the cache type in the template,
  and it never goes above the template's `ctx` or the model's own maximum (the profile's description says what it
  chose);
- the Models page marks each quant **fits** or **over budget** (its files plus ~1.5 GB for an 8K context), and the
  Profiles page tags profiles that need more than the budget.

On a Mac without a budget, new profiles still fit what Metal lets the GPU use. Memory estimates for llama.cpp
profiles use the same GGUF numbers (with sliding-window models such as Gemma they are on the high side), and are
replaced by measured use after a run.

### Several machines (peers)

Each machine runs its own Hangar; peering lets one dashboard, CLI and MCP server see and control all of them — say a
Windows PC with CUDA and a Mac with MLX. Add a machine from **Overview → Machines → + Add machine** (or the machine
switcher in the top bar): its name, the address this machine reaches it at (`http://macbook:9000`; Tailscale names
work well), and its token (`data/token.txt` on that machine). Ticking *Also add this machine there* pairs both ways in
one go.

```
hangar peer add macbook http://macbook:9000 <its token> --both http://my-pc:9000
hangar machines                              # every machine: online, what runs and how fast, memory
hangar --on macbook status                   # any command on a peer: start, stop, logs -f, connect, ...
```

The **machine switcher** at the top switches the whole dashboard to that machine — engines, profiles, models, logs,
Connect, usage — with a strip saying which one you are acting on. Behind it, `/api/peers/<name>/<path>` relays any API
call (live log streams included) to that peer with its token. In MCP, `machines` lists them and `status`,
`list_profiles`, `plan`, `start`, `switch_model`, `stop`, `restart`, `check_updates`, `update_engine`, `job_status`,
`logs`, `stats`, `events` and `client_config` take `machine="macbook"`; `add_machine`/`remove_machine` manage the list.
Peers can also be written in `config.yaml` (`peers: {macbook: {url: ..., token: ...}}`); `manager.name` sets how a
machine is called (default: its host name). Peers of a peer are not reachable through it — add each machine where you
want to see it.

### Remote access

Use [Tailscale](https://tailscale.com) (it is already installed on this PC): every device on your tailnet reaches
`http://<pc-name>:9000` with no router changes. Windows Firewall may ask once to allow Python. Everything except the
bare page needs the token, but don't forward port 9000 on your router.

### MCP

The MCP server is at `http://<pc>:9000/mcp` (streamable HTTP) and takes the same token.

```bash
claude mcp add --transport http hangar http://<pc>:9000/mcp --header "Authorization: Bearer <token>"
```

Tools: `status`, `list_profiles`, `plan`, `start`, `switch_model`, `stop`, `restart`, `run_custom`, `check_updates`,
`update_engine`, `rollback_engine`, `job_status`, `logs`, `stats`, `stats_series`, `events`, `list_models`,
`search_models`, `repo_files`, `download_model`, `downloads_status`, `cancel_download`, `create_profile_from_model`,
`save_profile`, `copy_profile`, `delete_profile`, `client_config`, `autostart_windows`, `machines`, `add_machine`, `remove_machine`, `memory_budget`.

### Connecting coding agents and other apps

The **Connect** section of the web page (and `hangar connect`) gives ready-to-paste settings for the running
models: opencode, Qwen Code, Pi, Codex CLI, Claude Code, Aider, Continue, or any OpenAI-compatible app. The second tab
does the same for this manager's MCP server (Claude Code, Claude Desktop, Codex, Qwen Code, Gemini CLI, opencode,
Cursor, VS Code). Pick the address the other machine should use (Tailscale name/IP, LAN, or 127.0.0.1 for this PC).
For opencode, Qwen Code and Pi, "Write to this PC" merges the settings into that tool's config file and keeps a
backup. Over MCP, the `client_config` tool returns the same snippets.

```
hangar hosts                              hangar connect opencode --host 100.101.102.103
hangar connect qwen --apply               hangar connect mcp codex --host my-pc.tailnet.ts.net
```

### hangar (CLI)

`hangar.cmd` runs on this PC (`hangar.cmd` is the old name and still works). On another PC, copy the repo and run
`hangar login http://<pc>:9000 <token>` once.

```
hangar status                         hangar stats            (day/week/month/year/all)
hangar switch tiel-coder-35b          hangar stop all
hangar check                          hangar update llamacpp  hangar rollback llamacpp
hangar search qwen3 coder             hangar files unsloth/Qwen3-8B-GGUF
hangar download unsloth/Qwen3-8B-GGUF "*Q4_K_M*"
hangar download qwen3:8b --ollama     hangar downloads
hangar custom tiel-coder-35b --ctx 65536 -- --temp 0.6
hangar autostart on                   (start with Windows; off to stop)
hangar logs strata -f                 (follow the log live; Ctrl+C stops)
```

REST: everything is under `/api` (interactive docs at `/api/docs`). Actions return a job; add `?wait=60` to wait for
it. `GET /api/logs/<engine>/stream` follows an engine log as plain text until you disconnect.

## Adding a model

1. **Get models** in the web page: search, tick a quant (and the mmproj if you want vision), Download. Tick *make a
   profile* to get a llama.cpp profile when it finishes. It uses `templates.llamacpp` from `config.yaml`.
2. Or put the files in `E:\models\<name>\` yourself and press **Make profile** in the library.
3. Tune the profile with **Edit** (context, arguments, `idle_stop_minutes`, `restart_on_crash`, policy), or try
   changes first with **Custom run**.

Keep each model's mmproj in that model's own folder: an mmproj only works with the model it was made for.

## Adding an engine

Subclass `Engine` in `manager/engines/` and register it in `manager/engines/__init__.py`. Implement `launch(profile)`;
optionally implement `health`, `estimate`, `discover_profiles`, `match_process`, `metrics_path` (Prometheus counters
with llama.cpp's names), and `check_update` / `update` / `rollback`. Everything else (processes, resources, jobs,
stats, UI, MCP) comes from the core. For a quick start, use the `command` plugin (see `config.example.yaml`).

## Files

| Path | What |
|---|---|
| `config.yaml` | your settings (git-ignored; `config.example.yaml` is the template) |
| `profiles.yaml` | profiles made from the UI (git-ignored) |
| `data/token.txt` | the API token (delete it and restart to make a new one) |
| `data/stats.db` | usage history, sessions, events (SQLite) |
| `logs/<engine>.log` | each engine's output |
