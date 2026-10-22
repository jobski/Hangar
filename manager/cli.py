"""hangar: the manager from a terminal, on this PC or any other. (llmctl still works as an alias.)

    hangar login http://my-pc:9000 <token>    remember where the manager is (saved in ~/.hangar.json)
    hangar --version                       this client's Hangar release
    hangar status | profiles | plan <profile>
    hangar start <profile> [--force] [--no-update | --update]
    hangar start last [engine]    what that engine (or the last one stopped) ran last
    hangar switch <profile|role>  start a profile; a role: bring its model up, switching its machine if needed
    hangar switching [set k=v ... | setup <machine> <thinker> <coder>]   request-driven model switching
    hangar stop <engine|all> | restart <engine>
    hangar check [engine] | update <engine> | rollback <engine>
    hangar logs [engine] [lines] [-f]      -f keeps printing new lines (Ctrl+C to stop); no engine: the whole
                                           machine - every engine and Hangar's events, following across switches
    hangar jobs | job <id> | events
    hangar stats [day|week|month|year|all]
    hangar requests [period] [--by target|client|caller|model|machine] [--latest]   what the gateway (/v1) passed on

  models
    hangar models                                   the model library
    hangar search <words> [--modelscope]           find GGUF repos
    hangar files <repo> [--modelscope|--ollama]    a repo's quants and sizes
    hangar download <repo> <pattern>... [--modelscope|--ollama|--url] [--no-profile]
          e.g. hangar download unsloth/Qwen3-8B-GGUF "*Q4_K_M*"     hangar download qwen3:8b --ollama
    hangar downloads                                progress, and unfinished downloads
    hangar downloads resume <folder|all> | discard <folder>   pick up or clean up an unfinished one
    hangar new-profile <model.gguf> [name] [ctx]    a llama.cpp profile from the template
    hangar custom <profile> [--ctx N] [-- extra llama args...]   one-off run with changes

  connect other tools
    hangar connect <client> [--host H] [--apply]   config for opencode, qwen, pi, omp, codex, claude, aider, continue,
                                                   generic; --apply writes it on the machine you run this on
    hangar connect mcp [client] [--host H] [--apply]   this manager as an MCP server (claude, codex, qwen, opencode, ...)
    hangar hosts                                    addresses other machines can use for --host

  this PC
    hangar autostart [on|off]                       start Hangar when you sign in to Windows (no argument: show)
    hangar roles                                    roles (main, helper, ...) and which model serves each now
    hangar role set <name> <machine/profile> [<machine/profile> ...] [--auto]   first = primary, then backups;
          --auto starts backups when needed (machine "this" = here). e.g. hangar role set helper macbook/qwen3-coder this/gemma-3-4b --auto
          --spill: while a target is busy, the role's requests go to a free target of the same role (--no-spill: off)
    hangar role rm <name>
    hangar reasoning <engine> [level] [--restart]  a thinking model's effort: none/low/medium/high/...; show without level
    hangar budget [GB|off]                          memory Hangar's engines may use here (no argument: show)

  other machines (peers: other Hangars, e.g. a Mac running MLX)
    hangar machines                                 this machine and its peers: online, what runs, memory
    hangar pair-code                                this machine's pairing code, to paste into another Hangar
    hangar peer add <pairing-code> [--both] [--name N]   add a machine from its code; --both adds this one there too
    hangar peer add <name> <url> <token> [--both]   the same with an address and token
    hangar peer remove <name>
    hangar wake <machine> [--auto on|off]           wake-on-LAN now; --auto: also whenever a request needs it
    hangar --on <machine> <command> ...             run any command on a peer, e.g. hangar --on macbook status
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

CONF = Path.home() / ".hangar.json"
OLD_CONF = Path.home() / ".llmctl.json"        # from before the rename; still read
ROOT = Path(__file__).resolve().parent.parent


def _conn() -> tuple[str, str]:
    conf = CONF if CONF.exists() else OLD_CONF
    if conf.exists():
        from .config import private_file
        private_file(conf)
    saved = json.loads(conf.read_text()) if conf.exists() else {}
    url = os.environ.get("HANGAR_URL") or os.environ.get("LLMCTL_URL") or saved.get("url") or "http://127.0.0.1:9000"
    tok = os.environ.get("HANGAR_TOKEN") or os.environ.get("LLMCTL_TOKEN") or saved.get("token")
    if not tok and (ROOT / "data" / "token.txt").exists():
        tok = (ROOT / "data" / "token.txt").read_text().strip()
    return url.rstrip("/"), tok or ""


ON: str | None = None      # --on <machine>: send every call to that peer through this Hangar


def _path(path: str) -> str:
    return path.replace("/api/", f"/api/peers/{ON}/", 1) if ON and path.startswith("/api/") else path


def _call(method: str, path: str, **kw):
    url, tok = _conn()
    path = _path(path)
    try:
        r = httpx.request(method, url + path, headers={"Authorization": f"Bearer {tok}"}, timeout=660, **kw)
    except httpx.HTTPError as e:
        sys.exit(f"cannot reach the manager at {url}: {e}")
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail") or r.json().get("error")
        except ValueError:
            msg = r.text
        sys.exit(f"error {r.status_code}: {msg}")
    return r.json() if "json" in r.headers.get("content-type", "") else r.text


def _mib(v):
    return f"{v / 1024:.1f} GB" if v and v >= 1024 else f"{v or 0:.0f} MiB"


def _dur(s):
    s = int(s or 0)
    return f"{s // 86400}d {s % 86400 // 3600}h" if s >= 86400 else f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s"


def _num(v):
    v = v or 0
    return f"{v / 1e9:.2f}B" if v >= 1e9 else f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.1f}K" if v >= 1e3 else f"{v:.0f}"


def _follow(job: dict):
    """Print a job's log as it runs."""
    seen = None
    while True:
        log = job.get("log", [])
        if "log_start" in job:
            start = job["log_start"]
            if seen is not None and start > seen:
                print(f"({start - seen} older log lines expired)")
            for line in log[max(0, (seen or start) - start):]:
                print(line)
            seen = start + len(log)
        else:
            for line in log[seen or 0:]:
                print(line)
            seen = len(log)
        if job["state"] not in ("queued", "running"):
            break
        time.sleep(2)
        job = _call("GET", f"/api/jobs/{job['id']}?lines=2000&since={seen or 0}")
    print(f"-> {job['state']}" + (f": {job['error']}" if job.get("error") else ""))
    return 0 if job["state"] == "done" else 1


def cmd_status(_):
    s = _call("GET", "/api/status")
    r = s["resources"]
    for g in r["gpus"]:
        print(f"GPU {g['index']} {g['name']}: {_mib(g['vram_used_mib'])} / {_mib(g['vram_total_mib'])} VRAM, "
              f"{g['util_percent'] or 0:.0f}% busy, {g['temp_c'] or 0:.0f} C")
    print(f"RAM {_mib(r['ram_total_mib'] - r['ram_available_mib'])} / {_mib(r['ram_total_mib'])}, CPU {r['cpu_percent']:.0f}%")
    for w in s["warnings"]:
        print("warning:", w)
    print()
    for e in s["engines"]:
        upd = e["update"]
        v = f" [{upd.get('current') or '?'}" + (f" -> {upd['latest']} available" if upd.get("available") else "") + "]"
        run = e["running"]
        if run:
            st = run["stats"]
            w = st["window"]["5m"]
            print(f"{e['name']:10s} :{e['port']}  {run['state'].upper():8s} {run['profile']}  up {_dur(run['uptime_seconds'])}"
                  f"  RAM {_mib(run['ram_mib'])}{v}")
            tps = f"{w['gen_tps']:.1f} tok/s" if w["gen_tps"] else "idle"
            print(f"{'':16s}last 5 min: {tps}, {_num(w['gen_tokens'])} out / {_num(w['prompt_tokens'])} in; since start "
                  f"{_num(st['since_start']['gen_tokens'])} out / {_num(st['since_start']['prompt_tokens'])} in"
                  + ("" if run["managed"] else "  (started outside the manager)"))
        else:
            other = e.get("port_taken_by")
            last = e.get("last")
            print(f"{e['name']:10s} :{e['port']}  stopped" + (f" (port used by {other['name']} pid {other['pid']})" if other else "") + v
                  + (f"  last: {last['profile']} (hangar start last {e['name']})" if last else ""))
    if s["job"]:
        print(f"\nrunning job: {s['job']['title']} ({s['job']['state']})")


def cmd_profiles(_):
    for p in _call("GET", "/api/profiles"):
        n = p.get("needs", {})
        print(f"{'*' if p['running'] else ' '} {p['id']:32s} {p['engine']:9s} {p['policy']:9s} "
              f"~{_mib(n.get('vram_mib'))} VRAM, ~{_mib(n.get('ram_mib'))} RAM ({n.get('source', '?')})  {p['description']}")


def cmd_plan(a):
    print(json.dumps(_call("GET", "/api/plan", params={"profile": a[0], "force": "--force" in a}), indent=1))


def _start_args(a):
    upd = "skip" if "--no-update" in a else "force" if "--update" in a else "auto"
    return {"profile": a[0], "force": "--force" in a, "update": upd}


def cmd_start(a):
    if a and a[0] == "last":
        engines = [e for e in _call("GET", "/api/status")["engines"] if not e["running"] and e.get("last")]
        if len(a) > 1 and not a[1].startswith("--"):
            engines = [e for e in engines if e["name"] == a[1]]
        if not engines:
            sys.exit("nothing to start again: " + ("that engine is running or has no earlier run" if len(a) > 1 else "every engine is running or has no earlier run"))
        e = max(engines, key=lambda e: e["last"]["stopped"] or 0)
        print(f"starting {e['last']['profile']} again on {e['name']}")
        a = [e["last"]["profile"]] + [x for x in a[1:] if x.startswith("--")]
    return _follow(_call("POST", "/api/start", json=_start_args(a)))


def cmd_switch(a):
    """A role name: bring its model up (switching its machine if the role switches), following it until it is up or
    fails, exiting non-zero then; a profile: start it."""
    if not (a and not a[0].startswith("-") and a[0] != "last" and a[0] in {r["name"] for r in _call("GET", "/api/roles")}):
        return cmd_start(a)
    role, began, last_reason, stalled = a[0], time.monotonic(), None, None

    def target_state():
        r = next((r for r in _call("GET", "/api/roles") if r["name"] == role), None)
        return ((r or {}).get("targets") or [{}])[0].get("state")
    while time.monotonic() - began < 600:
        # "waiting" schedules nothing (another model keeps its minimum turn): ask again. "switching": a start is
        # queued, so follow the target's state - asking again could queue a second one
        r = _call("POST", "/api/warm", json={"model": role})
        state = r.get("state")
        if state == "waiting":
            if r.get("reason") != last_reason:
                print(f"{role}: waiting - {r.get('reason')}" + (f" (about {r['wait_s']} s)" if r.get("wait_s") else ""))
                last_reason = r.get("reason")
            time.sleep(min(max(r.get("wait_s") or 5, 2), 15))
            continue
        if state != "switching":
            break
        print(f"{role}: switching its machine to it")
        while time.monotonic() - began < 600:
            time.sleep(3)
            now = target_state()
            if now == "ready":
                print(f"{role}: ready")
                return
            stalled = None if now in ("starting", "stopping") else (stalled or time.monotonic())
            if stalled and time.monotonic() - stalled > 20:     # the start ended without the model up: why
                r = _call("POST", "/api/warm", json={"model": role})
                state = r.get("state")
                break
        break
    else:
        r, state = {"reason": "still not up after 600 s; it may yet come up"}, "timeout"
    print(f"{role}: {state}" + (f" - {r['reason']}" if r.get("reason") else ""))
    if state not in ("ready",):
        sys.exit(1)


def cmd_switching(a):
    """hangar switching | switching set key=value ... | switching setup <machine> <thinker> <coder>"""
    if not a:
        print(json.dumps(_call("GET", "/api/switching"), indent=2))
    elif a[0] == "set" and len(a) > 1:
        changes = {}
        for kv in a[1:]:
            k, _, v = kv.partition("=")
            changes[k] = None if v in ("", "none") else (float(v) if v.replace(".", "", 1).isdigit() else v)
        print(json.dumps(_call("POST", "/api/switching", json=changes), indent=2))
    elif a[0] == "setup" and len(a) == 4:
        r = _call("POST", "/api/switching/setup", json={"machine": a[1], "thinker": a[2], "coder": a[3]})
        print(f"{r['plan']} -> {a[1]}/{a[2]}, {r['build']} -> {a[1]}/{a[3]} (home), main -> the coder; "
              f"then: hangar connect opencode|omp|qwen --apply")
    else:
        sys.exit("usage: hangar switching [set key=value ... | setup <machine> <thinker-profile> <coder-profile>]")


def cmd_stop(a):
    return _follow(_call("POST", "/api/stop", json={"engine": a[0] if a else "all"}))


def cmd_restart(a):
    return _follow(_call("POST", "/api/restart", json={"engine": a[0]}))


def cmd_check(a):
    return _follow(_call("POST", "/api/check-updates", json={"engine": a[0] if a else None}))


def cmd_update(a):
    return _follow(_call("POST", "/api/update", json={"engine": a[0], "start_after": "--no-start" not in a}))


def cmd_rollback(a):
    return _follow(_call("POST", "/api/rollback", json={"engine": a[0], "start_after": "--no-start" not in a}))


def cmd_logs(a):
    """hangar logs [engine] [lines] [-f]: one engine's log, or (no engine) the whole machine's - every engine and
    Hangar's events, each line tagged, so -f keeps following across a switch from one engine to another."""
    follow = "-f" in a or "--follow" in a
    pl = [x for x in a if x not in ("-f", "--follow")]
    engine = pl[0] if pl and not pl[0].isdigit() else None
    nums = [x for x in pl if x.isdigit()]
    lines = int(nums[0]) if nums else 100
    snapshot, stream = ((f"/api/logs/{engine}", f"/api/logs/{engine}/stream") if engine else
                        ("/api/machine-log", "/api/machine-log/stream"))
    if not follow:
        print(_call("GET", snapshot, params={"lines": lines}))
        return
    url, tok = _conn()
    try:
        with httpx.stream("GET", url + _path(stream), params={"lines": lines},
                          headers={"Authorization": f"Bearer {tok}"}, timeout=httpx.Timeout(10, read=None)) as r:
            if r.status_code >= 400:
                r.read()
                sys.exit(f"error {r.status_code}: {r.text}")
            for chunk in r.iter_text():
                sys.stdout.write(chunk)
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    except httpx.HTTPError as e:
        sys.exit(f"log stream ended: {e}")


def cmd_jobs(_):
    for j in _call("GET", "/api/jobs"):
        print(f"{j['id']}  {time.strftime('%m-%d %H:%M', time.localtime(j['created']))}  {j['state']:8s} {j['title']}"
              + (f"  - {j['error']}" if j.get("error") else ""))


def cmd_job(a):
    return _follow(_call("GET", f"/api/jobs/{a[0]}?lines=100000"))


def cmd_events(_):
    for e in reversed(_call("GET", "/api/events", params={"limit": 40})):
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(e['ts']))} {e['level']:5s} {e['kind']:8s} {e['message']}")


def cmd_stats(a):
    periods = a or ["day", "week", "month", "year", "all"]
    for p in periods:
        s = _call("GET", "/api/stats/summary", params={"period": p})
        t = s["total"]
        tps = f"{t['gen_tps']:.1f} tok/s" if t["gen_tps"] else "-"
        print(f"{p:6s} out {_num(t['gen_tokens']):>8s}  in {_num(t['prompt_tokens']):>8s}  cached {_num(t['cached_tokens']):>8s}"
              f"  avg {tps:>11s}  up {_dur(t['uptime_seconds'])}  runs {t['runs']}")
        if len(periods) == 1:
            for name, v in sorted(s["profiles"].items(), key=lambda x: -x[1]["gen_tokens"]):
                print(f"   {name:32s} out {_num(v['gen_tokens']):>8s}  in {_num(v['prompt_tokens']):>8s}  up {_dur(v['uptime_seconds'])}")


def cmd_requests(a):
    by = a[a.index("--by") + 1] if "--by" in a and a.index("--by") + 1 < len(a) else "target"
    if "--latest" in a:
        for r in reversed(_call("GET", "/api/requests", params={"limit": 30})):
            who = (f"{r['machine']}/{r['profile']}" + (f" [{r['error']}]" if r["error"] else "")) if r["machine"] \
                else f"HTTP {r['status']} {r['error'] or ''}"
            tps = f"{r['gen_tokens'] / r['gen_s']:.1f} tok/s" if (r.get("gen_s") or 0) >= 0.05 and (r.get("gen_tokens") or 0) >= 4 else "-"
            src = f"{r['client'] or '-'}@{r.get('caller') or '?'}"
            print(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(r['ts']))} {r['model']:10s} {who:44s} {src:24s}"
                  f" out {_num(r['gen_tokens'] or 0):>6s}  {tps:>11s}  {r['duration_s'] or 0:6.1f} s")
        return
    period = next((x for x in _plain(a) if x != by), "day")
    s = _call("GET", "/api/requests/summary", params={"period": period, "by": by})
    keys = {"target": ("machine", "profile"), "client": ("client",), "caller": ("caller",), "model": ("model",),
            "machine": ("machine",)}[by]
    for r in s["rows"] + [{**s["total"], "_total": True}]:
        name = "total" if r.get("_total") else "/".join(str(r[k] or "-") for k in keys)
        tps = f"{r['gen_tps']:.1f} tok/s" if r["gen_tps"] else "-"
        ttft = f"{r['ttft_s']:.2f} s" if r.get("ttft_s") else "-"
        print(f"{name:44s} {r['requests']:5d} req  {r['errors']:3d} failed  out {_num(r['gen_tokens']):>8s}  in {_num(r['prompt_tokens']):>8s}"
              f"  {tps:>11s}  first token {ttft}")


def _source(a):
    for flag, src in (("--modelscope", "modelscope"), ("--ollama", "ollama"), ("--url", "url")):
        if flag in a:
            return src
    return "huggingface"


def _plain(a):
    return [x for x in a if not x.startswith("--")]


def cmd_models(_):
    for m in _call("GET", "/api/models"):
        print(f"{m['name']}  ({_mib(m['size'] / 2**20)}, {m['kind']})  {m['folder']}")
        for g in m["models"]:
            print(f"   model  {g['name']}  {_mib(g['size'] / 2**20)}" + (f"  used by {', '.join(g['used_by'])}" if g["used_by"] else "")
                  + ("" if g["complete"] else "  INCOMPLETE"))
        for g in m["mmproj"]:
            print(f"   mmproj {g['name']}  {_mib(g['size'] / 2**20)}")
        if m["partial"]:
            print(f"   partial downloads: {', '.join(m['partial'])}")


def cmd_search(a):
    for r in _call("GET", "/api/search", params={"q": " ".join(_plain(a)), "source": _source(a)}):
        print(f"{r['repo']:60s} {r.get('downloads') or 0:>9} downloads")


def cmd_files(a):
    res = _call("GET", "/api/repo-files", params={"repo": _plain(a)[0], "source": _source(a)})
    for g in res["groups"]:
        print(f"{(g['quant'] or ('mmproj' if g['mmproj'] else '?')):14s} {_mib(g['size'] / 2**20):>9s}  {g['key']}"
              + (f"  ({len(g['files'])} files)" if len(g["files"]) > 1 else ""))
    if not res["groups"]:
        for f in res["files"]:
            print(f"{_mib((f['size'] or 0) / 2**20):>9s}  {f['path']}")


def cmd_download(a):
    pl = _plain(a)
    d = _call("POST", "/api/downloads", json={"source": _source(a), "repo": pl[0], "include": pl[1:] or None,
                                              "make_profile": "--no-profile" not in a})
    print(f"download {d['id']}: {len(d['files'])} files, {_mib(d['total_bytes'] / 2**20)} -> {d['folder']}")
    print("follow it with: hangar downloads")


def cmd_downloads(a):
    if a and a[0] in ("resume", "discard"):
        if len(a) < 2:
            sys.exit(f"usage: hangar downloads {a[0]} <folder>" + (" | all" if a[0] == "resume" else ""))
        left = _call("GET", "/api/downloads")["leftovers"]
        targets = [lo["folder"] for lo in left if lo["resumable"]] if a[1] == "all" and a[0] == "resume" else [a[1]]
        for t in targets:
            r = _call("POST", f"/api/downloads/leftovers/{a[0]}", json={"folder": t})
            print(f"resumed {r['repo']} into {r['folder']} (id {r['id']})" if a[0] == "resume" else f"removed {', '.join(r['removed']) or 'nothing'}")
        if not targets:
            print("nothing to resume")
        return
    res = _call("GET", "/api/downloads")
    print(f"aria2c: {res['aria2'] or 'not installed (single connection downloads)'}")
    for d in res["items"]:
        pct = d["done_bytes"] / d["total_bytes"] * 100 if d["total_bytes"] else 0
        eta = f", ETA {_dur(d['eta_seconds'])}" if d["eta_seconds"] else ""
        print(f"{d['id']}  {d['state']:9s} {pct:5.1f}%  {_mib(d['done_bytes'] / 2**20)} / {_mib(d['total_bytes'] / 2**20)}"
              f"  {d['speed'] / 2**20:.1f} MiB/s{eta}  {d['repo']}" + (f"  -> profile {d['result']['profile']}" if d["result"].get("profile") else "")
              + (f"  {d['error']}" if d["error"] else ""))
    for lo in res.get("leftovers", []):
        frac = lo["have_bytes"] / lo["total_bytes"] * 100 if lo["total_bytes"] else None
        pct = (f"{frac:.1f}%" if frac < 10 else f"{frac:.0f}%") if frac is not None else _mib(lo["have_bytes"] / 2**20)
        print(f"unfinished {lo['name']}: {pct} of {lo['repo'] or 'unknown source'}"
              + ("  (hangar downloads resume|discard " + lo["name"] + ")" if lo["resumable"] else "  (no record: start it again to continue, or discard)"))


def cmd_new_profile(a):
    body = {"model": a[0], "name": a[1] if len(a) > 1 else None, "ctx": int(a[2]) if len(a) > 2 else None}
    print("created profile", _call("POST", "/api/profiles/from-model", json=body)["id"])


def cmd_custom(a):
    extra = a[a.index("--") + 1:] if "--" in a else []
    head = a[:a.index("--")] if "--" in a else a
    ov = {"args_add": extra}
    if "--ctx" in head:
        ov["ctx"] = int(head[head.index("--ctx") + 1])
    return _follow(_call("POST", "/api/run-custom", json={"profile": head[0], "overrides": ov}))


def cmd_hosts(_):
    for h in _call("GET", "/api/connect/hosts"):
        print(f"{h['host']:32s} {h['label']}")


def cmd_connect(a):
    host = a[a.index("--host") + 1] if "--host" in a and a.index("--host") + 1 < len(a) else "127.0.0.1"
    pl = [x for x in _plain(a) if x != host]
    if pl[0] == "mcp":
        snips = _call("GET", "/api/connect/mcp", params={"host": host})
        snip = next((s for s in snips if s["id"] == (pl[1] if len(pl) > 1 else "claude")), None)
        if not snip:
            sys.exit("unknown client; one of " + ", ".join(s["id"] for s in snips))
    else:
        snip = _call("GET", "/api/connect/models", params={"client": pl[0], "host": host})
    if "--apply" in a:
        if not snip.get("apply"):
            sys.exit(f"{snip['title']} cannot be written automatically; run without --apply and paste it ({snip['file']})")
        from .clients import CONFIG_FILES, apply         # the tool's config lives here, not on the manager's machine
        res = apply(snip["apply"], {**snip, "file": str(CONFIG_FILES[snip["apply"]])})
        print(f"written to {res['file']}" + (f" (backup {res['backup']})" if res["backup"] else ""))
        return
    print(f"# {snip['title']} -> {snip['file']}\n")
    print(snip["content"])
    if snip.get("notes"):
        print(f"\n# {snip['notes']}")


def cmd_autostart(a):
    if a and a[0] not in ("on", "off"):
        sys.exit("usage: hangar autostart [on|off]")
    s = _call("POST", "/api/autostart", json={"enabled": a[0] == "on"}) if a else _call("GET", "/api/autostart")
    if not s.get("supported", True):
        print(s["note"] + (f"\nleft over from an earlier version: {s['leftover']} (hangar autostart off removes it)"
                           if s.get("leftover") else ""))
        return
    print(("starts with Windows" if s["enabled"] else "does not start with Windows") + f"  ({s['shortcut']})")
    if s.get("stale"):
        print("the shortcut points at another copy of Hangar; run: hangar autostart on")


def cmd_reasoning(a):
    pl = [x for x in a if not x.startswith("--")]
    if not pl:
        sys.exit("usage: hangar reasoning <engine> [level] [--restart]")
    if len(pl) > 1:
        r = _call("POST", "/api/reasoning", json={"engine": pl[0], "level": pl[1], "restart": "--restart" in a})
        how = {"now": "applied now", "restart": f"restarting {pl[0]} (job {r.get('job')})",
               "next start": "applies at the next start" + (f"; add --restart to apply it now" if r["running"] else "")}[r["applied"]]
        print(f"{r['profile']}: reasoning effort {r['level']} - {how}")
        return
    r = _call("GET", "/api/reasoning", params={"engine": pl[0]})
    if not r["supported"]:
        print(f"{pl[0]} ({r['profile']}) cannot set a reasoning effort")
        return
    print(f"{r['profile']}: reasoning effort {r['level']}  (levels: {', '.join(r['levels'])}; "
          + ("changes apply at once" if r["live"] else "a change restarts the engine") + ")")


def cmd_roles(_):
    for r in _call("GET", "/api/roles"):
        chain = " > ".join(f"{t['machine'] or 'this'}/{t['profile']} [{t['state']}{', auto' if t['auto_start'] else ''}]"
                           + (" *" if t["serving"] else "") for t in r["targets"]) or ("(whatever runs here)" if r["name"] == "main" else "(none)")
        s = r["serving"]
        now = f"{s['machine'] or 'this'}/{s['profile']}" + (" (fallback)" if r["fallback"] else "") if s else "NOTHING UP"
        print(f"{r['name']:10s} {chain}\n{'':10s} serving now: {now}"
              + ("  (a busy target's requests go to a free one in this role)" if r.get("spill") else ""))


def cmd_role(a):
    pl = [x for x in a if not x.startswith("--")]
    if pl[:1] == ["set"] and len(pl) >= 3:
        targets = []
        for i, spec in enumerate(pl[2:]):
            machine, _, prof = spec.rpartition("/")
            targets.append({"machine": "" if machine in ("", "this") else machine, "profile": prof, "auto_start": i > 0 and "--auto" in a})
        spill = True if "--spill" in a else False if "--no-spill" in a else None
        _call("POST", "/api/roles", json={"name": pl[1], "targets": targets, "spill": spill})
        cmd_roles([])
    elif pl[:1] in (["rm"], ["remove"]) and len(pl) >= 2:
        _call("DELETE", f"/api/roles/{pl[1]}")
        print(f"removed role {pl[1]}")
    else:
        sys.exit("usage: hangar role set <name> <machine/profile> [...] [--auto] [--spill|--no-spill] | hangar role rm <name>\n"
                 "  --spill: while a target is busy, its requests go to a free one in the same role")


def cmd_budget(a):
    if a and a[0] not in ("off", "none"):
        try:
            gb = float(a[0].lower().rstrip("gb"))
        except ValueError:
            sys.exit("usage: hangar budget [GB|off]   e.g. hangar budget 15")
        b = _call("POST", "/api/budget", json={"gb": gb})
    elif a:
        b = _call("POST", "/api/budget", json={"gb": None})
    else:
        b = _call("GET", "/api/budget")
    print((f"memory budget {b['budget_mib'] / 1024:.1f} GB" if b["budget_mib"] else "no memory budget")
          + f"; engines use {b['used_mib'] / 1024:.1f} GB now"
          + (f"; new profiles fit in {b['limit_mib'] / 1024:.1f} GB" if b.get("limit_mib") else ""))


def cmd_machines(_):
    res = _call("GET", "/api/peers")
    rows = [{**res["self"], "online": True, "this": True}] + res["peers"]
    w = max(len(m.get("name") or "") for m in rows) + 2
    for m in rows:
        s = m.get("summary") or {}
        state = "this machine" if m.get("this") else ("online" if m["online"] else f"OFFLINE ({m.get('error')})")
        run = ", ".join(f"{r['profile']}" + (f" {r['gen_tps']:.1f} tok/s" if r.get("gen_tps") else "") for r in s.get("running", []))
        mem = f"{_mib(s.get('vram_used_mib'))} / {_mib(s.get('vram_total_mib'))} GPU" if s.get("vram_total_mib") else ""
        name = (m.get("machine") or {}).get("name") or m.get("name")
        wk = m.get("wake") or {}
        wake = "  [wakes on request]" if wk.get("auto") else ("  [can wake]" if wk.get("known") else "")
        print(f"{m.get('name', name):{w}s} {state:14s} {(m.get('machine') or m).get('os', ''):8s} {mem:22s} {run or 'idle'}{wake}")
    f = res.get("fleet")
    if f and res["peers"]:
        pct = lambda v: "-" if v is None else f"{v:.0f}%"  # noqa: E731
        print(f"{'all':{w}s} {str(f['machines']) + ' online':14s} {f['gen_tps']:.1f} tok/s out, "
              f"GPU {_mib(f['vram_used_mib'])} / {_mib(f['vram_total_mib'])} (load {pct(f['gpu_util_percent'])}), "
              f"RAM {_mib(f['ram_used_mib'])} / {_mib(f['ram_total_mib'])}, CPU {pct(f['cpu_percent'])} of {f['cpu_count']} threads")


def cmd_wake(a):
    pl = [x for i, x in enumerate(a) if not x.startswith("--") and not (i and a[i - 1] == "--auto")]
    if not pl:
        sys.exit("usage: hangar wake <machine> [--auto on|off]")
    if "--auto" in a:
        on = a[a.index("--auto") + 1:a.index("--auto") + 2] != ["off"]
        _call("POST", "/api/wake/auto", json={"machine": pl[0], "on": on})
        print(f"{pl[0]} is {'now' if on else 'no longer'} woken when a request needs it")
        return
    res = _call("POST", "/api/wake", json={"machine": pl[0]})
    print(f"sent a wake-on-LAN packet to {pl[0]} ({', '.join(res['sent'])})")


def cmd_pair_code(_):
    c = _call("GET", "/api/pairing-code")
    print(c["code"])
    print(f"\n# {c['name']}, reachable at: {', '.join(c['urls'])}", file=sys.stderr)
    print("# paste it into Add machine on the other Hangar, or there: hangar peer add <code> --both", file=sys.stderr)


def cmd_peer(a):
    named = a[a.index("--name") + 1] if "--name" in a and a.index("--name") + 1 < len(a) else None
    pl = [x for i, x in enumerate(a) if not x.startswith("--") and not (i and a[i - 1] == "--name")]
    if pl[:1] == ["add"] and len(pl) >= 2 and "hangar-pair:" in pl[1]:
        res = _call("POST", "/api/peers", json={"code": pl[1], "name": named, "both": "--both" in a})
        print(f"added {res['name']} ({res['url']})" + (f"; on {res['name']}: {res['reciprocal']}" if res.get("reciprocal") else ""))
    elif pl[:1] == ["add"] and len(pl) >= 4:
        res = _call("POST", "/api/peers", json={"name": pl[1], "url": pl[2], "token": pl[3], "both": "--both" in a})
        print(f"added {res['name']} ({res['url']})" + (f"; on {res['name']}: {res['reciprocal']}" if res.get("reciprocal") else ""))
    elif pl[:1] == ["remove"] and len(pl) >= 2:
        _call("DELETE", f"/api/peers/{pl[1]}")
        print(f"removed {pl[1]}")
    else:
        sys.exit("usage: hangar peer add <pairing-code> [--both] | hangar peer add <name> <url> <token> [--both] | "
                 "hangar peer remove <name>   (hangar pair-code prints this machine's code)")


def cmd_login(a):
    if len(a) < 2:
        sys.exit("usage: hangar login <url> <token>")
    from .config import write_json
    write_json(CONF, {"url": a[0], "token": a[1]})
    print(f"saved to {CONF}")
    print(_call("GET", "/api/status") and "connected")


COMMANDS = {"status": cmd_status, "profiles": cmd_profiles, "plan": cmd_plan, "start": cmd_start, "switch": cmd_switch, "switching": cmd_switching,
            "stop": cmd_stop, "restart": cmd_restart, "check": cmd_check, "update": cmd_update,
            "rollback": cmd_rollback, "logs": cmd_logs, "jobs": cmd_jobs, "job": cmd_job, "events": cmd_events,
            "stats": cmd_stats, "requests": cmd_requests, "wake": cmd_wake, "login": cmd_login, "models": cmd_models, "search": cmd_search, "files": cmd_files,
            "download": cmd_download, "downloads": cmd_downloads, "new-profile": cmd_new_profile, "custom": cmd_custom,
            "connect": cmd_connect, "hosts": cmd_hosts, "autostart": cmd_autostart, "machines": cmd_machines, "pair-code": cmd_pair_code, "budget": cmd_budget, "roles": cmd_roles, "role": cmd_role, "reasoning": cmd_reasoning,
            "peer": cmd_peer}
NEEDS_ARG = {"plan", "start", "switch", "restart", "update", "rollback", "logs", "job", "search", "files", "download",
             "new-profile", "custom", "connect"}


def main(argv=None) -> int:
    global ON
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--version"], ["-V"]):
        from . import __version__
        print(f"Hangar {__version__}")
        return 0
    if "--on" in argv:
        i = argv.index("--on")
        if i + 1 >= len(argv):
            sys.exit("--on needs a machine name (see: hangar machines)")
        ON = argv[i + 1].lower()
        del argv[i:i + 2]
    if not argv or argv[0] in ("-h", "--help", "help") or argv[0] not in COMMANDS:
        print(__doc__)
        return 0 if not argv or argv[0] in ("-h", "--help", "help") else 2
    cmd, rest = argv[0], argv[1:]
    if cmd in NEEDS_ARG and not [x for x in rest if not x.startswith("--")]:
        sys.exit(f"hangar {cmd} needs an argument")
    return COMMANDS[cmd](rest) or 0


if __name__ == "__main__":
    sys.exit(main())
