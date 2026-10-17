"""REST API + web UI + MCP, all behind one token (Authorization: Bearer <token>, or ?token= for the web page)."""
from __future__ import annotations

import codecs
import contextlib
import hmac
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from mcp.server.transport_security import TransportSecuritySettings

from . import autostart, clients
from . import config as config_mod
from .core import Manager
from .mcp_server import build as build_mcp
from .peers import Peers, fleet, machine_info, make_code, normalize_url, summary
from .roles import WAKE_WAIT_S, Roles
from .stats import RequestMeter, client_name, merge_series, merge_summaries

WEB = Path(__file__).parent / "web"
TOO_LONG = re.compile(r"context|too long|too many tokens|exceed|n_ctx|maximum", re.I)
OPEN_PATHS = {"/", "/favicon.svg", "/api/ping", "/manifest.webmanifest"}


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


def create_app(mgr: Manager | None = None) -> FastAPI:
    mgr = mgr or Manager()
    token = config_mod.token(mgr.cfg)
    peers = mgr.peers = Peers(mgr.cfg)
    roles = mgr.roles = Roles(mgr, peers)
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
        mgr.shutdown()

    app = FastAPI(title="Hangar", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
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

    def job_reply(job, wait: float):
        if wait:
            job.wait(min(wait, 600))
        return job.public(log_lines=60)

    # ---- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return FileResponse(WEB / "favicon.svg")

    @app.get("/api/ping")
    def ping():
        return {"ok": True, "service": "hangar"}

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
            yield data.decode("utf-8", "replace")
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
    def job(job_id: str, wait: float = 0, lines: int = 200):
        with mgr.lock:
            j = mgr.jobs.get(job_id)
        if not j:
            raise HTTPException(404, f"no job {job_id}")
        if wait:
            j.wait(min(wait, 600))
        return j.public(log_lines=lines)

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
    def start(profile: str = Body(..., embed=True), force: bool = Body(False, embed=True),
              update: str = Body("auto", embed=True), wait: float = 0):
        return job_reply(guard(mgr.start, profile, force, update), wait)

    @app.post("/api/switch")
    def switch(profile: str = Body(..., embed=True), force: bool = Body(False, embed=True),
               update: str = Body("auto", embed=True), wait: float = 0):
        return job_reply(guard(mgr.start, profile, force, update), wait)

    @app.post("/api/stop")
    def stop(engine: str = Body(..., embed=True), wait: float = 0):
        return job_reply(guard(mgr.stop, engine), wait)

    @app.post("/api/restart")
    def restart(engine: str = Body(..., embed=True), wait: float = 0):
        return job_reply(guard(mgr.restart, engine), wait)

    @app.post("/api/update")
    def update(engine: str = Body(..., embed=True), start_after: bool = Body(True, embed=True), wait: float = 0):
        return job_reply(guard(mgr.update, engine, start_after), wait)

    @app.post("/api/check-updates")
    def check_updates(engine: str | None = Body(None, embed=True), wait: float = 0):
        return job_reply(guard(mgr.check_updates, engine), wait)

    @app.post("/api/rollback")
    def rollback(engine: str = Body(..., embed=True), start_after: bool = Body(True, embed=True), wait: float = 0):
        return job_reply(guard(mgr.rollback, engine, start_after), wait)

    @app.post("/api/run-custom")
    def run_custom(profile: str = Body(..., embed=True), overrides: dict = Body({}, embed=True),
                   force: bool = Body(False, embed=True), update: str = Body("auto", embed=True),
                   save_as: str | None = Body(None, embed=True), wait: float = 0):
        return job_reply(guard(mgr.run_custom, profile, overrides, force, update, save_as), wait)

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
        # the roles, through this Hangar's gateway: the model name is the role (main, helper, ...)
        for r in roles.public():
            if r["targets"] or r["name"] == "main":
                ctx = roles.context(r["name"])
                entries.append({"profile": f"role-{r['name']}", "engine": "hangar", "provider": "hangar",
                                "provider_name": "Hangar roles", "base": f"http://{host}:{port}", "api_key": token,
                                "model": r["name"], "name": f"{r['name']} (Hangar role)", "context": ctx,
                                "output": min(16384, max(1024, ctx // 4)), "vision": False, "reasoning": False,
                                "env_key": "HANGAR_TOKEN"})
        return clients.MODEL_CLIENTS[client](entries, env)

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

    # ---- roles (main, helper, ...) and the gateway that serves them
    @app.get("/api/roles")
    def roles_list():
        return roles.public()

    @app.post("/api/roles")
    def roles_save(name: str = Body(..., embed=True), targets: list[dict] = Body(..., embed=True),
                   spill: bool | None = Body(None, embed=True)):
        return guard(roles.save, name, targets, spill)

    @app.delete("/api/roles/{name}")
    def roles_delete(name: str):
        guard(roles.delete, name)
        return roles.public()

    routing_lock = anyio.Lock()
    gateway = httpx.AsyncClient(timeout=httpx.Timeout(10, read=None))   # generations can stream for many minutes

    @app.get("/v1/models")
    def gateway_models():
        data = [{"id": r["name"], "object": "model", "owned_by": "hangar-role"} for r in roles.public()]
        data += [{"id": i.profile, "object": "model", "owned_by": "hangar-profile"} for i in mgr.instances.values()]
        return {"object": "list", "data": data}

    async def direct_engine(t: dict) -> tuple[str, str] | None:
        """For a peer without a gateway: its engine's /v1/models URL (from the peer's status) and the model id it
        serves."""
        st = (peers.state.get(t["machine"]) or {}).get("status") or {}
        port = next((e["port"] for e in st.get("engines") or [] if (e.get("running") or {}).get("profile") == t["profile"]), None)
        if not port:
            return None
        host = peers.get(t["machine"])["url"].split("://", 1)[1].rsplit(":", 1)[0]
        url = f"http://{host}:{port}/v1/models"
        try:
            ids = [m["id"] for m in (await gateway.get(url, timeout=5)).json().get("data", [])]
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        return (url, ids[0]) if ids else None

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
        model = str(data.get("model") or "main")
        me = machine_info(mgr.cfg)["name"]
        client, via = client_name(request.headers), request.headers.get("x-hangar-via")
        # the device: as a hub that passed it on saw it, else who connected here
        caller = (request.headers.get("x-hangar-caller", "")[:40] if via else "") or \
            await anyio.to_thread.run_sync(peers.caller_name, request.client.host if request.client else "")

        def log(**kw):
            try:
                mgr.store.log_request(t0, model=model, client=client, caller=caller, via=via, path=path, stream=int(bool(data.get("stream"))),
                                      duration_s=time.time() - t0, **kw)
            except Exception:  # noqa: BLE001 - the log never fails a request
                pass
        woken = await anyio.to_thread.run_sync(roles.wake_for, model)
        # rough size of the request in tokens (~4 bytes each) plus what it may generate: a spill target must fit it
        need = len(raw) // 4 + max(0, max_tokens)
        cands = await anyio.to_thread.run_sync(roles.candidates, model, need, (client, caller))
        if not cands and (woken or await anyio.to_thread.run_sync(roles.starting, model)):
            cands = await anyio.to_thread.run_sync(roles.wait_for, model, woken)
        if not cands:
            names = ", ".join(r["name"] for r in roles.public())
            why = (f"woke {', '.join(woken)}, but nothing could answer within {WAKE_WAIT_S} s (a model may still be "
                   "loading there: try again shortly)") if woken else (f"a model for {model!r} is still starting: try again "
                   "shortly") if roles.starting(model) else f"nothing is up to serve {model!r}"
            log(status=503, error="woke " + ", ".join(woken) if woken else "nothing up")
            return JSONResponse({"error": {"type": "unavailable", "message": f"{why} (roles: {names}; or a running "
                                           "profile's id)"}}, status_code=503)
        passthrough = {k: v for k, v in request.headers.items() if k.lower() in ("accept", "anthropic-version", "anthropic-beta")}
        errors = []
        attempted = set()
        while True:
            async with routing_lock:
                cands = await anyio.to_thread.run_sync(roles.candidates, model, need, (client, caller))
                cands = [t for t in cands if (t["machine"], t["profile"]) not in attempted]
                if not cands:
                    break
                t = cands[0]
                attempted.add((t["machine"], t["profile"]))
                roles.opened(t)               # reserve before resolving/sending, including prompt processing
            r, relaying = None, False
            try:
                if t["machine"]:
                    p = peers.get(t["machine"])
                    url, mid = f"{p['url']}/v1/{path}", f"profile:{t['profile']}"
                    auth = {"Authorization": f"Bearer {p['token']}", "X-Hangar-Via": via or me, "X-Hangar-Client": client,
                            "X-Hangar-Caller": caller}
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
                if t["machine"] and r.status_code == 404:
                    # older peers have no gateway: try their engine directly when it needs no API key
                    await r.aclose()
                    direct = await direct_engine(t)
                    if direct:
                        url, mid = direct
                        req = gateway.build_request("POST", url.replace("/v1/models", f"/v1/{path}"), content=json.dumps(
                            {**data, "model": mid}).encode(), headers={**passthrough, "Content-Type": "application/json"})
                        r = await gateway.send(req, stream=True)
                if r.status_code in (502, 503, 504) or (t["machine"] and r.status_code in (401, 404)):
                    errors.append(f"{t['machine'] or 'here'}/{t['profile']}: HTTP {r.status_code}")
                    continue
                if r.status_code in (400, 413) and len(cands) > 1:
                    body = await r.aread()
                    if TOO_LONG.search(body[:4096].decode("utf-8", "replace")):
                        errors.append(f"{t['machine'] or 'here'}/{t['profile']}: too long for its context")
                        continue
                    log(machine=t["machine"] or me, profile=t["profile"], status=r.status_code, error=f"HTTP {r.status_code}")
                    return Response(body, status_code=r.status_code, media_type=r.headers.get("content-type"))
                roles.note_used(t, model, (client, caller))
                meter = RequestMeter(t0, "text/event-stream" in r.headers.get("content-type", ""))
                done, finished = False, False

                async def finish(r=r, t=t, meter=meter):
                    nonlocal finished
                    if finished:
                        return
                    finished = True
                    roles.closed(t)
                    with anyio.CancelScope(shield=True):
                        await r.aclose()
                    log(machine=t["machine"] or me, profile=t["profile"], status=r.status_code,
                        error=("closed before the end" if not done else f"HTTP {r.status_code}" if r.status_code >= 400 else None),
                        **meter.result())

                async def relay(r=r, t=t, meter=meter):
                    nonlocal done
                    try:
                        async for chunk in r.aiter_bytes():
                            meter.feed(chunk, time.time())
                            yield chunk
                        done = True
                    finally:
                        await finish()
                response = UpstreamResponse(relay(), on_close=finish, status_code=r.status_code, media_type=r.headers.get("content-type"),
                                             headers={"X-Hangar-Served-By": f"{t['machine'] or me}/{t['profile']}",
                                                      "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
                relaying = True
                return response
            except httpx.HTTPError as ex:
                errors.append(f"{t['machine'] or 'here'}/{t['profile']}: {type(ex).__name__}")
            finally:
                if not relaying:
                    roles.closed(t)
                    if r is not None:
                        with anyio.CancelScope(shield=True):
                            await r.aclose()
        log(status=502, error="; ".join(errors)[:500])
        return JSONResponse({"error": {"type": "unavailable", "message": f"every target for {model!r} failed: "
                                       + "; ".join(errors)}}, status_code=502)

    @app.get("/api/requests")
    def requests_list(limit: int = 50, before: float | None = None):
        return mgr.store.requests(min(500, max(1, limit)), before)

    @app.get("/api/requests/summary")
    def requests_summary(period: str = "day", by: str = "target"):
        return guard(mgr.store.request_summary, period, by)

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
        # a peer's list of its machines may be read (its role editor needs the names); nothing further through it
        if (path == "peers" and request.method != "GET") or path.startswith("peers/"):
            raise HTTPException(400, "a peer's own peers are not reachable through it; add them here")
        p = guard(peers.get, name)
        body = await request.body()
        req = proxy.build_request(request.method, f"{p['url']}/api/{path}", params=request.query_params, content=body or None,
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
    uvicorn.run(create_app(mgr), host=m["host"], port=int(m["port"]), log_level="warning")
