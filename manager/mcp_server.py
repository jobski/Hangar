"""MCP tools over the same actions as the REST API. Served at /mcp (streamable HTTP) behind the manager's token."""
from __future__ import annotations

import json

import anyio
import httpx
from mcp.server.mcpserver import MCPServer

from . import autostart, clients
from .core import Manager
from .peers import machine_info, summary

INSTRUCTIONS = """Manages local LLM engines (llama.cpp, Strata, ...) on one PC with one GPU.
Use `status` first. `list_profiles` shows what can run and what each needs. `plan` tells you what starting a profile
would stop or whether it fits, without doing it. start/switch/stop/restart/update return a job; long jobs keep running
after the tool returns - poll `job_status`. Starting usually checks for engine updates first (pass update="skip" to
avoid that). Stats: `stats` for a period (day, week, month, year, all).
Several machines: `machines` lists this one and its peers (other Hangars, e.g. a Mac running MLX); pass machine="<name>"
to status, list_profiles, plan, start, switch_model, stop, restart, check_updates, update_engine, job_status, logs, stats,
events and client_config to act on that machine instead of this one. add_machine/remove_machine manage the list."""


def _j(x) -> str:
    return json.dumps(x, indent=1, default=str)


def build(mgr: Manager) -> MCPServer:
    mcp = MCPServer("hangar", instructions=INSTRUCTIONS)

    async def run(fn, *a):
        return await anyio.to_thread.run_sync(lambda: fn(*a))

    async def peer(machine: str, method: str, path: str, params: dict | None = None, body: dict | None = None,
                   timeout: float = 30) -> str:
        """Run one REST call on a peer and return its answer as text (errors included, so the model sees them)."""
        try:
            p = mgr.peers.get(machine.lower())
        except KeyError:
            names = ", ".join(x["name"] for x in mgr.peers.public()) or "none"
            return f"no machine called {machine}; peers: {names} (omit machine for this one)"
        try:
            async with httpx.AsyncClient(timeout=timeout + 30) as c:
                r = await c.request(method, f"{p['url']}/api/{path}", params=params, json=body,
                                    headers={"Authorization": f"Bearer {p['token']}"})
        except httpx.HTTPError as e:
            return f"{machine} is unreachable ({type(e).__name__})"
        try:
            return _j(r.json()) if r.status_code < 400 else f"{machine} answered {r.status_code}: {r.json().get('detail', r.text)}"
        except ValueError:
            return r.text

    async def wait_job(job, wait_seconds: int):
        await anyio.to_thread.run_sync(lambda: job.wait(max(0, min(wait_seconds, 600))))
        d = job.public(log_lines=40)
        if d["state"] in ("queued", "running"):
            d["note"] = f"still {d['state']}: call job_status('{job.id}') to follow it"
        return _j(d)

    @mcp.tool(description="What runs now: engines, profiles, ports, uptime, live token rates, VRAM/RAM/CPU, update info.")
    async def status(machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", "status")
        s = await run(mgr.status)
        for e in s["engines"]:
            e.get("running") and e["running"].get("stats", {}).get("window", {}).pop("1h", None)
        return _j(s)

    @mcp.tool(description="All profiles (models/setups) each engine can run, with their policy and resource needs.")
    async def list_profiles(machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", "profiles")
        return _j(await run(mgr.profiles_public))

    @mcp.tool(description="Dry run: what starting this profile would stop, whether it fits, and warnings.")
    async def plan(profile: str, force: bool = False, machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", "plan", {"profile": profile, "force": force})
        return _j(await run(mgr.plan, profile, force))

    @mcp.tool(description="Start a profile (switches the engine's model if it already runs another). force=True stops "
                          "whatever is in the way. update: auto (default, checks for engine updates at most every few "
                          "hours), skip, or force.")
    async def start(profile: str, force: bool = False, update: str = "auto", wait_seconds: int = 120,
                    machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "POST", "start", {"wait": wait_seconds}, {"profile": profile, "force": force, "update": update},
                              wait_seconds)
        job = await run(mgr.start, profile, force, update)
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="Switch to another model/profile. Same as start; named for clarity.")
    async def switch_model(profile: str, force: bool = False, update: str = "auto", wait_seconds: int = 120,
                           machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "POST", "start", {"wait": wait_seconds}, {"profile": profile, "force": force, "update": update},
                              wait_seconds)
        job = await run(mgr.start, profile, force, update)
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="Stop an engine by name, or 'all'.")
    async def stop(engine: str, wait_seconds: int = 60, machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "POST", "stop", {"wait": wait_seconds}, {"engine": engine}, wait_seconds)
        job = await run(mgr.stop, engine)
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="Restart an engine with the profile it runs now.")
    async def restart(engine: str, wait_seconds: int = 120, machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "POST", "restart", {"wait": wait_seconds}, {"engine": engine}, wait_seconds)
        job = await run(mgr.restart, engine)
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="Check every engine (or one) for updates without installing anything.")
    async def check_updates(engine: str | None = None, wait_seconds: int = 120, machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "POST", "check-updates", {"wait": wait_seconds}, {"engine": engine}, wait_seconds)
        job = await run(mgr.check_updates, engine)
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="Update an engine now (stops it if running, updates, starts the same profile again).")
    async def update_engine(engine: str, start_after: bool = True, wait_seconds: int = 60,
                            machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "POST", "update", {"wait": wait_seconds}, {"engine": engine, "start_after": start_after},
                              wait_seconds)
        job = await run(mgr.update, engine, start_after)
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="Go back to the engine's previous version (llama.cpp keeps the last build).")
    async def rollback_engine(engine: str, start_after: bool = True, wait_seconds: int = 120) -> str:
        job = await run(mgr.rollback, engine, start_after)
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="A job's state and log (jobs come from start/stop/update ...). No id: the recent jobs.")
    async def job_status(job_id: str | None = None, wait_seconds: int = 0, machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", f"jobs/{job_id}" if job_id else "jobs",
                              {"wait": wait_seconds, "lines": 40} if job_id else None, wait_seconds)
        if not job_id:
            return _j(mgr.list_jobs())
        job = mgr.jobs.get(job_id)
        if not job:
            return f"no job {job_id}"
        return await wait_job(job, wait_seconds)

    @mcp.tool(description="The last lines of an engine's log.")
    async def logs(engine: str, lines: int = 80, machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", f"logs/{engine}", {"lines": min(lines, 1000)})
        return "\n".join(await run(mgr.tail_log, engine, min(lines, 1000)))

    @mcp.tool(description="Usage totals for a period: day, week, month, year, all (also 24h, 7d, 30d, 365d) - tokens "
                          "in/out, requests, average speeds, uptime, per profile.")
    async def stats(period: str = "day", machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", "stats/summary", {"period": period})
        return _j(await run(mgr.store.summary, period))

    @mcp.tool(description="Token totals per bucket for trends: bucket hour/day/week/month/year, count buckets.")
    async def stats_series(bucket: str = "day", count: int = 14) -> str:
        return _j(await run(mgr.store.series, bucket, min(count, 400)))

    @mcp.tool(description="Recent events: starts, stops, crashes, updates, refusals.")
    async def events(limit: int = 30, machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", "events", {"limit": min(limit, 500)})
        return _j(await run(mgr.store.events, min(limit, 500)))

    @mcp.tool(description="Start a profile once with changed settings, without saving: overrides like {\"ctx\": 65536, "
                          "\"args_add\": [\"--temp\", \"0.6\"], \"mmproj\": null}. save_as saves it as a new profile instead.")
    async def run_custom(profile: str, overrides: dict, force: bool = False, update: str = "auto",
                         save_as: str | None = None, wait_seconds: int = 120) -> str:
        job = await run(mgr.run_custom, profile, overrides, force, update, save_as)
        return await wait_job(job, wait_seconds)

    # ---- models
    @mcp.tool(description="The model library (E:\\models and scanned folders): each folder's GGUFs, mmproj files, sizes, "
                          "and which profiles use them.")
    async def list_models() -> str:
        return _j(await run(mgr.library.list, mgr.profiles))

    @mcp.tool(description="Search for GGUF models. source: huggingface (default) or modelscope.")
    async def search_models(query: str, source: str = "huggingface", limit: int = 15) -> str:
        return _j(await run(mgr.downloader.search, source, query, min(limit, 50)))

    @mcp.tool(description="A repo's files grouped by quant (shards together) with sizes, to choose what to download. "
                          "source: huggingface, modelscope, ollama (repo like 'qwen3:8b'), url.")
    async def repo_files(repo: str, source: str = "huggingface") -> str:
        res = await run(mgr.downloader.list_files, source, repo)
        return _j(res["groups"] or res["files"][:200])

    @mcp.tool(description="Download a model into the library. include: file patterns, e.g. [\"*UD-Q4_K_XL*\", "
                          "\"mmproj-F16.gguf\"] (required for huggingface/modelscope). make_profile creates a llama.cpp "
                          "profile from it when done. Runs in the background: follow it with downloads_status.")
    async def download_model(repo: str, source: str = "huggingface", include: list[str] | None = None,
                             folder: str | None = None, make_profile: bool = True) -> str:
        d = await run(mgr.downloader.start, source, repo, include, folder, None, make_profile)
        return _j(d.public())

    @mcp.tool(description="Progress of downloads (bytes, speed, ETA, state).")
    async def downloads_status() -> str:
        return _j(mgr.downloader.list())

    @mcp.tool(description="Cancel a download (partial files stay; downloading again resumes).")
    async def cancel_download(download_id: str) -> str:
        return _j(mgr.downloader.cancel(download_id).public())

    # ---- profiles
    @mcp.tool(description="Make a llama.cpp profile for a GGUF in the library (uses the template from config; finds "
                          "the mmproj next to it).")
    async def create_profile_from_model(model_path: str, name: str | None = None, ctx: int | None = None) -> str:
        return _j({"id": await run(mgr.profile_from_model, model_path, name, "llamacpp", ctx, None, None)})

    @mcp.tool(description="Create or replace a UI-managed profile. settings for llamacpp: engine, model, mmproj, alias, "
                          "ctx, args (list), policy, description, idle_stop_minutes, restart_on_crash.")
    async def save_profile(profile_id: str, settings: dict) -> str:
        return _j({"id": await run(mgr.save_profile, profile_id, settings, True)})

    @mcp.tool(description="Copy a profile under a new name, with optional changed settings.")
    async def copy_profile(profile_id: str, new_id: str, changes: dict | None = None) -> str:
        return _j({"id": await run(mgr.copy_profile, profile_id, new_id, changes)})

    @mcp.tool(description="Delete a UI-managed profile (not config.yaml or engine-discovered ones).")
    async def delete_profile(profile_id: str) -> str:
        await run(mgr.delete_profile, profile_id)
        return "deleted"

    @mcp.tool(description="Ready-to-paste config that points a coding agent at the local models. client: opencode, qwen, "
                          "pi, codex, claude, aider, continue, generic. host: how the client reaches this PC (default "
                          "127.0.0.1; see the hosts list in the result's notes). profiles: comma-separated, default all.")
    async def client_config(client: str, host: str = "127.0.0.1", profiles: str | None = None,
                            machine: str | None = None) -> str:
        if machine:
            return await peer(machine, "GET", "connect/models", {"client": client, "host": host, "profiles": profiles})
        if client not in clients.MODEL_CLIENTS:
            return f"unknown client; one of {', '.join(clients.MODEL_CLIENTS)}"
        await run(mgr.refresh_profiles)
        entries = await run(clients.model_entries, mgr, [p for p in (profiles or "").split(",") if p] or None, host)
        snip = clients.MODEL_CLIENTS[client](entries, False)
        snip["hosts"] = await run(clients.hosts)
        return _j(snip)

    @mcp.tool(description="The memory budget: how much of the machine (VRAM + RAM together, GB) Hangar's engines may use. "
                          "Starts that would go over it are refused (force overrides) and new profiles get the largest "
                          "context that fits. gb: set it; clear=True: no budget; neither: just report.")
    async def memory_budget(gb: float | None = None, clear: bool = False, machine: str | None = None) -> str:
        if machine:
            if gb is None and not clear:
                return await peer(machine, "GET", "budget")
            return await peer(machine, "POST", "budget", body={"gb": None if clear else gb})
        if gb is None and not clear:
            return _j(await run(mgr.budget_public))
        try:
            return _j(await run(mgr.set_budget, None if clear else gb))
        except ValueError as e:
            return str(e)

    @mcp.tool(description="This machine and its peers (other Hangars): name, OS, online, what runs and how fast, memory.")
    async def machines() -> str:
        me = {**machine_info(mgr.cfg), "this_machine": True, "summary": summary(await run(mgr.status))}
        return _j([me] + await run(mgr.peers.public))

    @mcp.tool(description="Add a peer: another Hangar this one can see and control. url: how this machine reaches it "
                          "(e.g. http://macbook:9000); token: that Hangar's token (its data/token.txt).")
    async def add_machine(name: str, url: str, token: str) -> str:
        try:
            res = await run(mgr.peers.add, name, url, token)
        except ValueError as e:
            return str(e)
        mgr.store.event("peer", f"added peer {res['name']} ({res['url']})")
        return _j(res)

    @mcp.tool(description="Remove a peer added from the dashboard/CLI/MCP (ones in config.yaml stay).")
    async def remove_machine(name: str) -> str:
        try:
            await run(mgr.peers.remove, name.lower())
        except KeyError as e:
            return e.args[0]
        mgr.store.event("peer", f"removed peer {name}")
        return "removed"

    @mcp.tool(description="Whether Hangar starts when the user signs in to Windows (not available on macOS). enabled: true to turn it on, false "
                          "to turn it off, omit to just report. Only the manager starts; engines start on demand.")
    async def autostart_windows(enabled: bool | None = None) -> str:
        if enabled is None:
            return _j(await run(autostart.status))
        try:
            res = await run(autostart.enable if enabled else autostart.disable)
        except ValueError as e:
            return str(e)
        mgr.store.event("autostart", "starts with Windows" if enabled else "no longer starts with Windows")
        return _j(res)

    return mcp
