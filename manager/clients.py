"""Ready-to-paste configs: the models for coding agents and chat apps, and the manager's own MCP server for MCP clients.

Each generator returns {title, file, language, content, notes, apply?}. `apply` means the manager can merge it into
that tool's config file on this PC itself (a backup is written first).
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psutil

from .engines import Profile
from .engines.base import arg_value

HOME = Path.home()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------------------------------------------------------------------------------ addresses
def hosts() -> list[dict]:
    """Addresses this PC can be reached at: localhost, Tailscale (name and IP), LAN."""
    out = [{"host": "127.0.0.1", "label": "This PC only"}]
    ts = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        st = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=5,
                                       creationflags=NO_WINDOW).stdout or "{}")
        me = st.get("Self") or {}
        if me.get("DNSName"):
            out.append({"host": me["DNSName"].rstrip("."), "label": "Tailscale name"})
        for ip in me.get("TailscaleIPs") or []:
            if ":" not in ip:
                out.append({"host": ip, "label": "Tailscale IP"})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.", "100.")):
                out.append({"host": a.address, "label": f"LAN ({name})"})
    out.append({"host": socket.gethostname(), "label": "PC name"})
    seen, uniq = set(), []
    for h in out:
        if h["host"] not in seen:
            seen.add(h["host"])
            uniq.append(h)
    return uniq


# ------------------------------------------------------------------------------------------------ the models
def model_entries(mgr, profile_ids: list[str] | None, host: str) -> list[dict]:
    """What a client needs per profile: provider, base URL, key, model id, context, vision."""
    out = []
    for p in mgr.profiles.values():
        if p.source == "custom" or (profile_ids and p.id not in profile_ids):
            continue
        eng = mgr.engines[p.engine]
        e = {"profile": p.id, "engine": p.engine, "provider": f"{p.engine}", "provider_name": _provider_name(p.engine),
             "base": f"http://{host}:{eng.port}", "api_key": "", "model": p.id, "name": p.description or p.id,
             "context": 32768, "output": 16384, "vision": False, "reasoning": False}
        if p.engine and eng.plugin == "llamacpp":
            e.update(model=p.cfg.get("alias") or p.id, api_key=eng.api_key, vision=bool(p.cfg.get("mmproj")),
                     context=int(p.cfg.get("ctx") or arg_value([str(a) for a in p.cfg.get("args", [])], "-c") or 32768))
        elif eng.plugin == "strata":
            rc = eng._run_config(p)
            key = rc.get("api_key") or eng.api_key or ""
            e.update(model=rc.get("model_name") or p.id, api_key=key if isinstance(key, str) else (key[0] if key else ""),
                     vision=bool(rc.get("vision")), reasoning=True,
                     context=int(arg_value(rc.get("args", []), "--max-context") or 131072), output=32768)
        else:
            e.update(api_key=eng.api_key)
        e["output"] = min(e["output"], max(1024, e["context"] // 4))
        e["env_key"] = f"{p.engine.upper()}_API_KEY"
        out.append(e)
    return out


def _provider_name(engine: str) -> str:
    return {"llamacpp": "llama.cpp (local)", "strata": "Strata (local)"}.get(engine, f"{engine} (local)")


def _by_provider(entries):
    prov: dict[str, list[dict]] = {}
    for e in entries:
        prov.setdefault(e["provider"], []).append(e)
    return prov


def _key(e, use_env: bool, style: str = "value"):
    if not e["api_key"]:
        return "none" if style == "value" else ""
    return f"{{env:{e['env_key']}}}" if use_env and style == "opencode" else (f"${e['env_key']}" if use_env else e["api_key"])


def gen_opencode(entries, use_env):
    cfg = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    for prov, es in _by_provider(entries).items():
        opts = {"baseURL": es[0]["base"] + "/v1"}
        if es[0]["api_key"]:
            opts["apiKey"] = _key(es[0], use_env, "opencode")
        models = {}
        for e in es:
            m = {"name": e["name"], "limit": {"context": e["context"], "output": e["output"]}}
            if e["vision"]:
                m.update(attachment=True, modalities={"input": ["text", "image"], "output": ["text"]})
            models[e["model"]] = m
        cfg["provider"][prov] = {"npm": "@ai-sdk/openai-compatible", "name": es[0]["provider_name"], "options": opts,
                                 "models": models}
    if entries:
        cfg["model"] = f"{entries[0]['provider']}/{entries[0]['model']}"
    return {"title": "opencode", "file": str(HOME / ".config" / "opencode" / "opencode.json"), "language": "json",
            "content": json.dumps(cfg, indent=2), "apply": "opencode",
            "notes": "Merged into your opencode.json: these providers are replaced, everything else is kept. Pick the model "
                     "with /models."}


def gen_qwen(entries, use_env):
    provs = []
    env = {}
    for e in entries:
        item = {"id": e["model"], "name": e["name"], "baseUrl": e["base"] + "/v1",
                "wireApi": "responses" if e["engine"] == "strata" else "chat-completions"}
        if e["api_key"]:
            item["envKey"] = e["env_key"]
            env[e["env_key"]] = e["api_key"]
        provs.append(item)
    cfg = {"env": env, "modelProviders": {"openai": provs}}
    if entries:
        cfg["model"] = {"name": entries[0]["model"], "baseUrl": entries[0]["base"] + "/v1"}
    return {"title": "Qwen Code", "file": str(HOME / ".qwen" / "settings.json"), "language": "json",
            "content": json.dumps(cfg, indent=2), "apply": "qwen",
            "notes": "Merged into ~/.qwen/settings.json: entries with these model ids are replaced, others kept. Switch with "
                     "/model. The keys go in \"env\" (Qwen Code reads them by envKey)."}


def gen_pi(entries, use_env):
    provs = {}
    for prov, es in _by_provider(entries).items():
        provs[prov] = {"baseUrl": es[0]["base"] + "/v1", "api": "openai-completions",
                       "apiKey": _key(es[0], use_env) if es[0]["api_key"] else "none",
                       "models": [{"id": e["model"], "name": e["name"], "reasoning": e["reasoning"],
                                   "input": ["text", "image"] if e["vision"] else ["text"],
                                   "contextWindow": e["context"], "maxTokens": e["output"],
                                   "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}} for e in es]}
    return {"title": "Pi", "file": str(HOME / ".pi" / "agent" / "models.json"), "language": "json",
            "content": json.dumps({"providers": provs}, indent=2), "apply": "pi",
            "notes": "Pi's custom models file. Merged by provider. Pick the model with /model (or pi --model)."}


def _toml_key(k: str) -> str:
    return k if k.replace("-", "").replace("_", "").isalnum() else json.dumps(k)


def gen_codex(entries, use_env):
    lines, env = [], []
    for prov, es in _by_provider(entries).items():
        lines += [f"[model_providers.{_toml_key(prov)}]", f'name = "{es[0]["provider_name"]}"', f'base_url = "{es[0]["base"]}/v1"',
                  'wire_api = "responses"']
        if es[0]["api_key"]:
            lines.append(f'env_key = "{es[0]["env_key"]}"')
            env.append(f'{es[0]["env_key"]}={es[0]["api_key"]}')
        lines.append("")
    for e in entries:
        lines += [f"[profiles.{_toml_key(e['profile'])}]", f'model_provider = "{e["provider"]}"', f'model = "{e["model"]}"',
                  f"model_context_window = {e['context']}", ""]
    notes = "Add to ~/.codex/config.toml, then run: codex --profile <profile>."
    if env:
        notes += " Set the key(s) as user environment variables: " + ", ".join(env)
    return {"title": "Codex CLI", "file": str(HOME / ".codex" / "config.toml"), "language": "toml",
            "content": "\n".join(lines).strip() + "\n", "notes": notes}


def gen_claude(entries, use_env):
    blocks = []
    for e in entries:
        key = e["api_key"] or "none"
        blocks.append(f"# {e['name']}  ({e['engine']})\n"
                      f"$env:ANTHROPIC_BASE_URL = \"{e['base']}\"\n$env:ANTHROPIC_AUTH_TOKEN = \"{key}\"\n"
                      f"$env:ANTHROPIC_MODEL = \"{e['model']}\"\n$env:ANTHROPIC_SMALL_FAST_MODEL = \"{e['model']}\"\nclaude\n")
    return {"title": "Claude Code (local model)", "file": "PowerShell, before running claude", "language": "powershell",
            "content": "\n".join(blocks),
            "notes": "Runs Claude Code against a local model through the engine's Anthropic-compatible /v1/messages API "
                     "(Strata has it; recent llama-server builds too). Use one block per session."}


def gen_aider(entries, use_env):
    rows = [f"aider --model openai/{e['model']} --openai-api-base {e['base']}/v1 --openai-api-key {e['api_key'] or 'none'}"
            for e in entries]
    return {"title": "Aider", "file": "command line", "language": "bash", "content": "\n".join(rows),
            "notes": "One line per model."}


def gen_continue(entries, use_env):
    lines = ["models:"]
    for e in entries:
        lines += [f"  - name: {e['name']}", "    provider: openai", f"    model: {e['model']}",
                  f"    apiBase: {e['base']}/v1", f"    apiKey: {e['api_key'] or 'none'}",
                  "    roles: [chat, edit, apply]", f"    defaultCompletionOptions: {{contextLength: {e['context']}}}"]
    return {"title": "Continue", "file": str(HOME / ".continue" / "config.yaml"), "language": "yaml",
            "content": "\n".join(lines) + "\n", "notes": "Add the entries under models: in Continue's config.yaml."}


def gen_generic(entries, use_env):
    rows = []
    for e in entries:
        rows.append(f"# {e['name']} ({e['engine']}, {e['context']:,} context{', vision' if e['vision'] else ''})\n"
                    f"OPENAI_BASE_URL={e['base']}/v1\nOPENAI_API_KEY={e['api_key'] or 'none'}\nOPENAI_MODEL={e['model']}\n"
                    f"# Anthropic-style clients: {e['base']}  (POST /v1/messages)\n"
                    f"curl {e['base']}/v1/chat/completions -H \"Authorization: Bearer {e['api_key'] or 'none'}\" "
                    f"-H \"Content-Type: application/json\" -d '{{\"model\":\"{e['model']}\",\"messages\":[{{\"role\":\"user\","
                    f"\"content\":\"hi\"}}]}}'\n")
    return {"title": "Any OpenAI-compatible app", "file": "Open WebUI, LibreChat, Cline, Roo, Cursor, scripts ...",
            "language": "bash", "content": "\n".join(rows),
            "notes": "Base URL, key and model id for any app that speaks the OpenAI API."}


MODEL_CLIENTS = {"opencode": gen_opencode, "qwen": gen_qwen, "pi": gen_pi, "codex": gen_codex, "claude": gen_claude,
                 "aider": gen_aider, "continue": gen_continue, "generic": gen_generic}


# --------------------------------------------------------------------------------------------- the MCP server
def mcp_configs(host: str, port: int, token: str) -> list[dict]:
    url = f"http://{host}:{port}/mcp"
    auth = f"Bearer {token}"
    hdr = {"Authorization": auth}
    return [
        {"id": "claude", "title": "Claude Code", "file": "command line", "language": "bash",
         "content": f'claude mcp add --scope user --transport http hangar {url} --header "Authorization: {auth}"',
         "notes": "--scope user makes it available in every project. Check with /mcp."},
        {"id": "claude-json", "title": "Claude Code (.mcp.json, per project)", "file": ".mcp.json in a project", "language": "json",
         "content": json.dumps({"mcpServers": {"hangar": {"type": "http", "url": url, "headers": hdr}}}, indent=2)},
        {"id": "claude-desktop", "title": "Claude Desktop", "file": "%APPDATA%\\Claude\\claude_desktop_config.json",
         "language": "json", "content": json.dumps({"mcpServers": {"hangar": {"command": "npx", "args": [
             "-y", "mcp-remote", url, "--allow-http", "--header", f"Authorization:{auth}"]}}}, indent=2),
         "notes": "Claude Desktop reaches HTTP servers through the mcp-remote bridge (needs Node.js)."},
        {"id": "codex", "title": "Codex CLI", "file": str(HOME / ".codex" / "config.toml"), "language": "toml",
         "content": f'[mcp_servers.hangar]\nurl = "{url}"\nhttp_headers = {{ Authorization = "{auth}" }}\n'},
        {"id": "qwen", "title": "Qwen Code", "file": str(HOME / ".qwen" / "settings.json"), "language": "json",
         "content": json.dumps({"mcpServers": {"hangar": {"httpUrl": url, "headers": hdr}}}, indent=2),
         "apply": "qwen-mcp", "notes": "Merged into settings.json. /mcp lists it."},
        {"id": "gemini", "title": "Gemini CLI", "file": str(HOME / ".gemini" / "settings.json"), "language": "json",
         "content": json.dumps({"mcpServers": {"hangar": {"httpUrl": url, "headers": hdr}}}, indent=2)},
        {"id": "opencode", "title": "opencode", "file": str(HOME / ".config" / "opencode" / "opencode.json"),
         "language": "json", "apply": "opencode-mcp",
         "content": json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {"hangar": {
             "type": "remote", "url": url, "headers": hdr, "enabled": True}}}, indent=2)},
        {"id": "cursor", "title": "Cursor", "file": str(HOME / ".cursor" / "mcp.json"), "language": "json",
         "content": json.dumps({"mcpServers": {"hangar": {"url": url, "headers": hdr}}}, indent=2)},
        {"id": "vscode", "title": "VS Code (Copilot)", "file": ".vscode/mcp.json or user mcp.json", "language": "json",
         "content": json.dumps({"servers": {"hangar": {"type": "http", "url": url, "headers": hdr}}}, indent=2)},
        {"id": "pi", "title": "Pi", "file": "-", "language": "text",
         "content": "Pi has no built-in MCP support (by design). Use the hangar CLI from Pi's shell tool instead:\n"
                    f"  hangar login http://{host}:{port} {token}\n  hangar status",
         "notes": "Or install an MCP extension for Pi if you use one."},
        {"id": "raw", "title": "Raw values", "file": "-", "language": "text",
         "content": f"URL:       {url}\nTransport: streamable HTTP\nHeader:    Authorization: {auth}"},
    ]


# --------------------------------------------------------------------------------------------- apply locally
def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.read_text(encoding="utf-8").strip() else {}


def _write_json(path: Path, data: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(backup) if backup else ""


APPLY = {"opencode", "opencode-mcp", "qwen", "qwen-mcp", "pi"}


def apply(kind: str, snippet: dict) -> dict:
    """Merge a generated snippet into the tool's config file on this PC."""
    if kind not in APPLY:
        raise ValueError(f"cannot apply {kind}; one of {', '.join(sorted(APPLY))}")
    path = Path(snippet["file"])
    new = json.loads(snippet["content"])
    cur = _load_json(path)
    if kind == "opencode":
        cur.setdefault("$schema", "https://opencode.ai/config.json")
        cur.setdefault("provider", {}).update(new["provider"])
        cur.setdefault("model", new.get("model"))
    elif kind == "opencode-mcp":
        cur.setdefault("$schema", "https://opencode.ai/config.json")
        cur.setdefault("mcp", {}).update(new["mcp"])
    elif kind == "qwen":
        cur.setdefault("env", {}).update(new["env"])
        lst = cur.setdefault("modelProviders", {}).setdefault("openai", [])
        ids = {m["id"] for m in new["modelProviders"]["openai"]}
        cur["modelProviders"]["openai"] = [m for m in lst if m.get("id") not in ids] + new["modelProviders"]["openai"]
    elif kind == "qwen-mcp":
        cur.setdefault("mcpServers", {}).update(new["mcpServers"])
    elif kind == "pi":
        cur.setdefault("providers", {}).update(new["providers"])
    backup = _write_json(path, cur)
    return {"file": str(path), "backup": backup}

