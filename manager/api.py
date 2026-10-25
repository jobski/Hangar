"""REST API + web UI + MCP, all behind one token (Authorization: Bearer <token>, or ?token= for the web page)."""
from __future__ import annotations

import codecs
import contextlib
import hmac
import itertools
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from mcp.server.transport_security import TransportSecuritySettings

from . import autostart, clients
from . import config as config_mod
from .core import Manager
from .mcp_server import build as build_mcp
from .peers import (Peers, engine_connection, engine_instance, fleet, machine_info, make_code,
                    normalize_url, record_engine_result, record_manager_failure, summary)
from .roles import ROLE_NAME, WAKE_WAIT_S, Roles, canonical_role
from .stats import RequestMeter, client_name, merge_series, merge_summaries, request_error
from .switching import keepalive, stream_error, strip_progress

WEB = Path(__file__).parent / "web"
TOO_LONG = re.compile(r"context|too long|too many tokens|exceed|n_ctx|maximum", re.I)
OPEN_PATHS = {"/", "/favicon.svg", "/api/ping", "/manifest.webmanifest"}
DRAIN_BUDGET_S = 25          # on shutdown, let in-flight requests finish within this before uvicorn exits cleanly


class UpstreamResponse(StreamingResponse):
    """Close the upstream even if sending response headers fails before the body iterator starts."""
    def __init__(self, *args, on_close, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.on_close()


def create_app(mgr: Manager | None = None, *, peers: Peers | None = None, roles: Roles | None = None,
               gateway: httpx.AsyncClient | None = None) -> FastAPI:
    """Build the app. peers/roles/gateway are injectable for tests; production calls create_app()."""
    mgr = mgr or Manager()
    token = config_mod.token(mgr.cfg)
    peers = mgr.peers = peers or Peers(mgr.cfg)
    if roles is None:
        roles = Roles(mgr, peers)
        roles.migrate_default_role()
    mgr.roles = roles
    mcp = build_mcp(mgr)
    # the token check below protects /mcp; the Host-header check would block access by Tailscale name or LAN IP
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="0.0.0.0",
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        mgr.start_background()
        peers.start()
        roles.start()
        async with mcp.session_manager.run():
            yield
        roles.shutdown()
        peers.shutdown()
        await proxy.aclose()
        await gateway.aclose()
        await mgr.mcp_client.aclose()
        mgr.shutdown()

    app = FastAPI(title="Hangar", version=mgr.hangar["version"], lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.mgr = mgr

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith("/static/") or path in ("/api/docs", "/api/openapi.json"):
            return await call_next(request)
        given = request.headers.get("authorization", "")
        # Bearer (OpenAI-style clients), x-api-key (Anthropic-style ones, on the gateway), or ?token= (the web page)
        given = (given[7:] if given.lower().startswith("bearer ")
                 else request.headers.get("x-api-key") or request.query_params.get("token", ""))
        if not given or not hmac.compare_digest(given.encode(), token.encode()):
            return JSONResponse({"error": "missing or wrong token"}, status_code=401)
        return await call_next(request)

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))

    async def job_reply(job, wait: float):
        deadline = time.monotonic() + max(0, min(wait, 600))
        while not job.done_event.is_set() and time.monotonic() < deadline:
            await anyio.sleep(0.1)
        return job.public(log_lines=60)

    # ---- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return FileResponse(WEB / "favicon.svg")

    @app.get("/logs", include_in_schema=False)
    def logs_window():
        return FileResponse(WEB / "logs.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/ping")
    def ping():
        return {"ok": True, "service": "hangar", "version": mgr.hangar["version"]}

    # ---- reading
    @app.get("/api/status")
    def status():
        return mgr.status()

    @app.get("/api/profiles")
    def profiles():
        return mgr.profiles_public()

    @app.get("/api/plan")
    def plan(profile: str, force: bool = False):
        return guard(mgr.plan, profile, force)

    @app.get("/api/history")
    def history(minutes: int = 60):
        pts = list(mgr.history)
        return pts[-max(1, minutes * 60 // mgr.cfg["resources"]["sample_interval_s"]):]

    @app.get("/api/machine-log", response_class=PlainTextResponse)
    def machine_log(lines: int = 200):
        """Every engine's log and Hangar's events, the last lines of each, each line tagged."""
        return mgr.machine_log(None, min(max(lines, 1), 5000), snapshot=True)[0]

    @app.get("/api/machine-log/stream")
    async def machine_log_stream(request: Request, lines: int = 200):
        """The machine as it runs: whichever engine is writing, and Hangar's events, until the client disconnects."""
        async def follow():
            text, state = await anyio.to_thread.run_sync(mgr.machine_log, None, min(max(lines, 1), 5000))
            yield text
            while not await request.is_disconnected():
                await anyio.sleep(0.5)
                text, state = await anyio.to_thread.run_sync(mgr.machine_log, state, lines)
                if text:
                    yield text

        return StreamingResponse(follow(), media_type="text/plain; charset=utf-8",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/logs/{engine}", response_class=PlainTextResponse)
    def logs(engine: str, lines: int = 200):
        guard(mgr.engine, engine)
        return "\n".join(mgr.tail_log(engine, min(lines, 5000)))

    @app.get("/api/logs/{engine}/stream")
    async def logs_stream(engine: str, request: Request, lines: int = 200):
        """The log as it is written: the last `lines` lines, then new output until the client disconnects."""
        guard(mgr.engine, engine)

        async def follow():
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            data, pos, ident = mgr.follow_log(engine, None, lines=min(lines, 5000))
            yield dec.decode(data)
            while not await request.is_disconnected():
                await anyio.sleep(0.5)
                data, pos, new = await anyio.to_thread.run_sync(mgr.follow_log, engine, pos, ident)
                if new != ident and ident is not None and new is not None:
                    yield "\n--- new log file ---\n"
                ident = new
                if data:
                    yield dec.decode(data)

        return StreamingResponse(follow(), media_type="text/plain; charset=utf-8",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs")
    def jobs(limit: int = 20):
        return mgr.list_jobs(limit)

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, wait: float = 0, lines: int = 200, since: int | None = None):
        with mgr.lock:
            j = mgr.jobs.get(job_id)
        if not j:
            raise HTTPException(404, f"no job {job_id}")
        await job_reply(j, wait)
        return j.public(log_lines=min(5000, max(0, lines)), since=since)

    @app.get("/api/events")
    def events(limit: int = 100):
        return mgr.store.events(min(limit, 1000))

    @app.get("/api/stats/summary")
    def stats_summary(period: str = "day"):
        return guard(mgr.store.summary, period)

    @app.get("/api/stats/all")
    def stats_all():
        return {p: mgr.store.summary(p) for p in ("day", "week", "month", "year", "all")}

    @app.get("/api/stats/series")
    def stats_series(bucket: str = "day", count: int = 30):
        return guard(mgr.store.series, bucket, min(count, 400))

    # ---- the same numbers for every online machine, added up (each peer is asked for its own)
    def _fleet_stats(path: str, params: dict, mine):
        online = [n for n, st in peers.state.items() if st.get("online")]
        all_peers = peers.all()

        def ask(name):
            p = all_peers.get(name)
            if not p:
                return name, None, "no longer a peer"
            try:
                r = httpx.get(f"{p['url']}/api/{path}", params=params, timeout=10,
                              headers={"Authorization": f"Bearer {p['token']}"})
                r.raise_for_status()
                return name, r.json(), None
            except (httpx.HTTPError, ValueError) as e:
                return name, None, type(e).__name__
        with ThreadPoolExecutor(max_workers=max(1, len(online))) as pool:
            answers = list(pool.map(ask, online))
        me = machine_info(mgr.cfg)["name"]
        machines = [{"name": me, "ok": True}] + [{"name": n, "ok": d is not None, "error": err} for n, d, err in answers]
        return [(me, mine)] + [(n, d) for n, d, _ in answers if d is not None], machines

    @app.get("/api/stats/fleet/summary")
    def stats_fleet_summary(period: str = "day"):
        parts, machines = _fleet_stats("stats/summary", {"period": period}, guard(mgr.store.summary, period))
        return {**merge_summaries(period, parts), "machines": machines}

    @app.get("/api/stats/fleet/series")
    def stats_fleet_series(bucket: str = "day", count: int = 30):
        count = min(count, 400)
        parts, machines = _fleet_stats("stats/series", {"bucket": bucket, "count": count},
                                       guard(mgr.store.series, bucket, count))
        return merge_series([d for _, d in parts])

    # ---- actions (each returns a job; ?wait=N waits up to N seconds for it)
    @app.post("/api/start")
    async def start(profile: str = Body(..., embed=True), force: bool = Body(False, embed=True),
              update: str = Body("auto", embed=True), wait: float = 0):
        return await job_reply(await anyio.to_thread.run_sync(lambda: guard(mgr.start, profile, force, update)), wait)

    @app.post("/api/switch")
    async def switch(profile: str = Body(..., embed=True), force: bool = Body(False, embed=True),
               update: str = Body("auto", embed=True), wait: float = 0):
        return await job_reply(await anyio.to_thread.run_sync(lambda: guard(mgr.start, profile, force, update)), wait)

    @app.post("/api/stop")
    async def stop(engine: str = Body(..., embed=True), wait: float = 0,
                   idle_profile: str | None = Body(None, embed=True)):
        action = (lambda: guard(mgr.stop_idle, engine, idle_profile)) if idle_profile else (lambda: guard(mgr.stop, engine))
        return await job_reply(await anyio.to_thread.run_sync(action), wait)

    @app.post("/api/restart")
    async def restart(engine: str = Body(..., embed=True), wait: float = 0):
        return await job_reply(await anyio.to_thread.run_sync(lambda: guard(mgr.restart, engine)), wait)

    @app.post("/api/update")
    async def update(engine: str = Body(..., embed=True), start_after: bool = Body(True, embed=True), wait: float = 0):
        return await job_reply(await anyio.to_thread.run_sync(lambda: guard(mgr.update, engine, start_after)), wait)

    @app.post("/api/check-updates")
    async def check_updates(engine: str | None = Body(None, embed=True), wait: float = 0):
        return await job_reply(await anyio.to_thread.run_sync(lambda: guard(mgr.check_updates, engine)), wait)

    @app.post("/api/rollback")
    async def rollback(engine: str = Body(..., embed=True), start_after: bool = Body(True, embed=True), wait: float = 0):
        return await job_reply(await anyio.to_thread.run_sync(lambda: guard(mgr.rollback, engine, start_after)), wait)

    @app.post("/api/run-custom")
    async def run_custom(profile: str = Body(..., embed=True), overrides: dict = Body({}, embed=True),
                   force: bool = Body(False, embed=True), update: str = Body("auto", embed=True),
                   save_as: str | None = Body(None, embed=True), wait: float = 0):
        return await job_reply(await anyio.to_thread.run_sync(lambda: guard(mgr.run_custom, profile, overrides, force, update, save_as)), wait)

    # ---- profiles (UI-managed ones live in profiles.yaml)
    @app.post("/api/profiles")
    def save_profile(id: str = Body(..., embed=True), settings: dict = Body(..., embed=True),
                     overwrite: bool = Body(True, embed=True)):
        return {"id": guard(mgr.save_profile, id, settings, overwrite)}

    @app.delete("/api/profiles/{pid}")
    def delete_profile(pid: str):
        guard(mgr.delete_profile, pid)
        return {"ok": True}

    @app.post("/api/profiles/{pid}/copy")
    def copy_profile(pid: str, new_id: str = Body(..., embed=True), changes: dict = Body({}, embed=True)):
        return {"id": guard(mgr.copy_profile, pid, new_id, changes)}

    @app.post("/api/profiles/from-model")
    def profile_from_model(model: str = Body(..., embed=True), name: str | None = Body(None, embed=True),
                           engine: str = Body("llamacpp", embed=True), ctx: int | None = Body(None, embed=True),
                           mmproj: str | None = Body(None, embed=True)):
        return {"id": guard(mgr.profile_from_model, model, name, engine, ctx, mmproj)}

    # ---- model library + downloads
    @app.get("/api/models")
    def models():
        return mgr.library.list(mgr.profiles)

    @app.delete("/api/models")
    def delete_model(folder: str):
        guard(mgr.delete_model_folder, folder)
        return {"ok": True}

    @app.get("/api/search")
    def search(q: str, source: str = "huggingface", limit: int = 20):
        return guard(mgr.downloader.search, source, q, min(limit, 50))

    @app.get("/api/repo-files")
    def repo_files(repo: str, source: str = "huggingface", revision: str | None = None):
        return guard(mgr.downloader.list_files, source, repo, revision)

    @app.get("/api/downloads")
    def downloads():
        return {"items": mgr.downloader.list(), "leftovers": mgr.downloader.leftovers(),
                "aria2": mgr.downloader.aria2_path(), "aria2_hint": mgr.downloader.aria2_hint()}

    @app.post("/api/downloads/leftovers/resume")
    def leftover_resume(folder: str = Body(..., embed=True)):
        return guard(mgr.downloader.resume, folder).public()

    @app.post("/api/downloads/leftovers/discard")
    def leftover_discard(folder: str = Body(..., embed=True)):
        res = guard(mgr.downloader.discard, folder)
        mgr.store.event("download", f"discarded the unfinished download in {Path(res['folder']).name}")
        return res

    @app.post("/api/downloads")
    def download(source: str = Body("huggingface", embed=True), repo: str = Body(..., embed=True),
                 include: list[str] | None = Body(None, embed=True), folder: str | None = Body(None, embed=True),
                 revision: str | None = Body(None, embed=True), make_profile: bool = Body(True, embed=True)):
        return guard(mgr.downloader.start, source, repo, include, folder, revision, make_profile).public()

    @app.post("/api/downloads/{did}/cancel")
    def cancel_download(did: str):
        return guard(mgr.downloader.cancel, did).public()

    # ---- copy-paste configs for coding agents and MCP clients
    @app.get("/api/connect/hosts")
    def connect_hosts():
        return clients.hosts()

    def _snippet(client: str, host: str, profiles: str | None, env: bool):
        if client not in clients.MODEL_CLIENTS:
            raise HTTPException(404, f"unknown client {client}; one of {', '.join(clients.MODEL_CLIENTS)}")
        mgr.refresh_profiles()
        entries = clients.model_entries(mgr, [p for p in (profiles or "").split(",") if p] or None, host)
        port = int(mgr.cfg["manager"]["port"])
        # the roles, through this Hangar's gateway: the model name is the role (worker, thinker, helper, ...)
        for r in roles.public():
            if r["targets"] or r["name"] == "worker":
                served = role_served(r)
                entries.append({"profile": f"role-{r['name']}", "engine": "hangar", "provider": "hangar",
                                "provider_name": "Hangar roles", "base": f"http://{host}:{port}", "api_key": token,
                                "model": r["name"], "name": f"{r['name']} (Hangar)", "context": served["context"],
                                "output": served["output"], "vision": served["vision"], "reasoning": served["reasoning"],
                                "env_key": "HANGAR_TOKEN"})
        snip = clients.MODEL_CLIENTS[client](entries, env)
        mapping = mgr._settings().get("switching_roles") or {}
        names = {r["name"] for r in roles.public()}
        return clients.with_roles(snip, client, {k: v for k, v in mapping.items() if v in names})

    @app.get("/api/connect/models")
    def connect_models(client: str, host: str = "127.0.0.1", profiles: str | None = None, env: bool = False):
        return _snippet(client, host, profiles, env)

    @app.get("/api/connect/mcp")
    def connect_mcp(host: str = "127.0.0.1"):
        return clients.mcp_configs(host, int(mgr.cfg["manager"]["port"]), token)

    @app.post("/api/connect/apply")
    def connect_apply(kind: str = Body(..., embed=True), client: str | None = Body(None, embed=True),
                      host: str = Body("127.0.0.1", embed=True), profiles: str | None = Body(None, embed=True)):
        if kind not in clients.APPLY:
            raise HTTPException(400, f"cannot apply {kind}; one of {', '.join(sorted(clients.APPLY))}")
        if kind.endswith("-mcp"):
            snip = next(s for s in clients.mcp_configs(host, int(mgr.cfg["manager"]["port"]), token)
                        if s.get("apply") == kind)
        else:
            snip = _snippet(client or kind, host, profiles, False)
        res = guard(clients.apply, kind, snip)
        mgr.store.event("connect", f"wrote {kind} settings to {res['file']}")
        return res

    # ---- roles (worker, thinker, helper, ...) and the gateway that serves them
    @app.get("/api/roles")
    def roles_list():
        return roles.public()

    @app.get("/api/roles/main/follow-running", include_in_schema=False)
    @app.get("/api/roles/worker/follow-running")
    def worker_follow_running():
        return {"enabled": roles.follows_running()}

    @app.post("/api/roles/main/follow-running", include_in_schema=False)
    @app.post("/api/roles/worker/follow-running")
    def worker_follow_running_set(enabled: bool = Body(..., embed=True)):
        return roles.set_follows_running(enabled)

    @app.post("/api/roles")
    def roles_save(name: str = Body(..., embed=True), targets: list[dict] = Body(..., embed=True),
                   spill: bool | None = Body(None, embed=True), switch: bool | None = Body(None, embed=True)):
        return guard(roles.save, name, targets, spill, switch)

    @app.get("/api/switching")
    def switching_get():
        return {**mgr.switching(), "roles": mgr._settings().get("switching_roles") or {}}

    @app.post("/api/switching/setup")
    async def switching_setup(thinker: str = Body(..., embed=True), coder: str = Body(..., embed=True),
                              machine: str = Body("", embed=True), thinker_role: str = Body("thinker", embed=True),
                              coder_role: str = Body("worker", embed=True), small_role: str = Body("helper", embed=True)):
        """Model switching in one action: a switching role for a thinker and one for a coder on one machine, worker on
        the coder (no longer following whatever runs), and the coder as that machine's home model."""
        def run():
            machine_ = (machine or "").strip().lower()
            # checked before the first write, so a rejected setup leaves the routing as it was
            plan_r, build_r, small_r = ((rn or "").strip().lower() for rn in (thinker_role, coder_role, small_role))
            for rn in (plan_r, build_r, small_r):
                if not ROLE_NAME.match(rn):
                    raise ValueError(f"not a role name: {rn!r} (a lowercase word, e.g. plan or helper)")
            if plan_r == build_r:
                raise ValueError("the thinker and the coder need different role names")
            if "main" in (plan_r, build_r, small_r) or "worker" in (plan_r, small_r):
                raise ValueError("worker is the default coder role; use different names for thinker and helper")
            # the small role takes clients' background work: on a switching model it would swap the thinker out
            if small_r in (plan_r, build_r) or (mgr._settings().get("roles") or {}).get(small_r, {}).get("switch"):
                raise ValueError(f"the small role {small_r!r} must not switch models: background work would unload "
                                 "the thinker")
            if thinker == coder:
                raise ValueError("the thinker and the coder are two different models")
            if machine_:
                try:
                    ids = {p["id"] for p in roles._peer(machine_, "GET", "profiles").json()}
                except (httpx.HTTPError, KeyError) as e:
                    raise ValueError(f"could not ask {machine_} for its profiles: {e}") from None
                missing = [pid for pid in (thinker, coder) if pid not in ids]
                if missing:
                    raise ValueError(f"no profile {', '.join(missing)} on {machine_}")
            else:
                missing = [pid for pid in (thinker, coder) if pid not in mgr.profiles]
                if missing:
                    raise ValueError(f"no profile {', '.join(missing)} on this machine")
            roles.save(plan_r, [{"machine": machine_, "profile": thinker}], switch=True)
            roles.save(build_r, [{"machine": machine_, "profile": coder}], switch=True)
            if build_r != "worker":
                roles.save("worker", [{"machine": machine_, "profile": coder}], switch=True)
            roles.set_follows_running(False)
            if machine_:
                try:
                    roles._peer(machine_, "POST", "switching", json={"home": coder})
                except httpx.HTTPError as e:
                    raise ValueError(f"the roles are saved, but {machine_} did not take its home model: {e}") from None
            else:
                mgr.set_switching({"home": coder})
            mapping = {"plan": plan_r, "build": build_r, "small": small_r}
            me = machine_info(mgr.cfg)["name"]
            with mgr.settings_lock:
                s = mgr._settings()
                s["switching_roles"] = mapping
                mgr.write_settings(s)
            mgr.store.event("switch", f"model switching set up: {plan_r} -> {machine_ or me}/{thinker}, "
                                      f"{build_r} -> {machine_ or me}/{coder} (home)")
            return {**mapping, "home": f"{machine_ or me}/{coder}", "roles": roles.public()}
        return await anyio.to_thread.run_sync(lambda: guard(run))

    @app.post("/api/switching")
    def switching_set(changes: dict = Body(...)):
        return guard(mgr.set_switching, changes)

    @app.delete("/api/roles/{name}")
    def roles_delete(name: str):
        guard(roles.delete, name)
        return roles.public()

    routing_lock = anyio.Lock()
    wake_limiter = anyio.CapacityLimiter(8)
    gateway = gateway or httpx.AsyncClient(timeout=httpx.Timeout(10, read=None))   # generations can stream for many minutes
    # requests being generated right now, keyed by a counter; only the event loop touches it, so no lock
    inflight: dict[int, dict] = {}
    # Keep one model ID per target, invalidated when its endpoint or engine instance changes.
    direct_cache: dict[tuple, tuple[tuple, str]] = {}
    inflight_seq = itertools.count()

    app.state.inflight = inflight

    @app.get("/v1/models")
    def gateway_models():
        data = []
        for r in roles.public():
            served = role_served(r)
            data.append({"id": r["name"], "object": "model", "owned_by": "hangar-role", "name": served["name"],
                         "context_length": served["context"], "max_output_tokens": served["output"],
                         "served_by": served["served_by"], "fallback": r["fallback"]})
        data += [{"id": i.profile, "object": "model", "owned_by": "hangar-profile"} for i in mgr.instances.values()]
        return {"object": "list", "data": data}

    async def direct_engine(t: dict):
        """A peer's engine, reached straight (not through its gateway): (request_url, model_id, auth). Used when a
        peer has no gateway, or its gateway is down/restarting while its engine is still up. Authenticated with the
        engine key the peer published in its status."""
        st = (peers.state.get(t["machine"]) or {}).get("status") or {}
        engine = next((e for e in st.get("engines") or []
                       if (e.get("running") or {}).get("profile") == t["profile"]), None)
        if not engine or not engine.get("port"):
            return None
        base, auth = engine_connection(peers.get(t["machine"])["url"], engine)
        instance = engine_instance(engine)
        ck = (t["machine"], t["profile"])
        engine_id = (base, *instance)
        cached = direct_cache.get(ck)
        mid = cached[1] if cached and cached[0] == engine_id else None
        if mid is None:
            try:
                response = await gateway.get(base + "/v1/models", headers=auth, timeout=5)
                response.raise_for_status()
                ids = [m["id"] for m in response.json().get("data", [])]
            except (httpx.HTTPError, ValueError, AttributeError, TypeError, KeyError) as exc:
                record_engine_result(peers, t["machine"], t["profile"], instance, False, type(exc).__name__)
                return None
            if not ids:
                record_engine_result(peers, t["machine"], t["profile"], instance, False, "no model available")
                return None
            mid = ids[0]
            direct_cache[ck] = (engine_id, mid)
        return (base, mid, auth, instance)

    def target_engine(target):
        if target["machine"]:
            status = (peers.state.get(target["machine"]) or {}).get("status") or {}
            return next((e.get("plugin") or e["name"] for e in status.get("engines", [])
                         if (e.get("running") or {}).get("profile") == target["profile"]), None)
        profile = mgr.profiles.get(target["profile"])
        engine = mgr.engines.get(profile.engine) if profile else None
        return engine.plugin if engine else None

    def target_vision(target) -> bool:
        """Whether the model behind a target takes image input. A local profile answers from its own config, so a
        stopped one still answers; a peer answers from the vision tag its running engine publishes (core.status)."""
        if not target:
            return False
        if not target["machine"]:
            profile = mgr.profiles.get(target["profile"])
            return mgr._vision(profile.engine, target["profile"]) if profile else False
        status = (peers.state.get(target["machine"]) or {}).get("status") or {}
        return any((e.get("running") or {}).get("profile") == target["profile"] and (e.get("running") or {}).get("vision")
                   for e in status.get("engines", []))

    def role_served(r: dict) -> dict:
        """What a role's requests reach right now (from roles.public()), named so a client shows the real model."""
        s, ctx = r["serving"], roles.context(r["name"])
        served_by = f"{s['machine'] or machine_info(mgr.cfg)['name']}/{s['profile']}" if s else None
        # a switching role is named after its own model - not worker while it follows the running one (it never switches)
        own = (r.get("saved_targets") or [None])[0] if r.get("switch") and not r.get("follow_running") else None
        if own:
            name = f"{r['name']} → {own['profile']} on {own.get('machine') or machine_info(mgr.cfg)['name']} (switches)"
        elif not s:
            name = f"{r['name']} (nothing up)"
        else:
            name = f"{r['name']} → {s['profile']} on {served_by.split('/', 1)[0]}" + (" (fallback)" if r["fallback"] else "")
        return {"name": name, "served_by": served_by, "context": ctx, "output": min(16384, max(1024, ctx // 4)),
                # capability follows the model the role is set to, not a text-only fallback answering in its place
                "reasoning": bool(s) and target_engine(s) in ("strata", "maya"),
                "vision": target_vision((r["targets"] or [None])[0] or s)}

    def stopping_reply(model: str) -> JSONResponse:
        """A target finishing its reply before a switch stops it: a hub must retry, not route around the drain."""
        return JSONResponse({"error": {"type": "unavailable", "message": f"the model for {model!r} is being "
                                       "stopped to make room for another one: try again shortly"}},
                            status_code=503, headers={"Retry-After": "10", "X-Hangar-State": "stopping"})

    async def warm_model(model: str):
        """Bring a switching role's (or a profile's) model up before the first request, by the same rules."""
        model = canonical_role(model)
        st = await anyio.to_thread.run_sync(roles.switch_target, model)
        if st and st["machine"]:
            p = peers.get(st["machine"])
            r = await gateway.post(f"{p['url']}/v1/hangar/warm", json={"model": f"profile:{st['profile']}"},
                                   headers={"Authorization": f"Bearer {p['token']}", "X-Hangar-Switch": model}, timeout=30)
            return JSONResponse(r.json(), status_code=r.status_code)
        if not st and (model in roles.all() or model == "worker"):
            # a role that needs no switch: say truthfully whether it can answer, waking its machines as a request would
            woken = await anyio.to_thread.run_sync(roles.wake_for, model)
            if await anyio.to_thread.run_sync(roles.candidates, model):
                return {"model": model, "state": "ready"}
            return {"model": model, "state": "waking" if woken else "unavailable", "woken": woken}
        pid = st["profile"] if st else model.split(":", 1)[1] if model.startswith("profile:") else model
        if pid not in mgr.profiles:
            if model in roles.all():
                return {"model": model, "state": "ready"}        # a role whose model is up, or one that never switches
            raise HTTPException(404, f"no switching role or profile called {model!r} here")
        res = await anyio.to_thread.run_sync(mgr.request_switch, pid)
        return {"model": model, "profile": pid, "state": res["state"], "reason": res.get("reason"),
                "wait_s": round(res.get("wait_s", 0)), "job": res["job"].id if res.get("job") else None}

    @app.post("/v1/hangar/warm")
    async def gateway_warm(request: Request, model: str = Body(..., embed=True)):
        return await warm_model(model)

    # the CLI sends only /api/... calls to the machine picked by --on, so warming a peer needs this one too
    @app.post("/api/warm")
    async def api_warm(model: str = Body(..., embed=True)):
        return await warm_model(model)

    @app.post("/v1/{path:path}")
    async def gateway_call(path: str, request: Request):
        """OpenAI / Anthropic API calls for a role (or a profile): sent to the first target that is up, the next one
        when it fails before answering. A peer's target goes through that peer's gateway, with its token."""
        t0 = time.time()
        raw = await request.body()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "the gateway takes JSON requests")
        if not isinstance(data, dict):
            raise HTTPException(400, "the gateway takes a JSON object")
        try:
            max_tokens = int(data.get("max_tokens") or data.get("max_completion_tokens") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_tokens must be an integer")
        model = canonical_role(str(data.get("model") or "worker"))
        me = machine_info(mgr.cfg)["name"]
        client, via = client_name(request.headers), request.headers.get("x-hangar-via")
        # the device: as a hub that passed it on saw it, else who connected here
        caller = (request.headers.get("x-hangar-caller", "")[:40] if via else "") or \
            await anyio.to_thread.run_sync(peers.caller_name, request.client.host if request.client else "")

        async def log(**kw):
            try:
                await anyio.to_thread.run_sync(lambda: mgr.store.log_request(t0, model=model, client=client, caller=caller, via=via, path=path, stream=int(bool(data.get("stream"))),
                                      duration_s=time.time() - t0, **kw))
            except Exception:  # noqa: BLE001 - the log never fails a request
                pass
        data = strip_progress(data)
        # a switching role whose model is not up: its machine switches and holds the request, a hub relays it there
        forced, switch_pid, local_role = [], None, False
        model = canonical_role(model)
        st = await anyio.to_thread.run_sync(roles.switch_target, model)
        if st and st["machine"]:
            forced = [st]
        elif st:
            switch_pid, local_role = st["profile"], True       # this machine owns the role's model: fallbacks are ours
        elif request.headers.get("x-hangar-switch") and model.startswith("profile:"):
            switch_pid = model.split(":", 1)[1]
        if switch_pid and switch_pid not in mgr.profiles:
            switch_pid = None

        def fallback_header(t):
            """Answered by another target than the one a switch asked for: say so."""
            f = forced[0] if forced else None
            return {"X-Hangar-Fallback": f"{f['machine']}/{f['profile']}"} if f and t != f else {}

        async def route():
            """Send the request to the first target that is up (a forced one first), the next when one fails."""
            woken = await anyio.to_thread.run_sync(roles.wake_for, model)
            # rough size of the request in tokens (~4 bytes each) plus what it may generate: a spill target must fit it
            need = len(raw) // 4 + max(0, max_tokens)
            cands = list(forced) or await anyio.to_thread.run_sync(roles.candidates, model, need, (client, caller))
            # a target on its way up is worth waiting for even while another stops: a 503 would only make clients loop
            if not cands and (woken or await anyio.to_thread.run_sync(roles.starting, model)):
                cands = await anyio.to_thread.run_sync(roles.wait_for, model, woken, limiter=wake_limiter)
            if not cands and await anyio.to_thread.run_sync(roles.stopping, model):
                # its model is finishing a reply before a switch stops it: say so at once, never hold the request
                await log(status=503, error="stopping")
                return stopping_reply(model)
            if not cands:
                names = ", ".join(r["name"] for r in roles.public())
                why = (f"woke {', '.join(woken)}, but nothing could answer within {WAKE_WAIT_S} s (a model may still be "
                       "loading there: try again shortly)") if woken else (f"a model for {model!r} is still starting: try again "
                       "shortly") if roles.starting(model) else f"nothing is up to serve {model!r}"
                await log(status=503, error="woke " + ", ".join(woken) if woken else "nothing up")
                return JSONResponse({"error": {"type": "unavailable", "message": f"{why} (roles: {names}; or a running "
                                               "profile's id)"}}, status_code=503)
            passthrough = {k: v for k, v in request.headers.items() if k.lower() in ("accept", "anthropic-version", "anthropic-beta")}
            errors = []
            attempted = set()
            drained = False               # a target answered that it is draining for a switch
            while True:
                async with routing_lock:
                    normal = await anyio.to_thread.run_sync(roles.candidates, model, need, (client, caller))
                    # the forced target first, but its fallbacks stay behind it: it may not fit the request
                    cands = [t for t in forced if (t["machine"], t["profile"]) not in attempted] + \
                        [t for t in normal if t not in forced]
                    cands = [t for t in cands if (t["machine"], t["profile"]) not in attempted]
                    if not cands:
                        break
                    t = cands[0]
                    attempted.add((t["machine"], t["profile"]))
                    if not roles.opened(t, switching=t in forced):
                        continue
                r, relaying = None, False
                served_engine = target_engine(t)
                direct_tried = False
                direct_instance = None

                async def send_direct():
                    """Relay to this target's engine straight (its gateway is down or has none). Sets r, or None."""
                    nonlocal r, direct_tried, direct_instance
                    r, direct_tried = None, True
                    direct = await direct_engine(t)
                    if not direct:
                        return None
                    base, mid, auth, direct_instance = direct
                    req = gateway.build_request("POST", f"{base}/v1/{path}", content=json.dumps({**data, "model": mid}).encode(),
                                                headers={**passthrough, **auth, "Content-Type": "application/json"})
                    try:
                        r = await gateway.send(req, stream=True)
                    except httpx.HTTPError as exc:
                        direct_cache.pop((t["machine"], t["profile"]), None)
                        record_engine_result(peers, t["machine"], t["profile"], direct_instance, False, type(exc).__name__)
                        raise
                    healthy = r.status_code not in (401, 403, 404, 502, 503, 504)
                    if not healthy:
                        direct_cache.pop((t["machine"], t["profile"]), None)
                    record_engine_result(peers, t["machine"], t["profile"], direct_instance, healthy,
                                         None if healthy else f"HTTP {r.status_code}")
                    return r

                try:
                    if t["machine"]:
                        if (peers.state.get(t["machine"]) or {}).get("online"):
                            try:
                                p = peers.get(t["machine"])
                                url, mid = f"{p['url']}/v1/{path}", f"profile:{t['profile']}"
                                auth = {"Authorization": f"Bearer {p['token']}", "X-Hangar-Via": via or me, "X-Hangar-Client": client,
                                        "X-Hangar-Caller": caller,
                                        **({"X-Hangar-Switch": model} if t in forced else {})}
                                req = gateway.build_request("POST", url, content=json.dumps({**data, "model": mid}).encode(),
                                                            headers={**passthrough, **auth, "Content-Type": "application/json"})
                                r = await gateway.send(req, stream=True)
                            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError):
                                # online is still cached but the gateway is already refusing (its manager is on its way
                                # out before the poll flips it offline): go straight to the engine, not to a backup
                                r = None
                                record_manager_failure(peers, t["machine"])
                            # no answer (refused) or an old peer with no gateway (404): fall through to its engine
                            if r is None or r.status_code == 404:
                                if r is not None:
                                    with anyio.CancelScope(shield=True):
                                        await r.aclose()
                                await send_direct()
                        else:
                            # Manager offline: use the independently monitored engine.
                            await send_direct()
                        if r is None:
                            errors.append(f"{t['machine']}/{t['profile']}: gateway down, engine not reachable")
                            continue
                    else:
                        ents = await anyio.to_thread.run_sync(lambda: clients.model_entries(
                            mgr, [t["profile"]], "127.0.0.1", include_custom=True))
                        if not ents:
                            errors.append(f"here/{t['profile']}: profile no longer available")
                            continue
                        e = ents[0]
                        url, mid = f"{e['base']}/v1/{path}", e["model"]
                        auth = {"Authorization": f"Bearer {e['api_key']}"} if e["api_key"] else {}
                        req = gateway.build_request("POST", url, content=json.dumps({**data, "model": mid}).encode(),
                                                    headers={**passthrough, **auth, "Content-Type": "application/json"})
                        r = await gateway.send(req, stream=True)
                    if t["machine"]:
                        served_engine = r.headers.get("x-hangar-engine") or served_engine
                    if r.status_code in (502, 503, 504) or (t["machine"] and r.status_code in (401, 403, 404)):
                        # a gateway that answered but is on its way out: last-ditch try its engine before the next target
                        # (not when the peer is draining that engine for a switch: going direct would bypass the drain)
                        if t["machine"] and not direct_tried and r.headers.get("x-hangar-state") != "stopping":
                            with anyio.CancelScope(shield=True):
                                await r.aclose()
                            if not await send_direct():
                                errors.append(f"{t['machine']}/{t['profile']}: gateway down, engine not reachable")
                                continue
                            if r.status_code in (401, 403, 404, 502, 503, 504):
                                errors.append(f"{t['machine']}/{t['profile']}: HTTP {r.status_code}")
                                continue
                        else:
                            if r.headers.get("x-hangar-state") == "stopping":
                                drained = True    # its cached state may still say ready, so trust the answer we got
                            body = bytearray()
                            async for chunk in r.aiter_bytes():
                                body.extend(chunk[:8192 - len(body)])
                                if len(body) >= 8192:
                                    break
                            errors.append(f"{t['machine'] or 'here'}/{t['profile']}: {request_error(bytes(body), r.status_code)}")
                            continue
                    if r.status_code in (400, 413) and len(cands) > 1:
                        body = await r.aread()
                        if TOO_LONG.search(body[:4096].decode("utf-8", "replace")):
                            errors.append(f"{t['machine'] or 'here'}/{t['profile']}: {request_error(body, r.status_code)}")
                            continue
                        await log(machine=t["machine"] or me, profile=t["profile"], status=r.status_code, engine=served_engine, error=request_error(body, r.status_code))
                        return Response(body, status_code=r.status_code, media_type=r.headers.get("content-type"),
                                        headers={"X-Hangar-Served-By": f"{t['machine'] or me}/{t['profile']}",
                                                 "X-Hangar-Engine": served_engine or "", **fallback_header(t),
                                                 **({"X-Hangar-Route": "direct"} if direct_tried else {})})
                    roles.note_used(t, model, (client, caller))
                    meter = RequestMeter(t0, "text/event-stream" in r.headers.get("content-type", ""))
                    seq = next(inflight_seq)
                    inflight[seq] = {"started": time.time(), "model": model, "client": client, "caller": caller,
                                     "machine": t["machine"] or me, "profile": t["profile"], "engine": served_engine,
                                     "meter": meter}
                    done, finished = False, False

                    async def finish(r=r, t=t, meter=meter, served_engine=served_engine, seq=seq):
                        nonlocal finished
                        if finished:
                            return
                        finished = True
                        inflight.pop(seq, None)
                        roles.closed(t)
                        with anyio.CancelScope(shield=True):
                            await r.aclose()
                        measurements = meter.result()
                        error = ("closed before the end" if not done else
                                 f"HTTP {r.status_code}: {meter.error}" if r.status_code >= 400 and meter.error else
                                 request_error(bytes(meter.body), r.status_code) if r.status_code >= 400 else meter.error)
                        await log(machine=t["machine"] or me, profile=t["profile"], engine=served_engine,
                                  status=r.status_code, error=error, **measurements)

                    async def relay(r=r, t=t, meter=meter):
                        nonlocal done
                        try:
                            async for chunk in r.aiter_bytes():
                                meter.feed(chunk, time.time())
                                yield chunk
                            done = True
                            if direct_tried and r.status_code < 400:
                                record_engine_result(peers, t["machine"], t["profile"], direct_instance, True)
                        except httpx.HTTPError as exc:
                            if direct_tried:
                                record_engine_result(peers, t["machine"], t["profile"], direct_instance, False, type(exc).__name__)
                            raise
                        finally:
                            await finish()
                    response = UpstreamResponse(relay(), on_close=finish, status_code=r.status_code, media_type=r.headers.get("content-type"),
                                                 headers={"X-Hangar-Served-By": f"{t['machine'] or me}/{t['profile']}",
                                                          "X-Hangar-Engine": served_engine or "", **fallback_header(t),
                                                          **({"X-Hangar-Route": "direct"} if direct_tried else {}),
                                                          "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
                    relaying = True
                    return response
                except (httpx.HTTPError, KeyError) as ex:
                    errors.append(f"{t['machine'] or 'here'}/{t['profile']}: {type(ex).__name__}")
                finally:
                    if not relaying:
                        roles.closed(t)
                        if r is not None:
                            with anyio.CancelScope(shield=True):
                                await r.aclose()
            if drained or await anyio.to_thread.run_sync(roles.stopping, model):
                # it began draining while we routed, or a target said so: same 503, so a hub retries rather than going direct
                await log(status=503, error="stopping")
                return stopping_reply(model)
            await log(status=502, error="; ".join(errors)[:500])
            return JSONResponse({"error": {"type": "unavailable", "message": f"every target for {model!r} failed: "
                                           + "; ".join(errors)}}, status_code=502)

        async def switch_progress(pid: str):
            """While its model comes up: ("tick", text) about every 10 s, then ("ready", None) or ("error", why)."""
            p, sw = mgr.profiles[pid], mgr.switching()
            label = p.description or pid
            began, last = time.time(), 0.0
            deadline = began + float(sw["max_wait_s"])
            eta = (await anyio.to_thread.run_sync(mgr.needs, p)).get("load_seconds") or 75
            job = None               # our own start: asking again would queue a fresh one and supersede this one
            while time.time() < deadline:
                if job is not None and job.done_event.is_set():
                    if job.state == "done":
                        yield ("ready", None)                       # routing says so if it is somehow not up
                        return
                    if (job.error or "").startswith("a newer start"):
                        yield "error", f"a newer switch on {me} replaced this one ({job.error})"
                        return
                    res = await anyio.to_thread.run_sync(mgr.request_switch, pid)   # once: it notes the back-off
                    yield "error", res["reason"] if res["state"] == "refused" else (job.error or job.state)
                    return
                if job is None:
                    res = await anyio.to_thread.run_sync(mgr.request_switch, pid)
                    if res["state"] in ("ready", "refused"):
                        yield ("ready", None) if res["state"] == "ready" else ("error", res["reason"])
                        return
                    job = res.get("job")                            # still "waiting": its turn ends, so keep asking
                if time.time() - last >= 10:
                    last = time.time()
                    yield "tick", (f"loading {label} on {me} - {last - began:.0f} s of ~{eta:.0f} s" if job else
                                   f"{res['reason']} ({res['wait_s']:.0f} s left), then loading {label}")
                await anyio.sleep(1 if job else max(0.5, min(2.0, res.get("wait_s", 2))))
            yield "error", f"{label} did not come up within {float(sw['max_wait_s']):.0f} s"

        def label_fallback(resp, pid: str):
            """A switch here was wanted but another target answered: say which one it wanted."""
            if getattr(resp, "headers", None) is not None and resp.headers.get("x-hangar-served-by") != f"{me}/{pid}":
                resp.headers["X-Hangar-Fallback"] = f"{me}/{pid}"
            return resp

        async def switch_then(pid: str):
            """Bring this machine's model up for the request, holding it meanwhile, then route it as usual.
            Once the wait has begun (headers sent), a failure can only end the stream: a hub cannot fall back from it
            (a local role still tries its own fallbacks, inside the stream). Deliberate: for a model the client picked,
            a clear "did not start" beats a different model answering mid-stream as if nothing happened."""
            if not data.get("stream"):
                async for kind, why in switch_progress(pid):
                    if kind == "ready":
                        return label_fallback(await route(), pid)    # it may still go to a backup (too long for it)
                    if kind == "error":
                        if local_role:
                            # our own role's request: its other targets and fallbacks still get their turn
                            return label_fallback(await route(), pid)
                        await log(status=503, error=why[:500])
                        return JSONResponse({"error": {"type": "unavailable", "message": why}}, status_code=503,
                                            headers={"Retry-After": "15", "X-Hangar-Switching-To": f"{me}/{pid}"})

            progress = switch_progress(pid)
            kind, text = await progress.__anext__()      # the first event decides the response: still time to answer properly
            if kind == "ready":                          # up already: the engine's real status codes reach the hub
                return label_fallback(await route(), pid)
            if kind == "error":
                if local_role:
                    return label_fallback(await route(), pid)
                await log(status=503, error=text[:500])
                return JSONResponse({"error": {"type": "unavailable", "message": text}}, status_code=503,
                                    headers={"Retry-After": "15", "X-Hangar-Switching-To": f"{me}/{pid}"})

            async def relay_into_stream(resp):
                """A routed answer inside this open stream: its bytes, or its error, then end."""
                if not isinstance(resp, StreamingResponse):  # routing failed after all: its error, in the stream
                    try:
                        message = (json.loads(resp.body or b"{}").get("error") or {}).get("message")
                    except ValueError:
                        message = None
                    yield stream_error(path, message or f"HTTP {resp.status_code}")
                    return
                if resp.status_code >= 400:
                    # an engine error would otherwise ride inside this 200 stream: read it and end the stream
                    parts, size = [], 0
                    try:
                        async for chunk in resp.body_iterator:
                            parts.append(chunk)
                            size += len(chunk)
                            if size >= 8192:
                                break
                    finally:
                        with anyio.CancelScope(shield=True):
                            await resp.body_iterator.aclose()
                            if isinstance(resp, UpstreamResponse):
                                await resp.on_close()
                    body = b"".join(parts).decode("utf-8", "replace").strip()
                    try:
                        message = (json.loads(body or "{}").get("error") or {}).get("message")
                    except (ValueError, AttributeError):
                        message = None
                    yield stream_error(path, message or body or f"HTTP {resp.status_code}")
                    return
                try:
                    async for chunk in resp.body_iterator:
                        yield chunk
                finally:
                    with anyio.CancelScope(shield=True):
                        await resp.body_iterator.aclose()
                        if isinstance(resp, UpstreamResponse):
                            await resp.on_close()

            async def events():
                try:
                    yield keepalive(path, model, text)   # the tick we took to make the decision
                    async for kind, why in progress:
                        if kind == "tick":
                            yield keepalive(path, model, why)
                            continue
                        if kind == "error":
                            if local_role:
                                # our own role's model failed mid-wait: its other targets still get their turn
                                async for chunk in relay_into_stream(await route()):
                                    yield chunk
                                return
                            await log(status=503, error=why[:500])
                            yield stream_error(path, why)
                            return
                        async for chunk in relay_into_stream(await route()):
                            yield chunk
                        return
                finally:
                    with anyio.CancelScope(shield=True):
                        await progress.aclose()          # the client went away mid-wait: stop asking
            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                              "X-Hangar-Switching-To": f"{me}/{pid}"})

        if switch_pid:
            return await switch_then(switch_pid)
        return await route()

    def request_filters(client: str | None = None, caller: str | None = None, engine: str | None = None,
                        model: str | None = None, machine: str | None = None, profile: str | None = None,
                        result: str = "all"):
        return dict(client=client, caller=caller, engine=engine, model=model, machine=machine, profile=profile, result=result)

    @app.get("/api/requests")
    def requests_list(limit: int = 50, before: float | None = None, before_id: int | None = None,
                      period: str = "all", filters: dict = Depends(request_filters)):
        return guard(mgr.store.requests, min(500, max(1, limit)), before, period, filters, before_id)

    @app.get("/api/inflight", include_in_schema=False)
    async def inflight_list():
        """Gateway requests that are generating right now (headers arrived; queued ones are not here yet)."""
        now = time.time()
        def row(e):
            m = e["meter"]
            t, u = m.timings, m.usage
            gen = t.get("predicted_n") or u.get("completion_tokens") or u.get("output_tokens")
            return {k: v for k, v in e.items() if k != "meter"} | {
                "elapsed_s": round(now - e["started"], 1), "streaming": m.token_at is not None, "gen_tokens": gen,
                "tok_s": round(gen / (now - m.token_at), 1) if gen and m.token_at and now > m.token_at else None}
        return {"time": now, "requests": sorted((row(e) for e in inflight.values()), key=lambda e: e["started"])}

    @app.get("/api/requests/summary")
    def requests_summary(period: str = "day", by: str = "target", filters: dict = Depends(request_filters)):
        return guard(mgr.store.request_summary, period, by, filters)

    @app.get("/api/requests/options")
    def requests_options(period: str = "day"):
        return guard(mgr.store.request_options, period)

    @app.get("/api/requests/series")
    def requests_series(period: str = "day", bucket: str = "hour", filters: dict = Depends(request_filters)):
        return guard(mgr.store.request_series, period, bucket, filters)

    # ---- reasoning effort of a thinking model (live on Strata; a restart on llama.cpp)
    @app.get("/api/reasoning")
    def reasoning_get(engine: str, profile: str | None = None):
        return guard(mgr.reasoning, engine, profile)

    @app.post("/api/reasoning")
    def reasoning_set(engine: str = Body(..., embed=True), level: str = Body(..., embed=True),
                      profile: str | None = Body(None, embed=True), restart: bool = Body(False, embed=True)):
        return guard(mgr.set_reasoning, engine, level, profile, restart)

    # ---- memory budget: how much of this machine (VRAM + RAM) Hangar's engines may use
    @app.get("/api/budget")
    def budget_get():
        return mgr.budget_public()

    @app.post("/api/budget")
    def budget_set(gb: float | None = Body(None, embed=True)):
        return guard(mgr.set_budget, gb)

    # ---- start with Windows
    @app.get("/api/autostart")
    def autostart_status():
        return autostart.status()

    @app.post("/api/autostart")
    def autostart_set(enabled: bool = Body(..., embed=True)):
        res = guard(autostart.enable) if enabled else autostart.disable()
        mgr.store.event("autostart", "starts with Windows" if enabled else "no longer starts with Windows")
        return res

    # ---- peers: other Hangars, and any of their API calls through /api/peers/<name>/<path>
    proxy = httpx.AsyncClient(timeout=httpx.Timeout(30, read=660))     # ?wait= on a peer's job can take minutes

    @app.get("/api/peers")
    def peers_list():
        me, others = summary(mgr.status()), peers.public()
        return {"self": {**machine_info(mgr.cfg), "summary": me}, "peers": others,
                "fleet": fleet([me] + [p["summary"] for p in others if p["online"]])}

    def my_code() -> dict:
        """This machine's pairing code: its name, every address another machine may reach it at, and its token."""
        port = int(mgr.cfg["manager"]["port"])
        hosts = [h for h in clients.hosts() if h["host"] != "127.0.0.1"]
        # fastest to try first; the bare PC name last: Windows can take 15 s+ to look one up (127.0.0.1 is refused at
        # once on another machine and lets two Hangars on one machine pair)
        order = {"Tailscale name": 0, "Tailscale IP": 1}
        hosts.sort(key=lambda h: order.get(h["label"], 2 if h["label"].startswith("LAN") else 4))
        pcs = [h for h in hosts if h["label"] == "PC name"]
        urls = ([f"http://{h['host']}:{port}" for h in hosts if h not in pcs] + [f"http://127.0.0.1:{port}"]
                + [f"http://{h['host']}:{port}" for h in pcs])
        name = machine_info(mgr.cfg)["name"]
        return {"code": make_code(name, urls, token), "name": name, "urls": urls}

    @app.get("/api/pairing-code")
    def pairing_code():
        return my_code()

    @app.post("/api/peers")
    def peers_add(name: str | None = Body(None, embed=True), url: str | None = Body(None, embed=True),
                  token_: str = Body("", alias="token", embed=True), code: str | None = Body(None, embed=True),
                  both: bool = Body(False, embed=True), reciprocal_url: str | None = Body(None, embed=True)):
        """Add a peer from its pairing code (or an address and token), checked first. both: the peer gets this
        machine too (it is sent this machine's pairing code and keeps whichever address it can reach)."""
        res = guard(peers.add, name, url, token_, True, code)
        if both or reciprocal_url:
            token_ = peers.get(res["name"])["token"]           # the one actually saved (may have come from a code)
            body = ({"name": machine_info(mgr.cfg)["name"], "url": guard(normalize_url, reciprocal_url)[0], "token": token}
                    if reciprocal_url else {"code": my_code()["code"]})
            try:
                r = httpx.post(res["url"] + "/api/peers", headers={"Authorization": f"Bearer {token_}"}, timeout=30,
                               json=body)
                res["reciprocal"] = "added" if r.status_code == 200 else f"failed: {r.json().get('detail', r.status_code)}"
            except (httpx.HTTPError, ValueError) as e:
                res["reciprocal"] = f"failed: {e}"
        mgr.store.event("peer", f"added peer {res['name']} ({res['url']})")
        return res

    @app.post("/api/wake")
    def wake(machine: str = Body(..., embed=True)):
        res = guard(peers.wake, machine, True)
        mgr.store.event("wake", f"sent a wake-on-LAN packet to {machine}")
        return res

    @app.post("/api/wake/auto")
    def wake_auto(machine: str = Body(..., embed=True), on: bool = Body(..., embed=True)):
        guard(peers.set_wake, machine, on)
        mgr.store.event("wake", f"{machine} is {'now' if on else 'no longer'} woken when a request needs it")
        return {"machine": machine, "auto": on}

    @app.delete("/api/peers/{name}")
    def peers_remove(name: str):
        guard(peers.remove, name)
        mgr.store.event("peer", f"removed peer {name}")
        return {"removed": name}

    @app.api_route("/api/peers/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
    async def peers_proxy(name: str, path: str, request: Request):
        if "%" in path or path.startswith("/") or "\\" in path or any(p in (".", "..") for p in path.split("/")):
            raise HTTPException(400, "invalid peer API path")
        # a peer's list of its machines may be read (its role editor needs the names); nothing further through it
        if (path == "peers" and request.method != "GET") or path.startswith("peers/"):
            raise HTTPException(400, "a peer's own peers are not reachable through it; add them here")
        p = guard(peers.get, name)
        body = await request.body()
        params = [(k, v) for k, v in request.query_params.multi_items() if k.lower() != "token"]
        req = proxy.build_request(request.method, f"{p['url']}/api/{path}", params=params, content=body or None,
                                  headers={"Authorization": f"Bearer {p['token']}",
                                           "Content-Type": request.headers.get("content-type", "application/json")})
        try:
            r = await proxy.send(req, stream=True)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"{name} is unreachable ({type(e).__name__})")
        if r.status_code == 401:            # not ours: a 401 here would sign the browser out of this Hangar
            await r.aclose()
            raise HTTPException(502, f"{name} refused the saved token; remove the peer and add it with its current token")
        if path.endswith("/stream"):
            async def relay():
                try:
                    async for chunk in r.aiter_bytes():
                        yield chunk
                finally:
                    with anyio.CancelScope(shield=True):
                        await r.aclose()
            return UpstreamResponse(relay(), on_close=r.aclose, status_code=r.status_code, media_type=r.headers.get("content-type"),
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        try:
            content = await r.aread()
        finally:
            with anyio.CancelScope(shield=True):
                await r.aclose()
        return Response(content, status_code=r.status_code, media_type=r.headers.get("content-type"))

    @app.post("/api/reload-config")
    def reload_config():
        guard(mgr.reload_config)
        return {"ok": True, "profiles": len(mgr.profiles)}

    app.mount("/", mcp_app)          # serves /mcp; mounted last so the routes above win
    return app


def serve():
    import logging

    import uvicorn

    for noisy in ("httpx", "httpcore", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    mgr = Manager()
    m = mgr.cfg["manager"]
    print(f"Hangar on http://{m['host']}:{m['port']}  (token in {m['data_dir'] / 'token.txt'})", flush=True)
    # on SIGTERM: stop accepting, let in-flight requests (which can stream for minutes) finish within this budget,
    # then exit cleanly - instead of waiting forever and being SIGKILLed by systemd mid-stream.
    uvicorn.run(create_app(mgr), host=m["host"], port=int(m["port"]), log_level="warning",
                timeout_graceful_shutdown=int(m.get("shutdown_grace_s", DRAIN_BUDGET_S)))
