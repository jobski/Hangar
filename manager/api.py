"""REST API + web UI + MCP, all behind one token (Authorization: Bearer <token>, or ?token= for the web page)."""
from __future__ import annotations

import codecs
import contextlib
import hmac
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
from .peers import Peers, machine_info, summary

WEB = Path(__file__).parent / "web"
OPEN_PATHS = {"/", "/favicon.svg", "/api/ping", "/manifest.webmanifest"}


def create_app(mgr: Manager | None = None) -> FastAPI:
    mgr = mgr or Manager()
    token = config_mod.token(mgr.cfg)
    peers = mgr.peers = Peers(mgr.cfg)
    mcp = build_mcp(mgr)
    # the token check below protects /mcp; the Host-header check would block access by Tailscale name or LAN IP
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="0.0.0.0",
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        mgr.start_background()
        peers.start()
        async with mcp.session_manager.run():
            yield
        peers.shutdown()
        await proxy.aclose()
        mgr.shutdown()

    app = FastAPI(title="Hangar", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.mgr = mgr

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith("/static/") or path in ("/api/docs", "/api/openapi.json"):
            return await call_next(request)
        given = request.headers.get("authorization", "")
        given = given[7:] if given.lower().startswith("bearer ") else request.query_params.get("token", "")
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
        guard(mgr.library.delete, folder, mgr.profiles)
        mgr.store.event("model", f"deleted {folder}")
        return {"ok": True}

    @app.get("/api/search")
    def search(q: str, source: str = "huggingface", limit: int = 20):
        return guard(mgr.downloader.search, source, q, min(limit, 50))

    @app.get("/api/repo-files")
    def repo_files(repo: str, source: str = "huggingface", revision: str | None = None):
        return guard(mgr.downloader.list_files, source, repo, revision)

    @app.get("/api/downloads")
    def downloads():
        return {"items": mgr.downloader.list(), "aria2": mgr.downloader.aria2_path()}

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

    # ---- start with Windows
    @app.get("/api/autostart")
    def autostart_status():
        return autostart.status()

    @app.post("/api/autostart")
    def autostart_set(enabled: bool = Body(..., embed=True)):
        res = autostart.enable() if enabled else autostart.disable()
        mgr.store.event("autostart", "starts with Windows" if enabled else "no longer starts with Windows")
        return res

    # ---- peers: other Hangars, and any of their API calls through /api/peers/<name>/<path>
    proxy = httpx.AsyncClient(timeout=httpx.Timeout(30, read=660))     # ?wait= on a peer's job can take minutes

    @app.get("/api/peers")
    def peers_list():
        return {"self": {**machine_info(mgr.cfg), "summary": summary(mgr.status())}, "peers": peers.public()}

    @app.post("/api/peers")
    def peers_add(name: str = Body(..., embed=True), url: str = Body(..., embed=True), token_: str = Body(..., alias="token", embed=True),
                  reciprocal_url: str | None = Body(None, embed=True)):
        """Add a peer (checked first). reciprocal_url: how that peer reaches this machine - then it gets this one too."""
        res = guard(peers.add, name, url, token_)
        if reciprocal_url:
            try:
                r = httpx.post(res["url"] + "/api/peers", headers={"Authorization": f"Bearer {token_}"}, timeout=15,
                               json={"name": machine_info(mgr.cfg)["name"], "url": reciprocal_url, "token": token})
                res["reciprocal"] = "added" if r.status_code == 200 else f"failed: {r.json().get('detail', r.status_code)}"
            except (httpx.HTTPError, ValueError) as e:
                res["reciprocal"] = f"failed: {e}"
        mgr.store.event("peer", f"added peer {res['name']} ({res['url']})")
        return res

    @app.delete("/api/peers/{name}")
    def peers_remove(name: str):
        guard(peers.remove, name)
        mgr.store.event("peer", f"removed peer {name}")
        return {"removed": name}

    @app.api_route("/api/peers/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
    async def peers_proxy(name: str, path: str, request: Request):
        if path == "peers" or path.startswith("peers/"):
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
                    async for chunk in r.aiter_raw():
                        yield chunk
                finally:
                    await r.aclose()
            return StreamingResponse(relay(), status_code=r.status_code, media_type=r.headers.get("content-type"),
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        content = await r.aread()
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
