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

## Set up

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
`save_profile`, `copy_profile`, `delete_profile`, `client_config`, `autostart_windows`.

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
