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
  every 6 hours; an update the background check already found is installed on the next start, and a failed update
  never stops the model from starting.
- **Stats.** Live tok/s, tokens, requests, VRAM/RAM/CPU and context limits for running engines, plus totals for today,
  this week, this month, this year and all time. Gateway usage can be filtered by client, device, engine, role/model,
  machine, profile and result. Engine runtime remains available separately, including traffic sent directly to engines.
- **Models.** A library folder (`E:\models`, one folder per model) and a downloader for **Hugging Face** (search, pick
  a quant), **ModelScope**, the **Ollama library** (its model files are plain GGUFs) and **any URL**. Interrupted
  downloads resume, and aria2c is used for parallel connections when it is installed. A finished download can become
  a llama.cpp profile in one click.
- **Profiles.** Defined in `config.yaml` or managed from the UI (`profiles.yaml`). Discovered automatically for
  Strata (one per `strata-*.json`). A *custom run* starts a profile once with a different context or extra
  arguments, without saving it. Only UI-managed profiles can be deleted from Hangar; the others show **Delete?**,
  which says where the profile comes from and how to remove it there (Hangar never edits `config.yaml` or an
  engine's files). For Strata it also says how to add a model: Strata's own setup (`START-HERE.bat --setup
  --no-start` in the Strata folder) downloads and prepares it, and its new `strata-*.json` becomes a profile.
- **Housekeeping.** Optional stop-when-idle per profile (frees the GPU), restart on crash (at most 3 an hour), and
  engines that were already running get picked up when the manager starts, including ones started by hand.

## Install

One line, from a terminal. It downloads Hangar into `~/hangar`, makes its Python environment and `config.yaml`, puts the
`hangar` command on your PATH and starts it; on Windows it also turns on start with Windows. Running it again updates
in place.

```sh
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/jobski/Hangar/main/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/jobski/Hangar/main/install.ps1 | iex
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
git clone https://github.com/jobski/Hangar.git ~/hangar && cd ~/hangar
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

### Roles and the gateway (main, helper, backups)

Give models a *role* — `main`, `helper`, or any name — and point your tools at the role instead of a server. Each
Hangar has an OpenAI- and Anthropic-compatible **gateway** at `http://<machine>:9000/v1` (API key: the Hangar token,
as `Authorization: Bearer` or `x-api-key`); the model name is the role.

- A role is an ordered list of targets, each a profile on this machine or on a peer: the first is the primary, the
  rest are backups. A request goes to the first target that is up; if none is, to `main`, then to anything running.
  If a target fails before it answers, the next one gets the request.
- A backup marked *start when needed* is started once the targets before it have been down for 30 s — only if it
  fits next to what runs (nothing is stopped for it, and the memory budget holds). Once the primary is back, Hangar
  stops a backup it started after 10 minutes without requests.
- On a machine where `main` takes the whole GPU (Strata here), set no backup: the role falls back to `main`, which
  is already loaded.
- A peer's target goes through that peer's own gateway with its token (its engine keys stay there); a peer on an
  older Hangar is reached on its engine's port directly.

**Use running model as main** is enabled by default in each machine's **Roles** panel. `main` follows the most
recently started ready model on each configured target machine, retaining primary and backup order. Without saved
targets it follows the local running model. A hub follows its configured machines, so changing a Strata model does
not leave it pinned to the previous profile. Turn the option off to use saved profile targets; those targets are retained while automatic following is on. Other roles keep their
configured profiles. Clients should use the Hangar gateway with `model: "main"` to follow these changes.

Set roles on the Overview's **Roles** panel (badges on the engine cards show which roles a model serves), or
`hangar role set helper macbook/qwen3-coder-30b-a3b-instruct-ud-q4_k_xl this/gemma-3-4b --auto`, `hangar roles`,
`hangar role rm <name>`; over MCP, `roles` and `set_role`. The **Connect** page includes a `hangar` provider whose
models are the roles, with the smallest context among each role's targets, so the settings it gives work whichever
target answers.

#### Request log

Every request that passes through a gateway (/v1) is logged on the machine that received it: when, the model or role
asked for, which machine and profile served it, the client (from `X-Hangar-Client`, `X-Title` or the User-Agent —
QwenCode, opencode, claude-cli, ...), tokens in, out and cached, time to first token, tok/s (from the engine's own
timings when it reports them), how long it took, and failures (503 nothing up, 502 every target failed, closed
before the end). A request a hub passes on to another machine is logged there too, under the original client and
marked "via hub".

Where to see it:

- **Usage page** > **Gateway requests** — combine client, device, engine, role/model, machine, profile and result
  filters. The chosen period and filters apply to totals, the chart, grouping and paginated history. Grouping is
  separate from filtering; use **Errors only** to investigate failures and expand a result to read its error details.
  Engine names are recorded for new requests; older records without an engine show **Unknown**.
- CLI: `hangar requests [period] [--by target|client|model|machine]` and `hangar requests --latest`.
- API: `GET /api/requests?period=&limit=&before_id=`, `GET /api/requests/summary?period=&by=`,
  `GET /api/requests/series?period=&bucket=`, and `GET /api/requests/options?period=`. List, summary and series accept
  `client`, `caller` (device), `engine`, `model` (role/model requested), `machine`, `profile` and
  `result=all|success|error`. Use `__unknown__` to select a missing field. Group by `target`, `client`, `caller`,
  `engine`, `model`, `machine` or `profile`. Page with the last row's `id` as `before_id`; timestamp `before` remains
  supported. Error details are bounded upstream messages, rather than raw response bodies.

Handy for comparing two quantisations of the same model on real work: same role, look at tok/s and time to first
token per profile.

### Reasoning effort

How hard a thinking model thinks before it answers. A running engine that supports it shows **Reasoning: <level>**
next to Stop/Restart; press it to change the level.

- **Strata** (`default`, `none`, `low`, `medium`, `high`): changes at once, for every client that does not ask for its
  own level, through Strata's shared Chat settings (which it keeps between starts; its other settings stay as they are).
- **llama.cpp** (`default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`): set when it starts
  (`--reasoning-effort`, or `--reasoning off` for none), so the dashboard offers to restart it; the level is kept per
  profile, also for profiles that are not running. Needs a llama.cpp build that has `--reasoning-effort` (update it if
  the button is missing). A level in the profile's own `args` wins.
- MLX models do not offer it.

`hangar reasoning strata low`, `hangar reasoning llamacpp high --restart` (no level: show it); over MCP,
`reasoning_effort` (with `machine=` for a peer). A client can still set its own level per request.

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
Windows PC with CUDA and a Mac with MLX.

**Pairing takes one copy and one paste.** On the machine you want to add, open **Overview → Machines** and press
**Copy code**. On the machine that should see it, paste that into the same panel (or **+ Add machine**) and press **Add
machine**. The code holds the machine's name, every address it may be reached at (Tailscale name and IP, LAN, PC name)
and its token; Hangar tries them all at once and keeps the first that answers, and with *also add this machine there*
the other side does the same with this machine's code, so each keeps an address it can actually reach. Because the
code contains the token, share it only between your own machines. Typing an address and token still works under *Or
type its address and token*.

```
hangar pair-code                             # this machine's code (on the machine to add)
hangar peer add hangar-pair:... --both       # paste it on the other one
hangar machines                              # every machine: online, what runs and how fast, memory; and all added up
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

**All machines added up.** With peers, the Machines panel opens with the fleet's totals: tokens per second out
across every online machine and how many models run, GPU memory and RAM used of the total, CPU load (weighted by
thread count) and GPU load; each machine's card adds its CPU and GPU load. The top bar ends with an **All** chip
showing the fleet's tok/s (hover for the rest, click to jump to the panel) next to the viewed machine's own gauges.
On Apple Silicon the GPU memory is part of the RAM, so it is counted in both totals. `hangar machines` prints the
same totals as a last line, MCP `machines` returns them as `fleet`, and `/api/peers` carries them under `fleet`.

Engine and machine cards show the running context limit, with the exact token count on hover. A changed profile
setting is shown separately when a restart is needed; a server-adjusted limit does not trigger a restart warning.
Strata launch limits can be recovered on adoption only when the config file has not changed since process start.
If the running limit cannot be established, the card says **Unknown**; live llama.cpp limits come from `/props`, with explicit launch settings available as a fallback.

The **Usage** page's **Engine runtime** view has the same switch: **All machines** adds up every online machine's tokens in and out, cache
reuse, requests and run time for the chosen period, draws one chart for all of them, and lists each profile with its
machine. This Hangar asks each machine for its own numbers (`/api/stats/fleet/summary?period=…` and
`/api/stats/fleet/series?bucket=…&count=…`); a machine that does not answer is named under the switch and left out.
Run time is the machines' run times added together.

#### Wake-on-LAN

A machine reports its LAN network cards (address, netmask, MAC) in its status, and machines that know it as a peer
remember them (an older Hangar that does not report them: the MAC is read from the ARP table once it has been reached
on the LAN).

- **Wake it now:** the **Wake** button on its card under **Overview → Machines** (shown while it is offline), or
  `hangar wake <pc>`.
- **Wake on request:** switch **Wake-on-LAN** to **on request** on its card, or `hangar wake <pc> --auto on`. Then a
  role request that could use that machine wakes it while it is offline. The request is answered at once by whatever
  else is up (the next ones get the woken machine); only when nothing at all can answer does it wait (up to 90 s) for
  the machine to come up, starting the role's model there if it is not running.

Packets are broadcast on the local network, so the machine that sends them must be on the same LAN (a container on
the LAN is fine; over Tailscale it does not work). The machine itself must have wake-on-LAN turned on (BIOS/UEFI and
the network adapter's power settings; on Windows: Device Manager > the adapter > Power Management > "Allow this
device to wake the computer", and Advanced > "Wake on Magic Packet"). Wired Ethernet is the reliable case — most
laptops on Wi-Fi do not wake this way.

### An always-on hub (Linux server or container)

If your tools point at one machine's gateway — say the PC — that machine sleeping or rebooting breaks them. A small
always-on Linux box (a Proxmox LXC, a NAS VM, a Raspberry Pi) running Hangar with no engines can be the one address
your tools use.

Install it like any other machine: on Debian/Ubuntu, `apt install git python3 python3-venv curl lsof`, then the normal
`install.sh` one-liner. Two cores, 2 GB RAM and a few GB of disk are plenty — it runs no models. Give it a name
(`manager.name: hub`) and no engines or profiles of its own.

Start it with the system through a systemd unit (`scripts/hangar.service.example`):

```ini
[Unit]
Description=Hangar
After=network-online.target
Wants=network-online.target

[Service]
User=hangar
WorkingDirectory=/home/hangar/hangar
ExecStart=/home/hangar/hangar/.venv/bin/python -m manager serve
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

    systemctl enable --now hangar

Add the other machines on the hub — **Overview → Machines** or `hangar peer add` (one way is enough: the hub needs
their tokens, they do not need the hub's). Then define the roles on the hub, with targets on the machines — say
`main` = `pc/<big model>` then `macbook/<smaller model>` as a fallback, and `helper` = `macbook/<coder model>`. A role
answers from the first target that is up; when none is, from `main`, then anything running.

Point your tools at the hub: its dashboard's **Connect** page gives each tool's settings (on a machine with no engines
it offers its network address rather than 127.0.0.1), or use `http://<hub>:9000/v1` with the hub's token as the API
key. Responses carry an `X-Hangar-Served-By: machine/profile` header so you can see what actually answered.

The hub is optional — every machine still works on its own, and tools can still point straight at a machine.

**Networking.** The hub must be able to reach the machines' port 9000. A container on the LAN reaches them by LAN
address; a laptop that leaves home is only reachable over a VPN such as Tailscale, so add the laptop by an address that
works where the hub is.

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
`save_profile`, `copy_profile`, `delete_profile`, `client_config`, `autostart_windows`, `machines`, `add_machine`, `remove_machine`, `memory_budget`, `roles`, `set_role`, `reasoning_effort`.

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
hangar start last [engine]            (start what that engine ran last; also a Run button on a stopped engine's card)
hangar requests day                   hangar requests --latest
hangar wake pc                        hangar wake pc --auto on
```

REST: everything is under `/api` (interactive docs at `/api/docs`). Actions return a job; add `?wait=60` to wait for
it. `GET /api/logs/<engine>/stream` follows an engine log as plain text until you disconnect.

### Unfinished downloads

Each download keeps a small record (`.hangar-download.json`) in its folder until it completes. When Hangar starts it
looks for downloads that were cut off — by a restart, a crash or a reboot — and lists them on the Models page under
**Downloads** as *unfinished*, with how much is there: **Resume** continues from what is on disk (with aria2 when it is
installed), **Discard** deletes the partial files and leaves finished ones. Partial files without a record (from an
older version, say) are listed too; start the same download again to continue them, or discard them. Set
`downloads.auto_resume: true` to have them resumed when Hangar starts. From a terminal: `hangar downloads`, then
`hangar downloads resume <folder|all>` or `hangar downloads discard <folder>`; over MCP, `downloads_status`,
`resume_download` and `discard_download`.

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
