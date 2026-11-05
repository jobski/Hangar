"""Strata in a local Docker Compose service, without taking ownership of host proxy processes."""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .base import Engine, Estimate, Profile
from .strata import StrataEngine


class DockerStrataEngine(StrataEngine):
    external = True

    def __init__(self, name, cfg):
        Engine.__init__(self, name, cfg)
        self.docker = cfg['docker']
        self.container = str(self.docker['container'])
        self.service = str(self.docker.get('service', name))
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', self.container):
            raise ValueError('docker.container must be a container name or ID')
        self.compose_file = Path(self.docker['compose_file']).expanduser().resolve()
        self.project_dir = self.compose_file.parent
        self.profile_id = self.docker.get('profile', f'{name}-docker')
        self.startup_timeout_s = int(cfg.get('startup_timeout_s', 1800))
        self.stop_timeout_s = int(self.docker.get('stop_timeout_s', 120))
        self.can_update = self.can_rollback = False
        self._cached = None
        self._checked = 0.0
        self._lock = threading.RLock()
        self._log_checked = 0.0
        self._live_metadata = {}

    def _docker(self, *args, timeout=15, missing=False):
        try:
            p = subprocess.run(['docker', *map(str, args)], capture_output=True, text=True,
                               timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f'Docker unavailable: {type(exc).__name__}') from exc
        if p.returncode:
            if missing and ('No such container' in p.stderr or 'No such object' in p.stderr):
                return None
            raise RuntimeError(f'Docker {args[0]} failed: {p.stderr.strip()[:400]}')
        return p.stdout

    def _inspect(self, fresh=False):
        with self._lock:
            if not fresh and time.monotonic() - self._checked < 1:
                return self._cached
            raw = self._docker('container', 'inspect', self.container, missing=True)
            info = json.loads(raw)[0] if raw is not None else None
            if info:
                labels = info['Config'].get('Labels') or {}
                files = labels.get('com.docker.compose.project.config_files', '').split(',')
                if (labels.get('com.docker.compose.service') != self.service
                        or str(self.compose_file) not in files):
                    raise RuntimeError('Container does not belong to the configured Compose file and service')
                bindings = (info.get('HostConfig', {}).get('PortBindings') or {}).values()
                if not any(str(b.get('HostPort')) == str(self.port)
                           and b.get('HostIp', '') in ('', '0.0.0.0', '127.0.0.1')
                           for group in bindings for b in (group or [])):
                    raise RuntimeError('Container does not publish the configured port on local IPv4')
            if (info or {}).get('Id') != (self._cached or {}).get('Id'):
                self._live_metadata = {}
            self._cached, self._checked = info, time.monotonic()
            return info

    @staticmethod
    def _env(info):
        return dict(x.split('=', 1) for x in (info or {}).get('Config', {}).get('Env', []) if '=' in x)

    def _run_config(self, profile=None):
        env = self._env(self._inspect())
        context = str(env.get('CONTEXT') or '32768').lower()
        try:
            count = int(context[:-1]) * 1024 if context.endswith('k') else int(context)
        except ValueError:
            count = 32768
        count = self._live_metadata.get('max_context') or count
        return {'model_name': self._live_metadata.get('model') or self.cfg.get('model_name') or env.get('MODEL') or self.profile_id,
                'api_key': self.api_key or env.get('API_KEY', ''),
                'args': ['--max-context', str(count)],
                'vision': env.get('VISION', 'off').lower() not in ('off', 'false', '0', '')}

    def auth_headers(self):
        key = self._run_config().get('api_key', '').split(',')[0].strip()
        return {'Authorization': f'Bearer {key}'} if key else {}

    def health(self, profile):
        try:
            response = httpx.get(self.base_url() + '/health', headers=self.auth_headers(), timeout=5)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return False, 'unexpected health response'
            loaded = bool(data.get('loaded', False))
            if loaded:
                self._live_metadata = data
            return loaded, 'ready' if loaded else 'server up, model loading'
        except (httpx.HTTPError, ValueError) as exc:
            return False, f'not ready ({type(exc).__name__})'

    def discover_profiles(self):
        # Metadata contains no Docker environment or keys: profile settings are public in the UI.
        defaults = dict(self.cfg.get('profile_defaults') or {})
        return {self.profile_id: Profile(self.profile_id, self.name, defaults,
                description=self.cfg.get('description', 'Strata (Docker Compose)'), discovered=True,
                policy=defaults.get('policy', 'exclusive'))}

    def make_profile(self, pid, pcfg):
        if pid != self.profile_id:
            raise ValueError('A Docker Strata service has one profile; configure another service for another model')
        return Engine.make_profile(self, pid, pcfg)

    def launch(self, profile):
        raise ValueError('Docker Strata uses its Compose service, not a host launcher')

    def external_status(self):
        info = self._inspect(fresh=True)
        state = (info or {}).get('State', {})
        if not state.get('Running'):
            return None
        started = state['StartedAt']
        return {'identity': info['Id'] + ':' + started, 'profile': self.profile_id,
                'pid': state['Pid'],
                'created': datetime.datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp()}

    def validate_launch(self, profile):
        if profile.id != self.profile_id or any(profile.cfg.get(k) for k in ('ctx', 'args', 'server_args', 'env')):
            raise ValueError('Docker launch settings belong in Compose; custom launch overrides are unsupported')
        self._inspect(fresh=True)

    def start_external(self, profile):
        self.validate_launch(profile)
        info = self._inspect(fresh=True)
        if info:
            if not info['State'].get('Running'):
                self._docker('start', info['Id'], timeout=60)
        else:
            self._docker('compose', '--project-directory', self.project_dir, '-f', self.compose_file,
                         'up', '-d', '--no-deps', '--no-recreate', '--pull', 'never', self.service, timeout=120)
        result = self.external_status()
        if result is None:
            raise RuntimeError('Docker service did not enter a running state')
        return result

    def stop_external(self, identity, log):
        current = self.external_status()
        if current is None:
            return
        if current['identity'] != identity:
            raise RuntimeError('Container changed since adoption; refusing to stop the replacement')
        container_id = identity.split(':', 1)[0]
        log(f'Stopping Docker container {self.container} (grace {self.stop_timeout_s}s)')
        self._docker('stop', '--time', self.stop_timeout_s, container_id, timeout=self.stop_timeout_s + 20)
        self._checked = 0
        if self.external_status() is not None:
            raise RuntimeError('Container is still running after Docker stop')

    def extra_logs(self, profile):
        return []

    def estimate(self, profile, vram_total_mib, ram_total_mib):
        if profile.cfg.get('requires'):
            return Engine.estimate(self, profile, vram_total_mib, ram_total_mib)
        return Estimate(vram_total_mib, ram_total_mib * 0.85, True,
                        'Docker Strata uses the configured GPUs and may stream model data from RAM')

    def has_vision(self, profile):
        return bool(self._run_config(profile)['vision'])

    def version(self):
        info = self._inspect()
        return (info or {}).get('Config', {}).get('Image')

    def profile_identity(self, profile):
        """Invalidate learned memory estimates when Compose changes the actual launch settings."""
        info = self._inspect() or {}
        env = self._env(info)
        return {'image': info.get('Image') or info.get('Config', {}).get('Image'),
                'launch': {k: env.get(k) for k in
                           ('FAMILY', 'MODEL', 'CONTEXT', 'PARALLEL', 'GPU_ROLES', 'VISION')}}

    def public(self):
        return {**super().public(), 'deployment': 'docker', 'container': self.container}

    def external_usage(self):
        info = self._inspect()
        if not info or not info['State'].get('Running'):
            return {'ram_mib': 0, 'cpu_percent': 0, 'pids': []}
        raw = self._docker('stats', '--no-stream', '--format', '{{json .}}', info['Id'])
        data = json.loads(raw)
        memory = data['MemUsage'].split('/', 1)[0].strip()
        match = re.fullmatch(r'([0-9.]+)\s*([kKMGT]?i?B)', memory)
        scales = {'B': 1 / 2**20, 'KiB': 1 / 1024, 'MiB': 1, 'GiB': 1024, 'TiB': 1024**2,
                  'kB': 1000 / 2**20, 'MB': 1000**2 / 2**20, 'GB': 1000**3 / 2**20}
        ram = float(match[1]) * scales[match[2]] if match and match[2] in scales else 0
        top = self._docker('top', info['Id'], '-eo', 'pid')
        pids = [int(line.strip()) for line in top.splitlines() if line.strip().isdigit()]
        return {'ram_mib': ram, 'cpu_percent': float(data['CPUPerc'].rstrip('%')), 'pids': pids}

    def sync_log(self, path):
        """Mirror bounded timestamped Docker logs into the normal log reader, including after adoption."""
        with self._lock:
            if time.monotonic() - self._log_checked < 2:
                return
            info = self._inspect()
            if not info:
                return
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            identity_file = path.with_suffix('.docker-id')
            old_id = identity_file.read_text().strip() if identity_file.exists() else None
            if old_id != info['Id']:
                if path.exists():
                    path.replace(path.with_suffix('.old.log'))
                identity_file.write_text(info['Id'])
            last = ''
            if path.exists():
                with path.open('rb') as f:
                    f.seek(max(0, path.stat().st_size - 256 * 1024))
                    last = f.read().decode('utf-8', 'replace')
            history = last.splitlines()
            stamp = history[-1].split(' ', 1)[0] if history else None
            same = sum(line.split(' ', 1)[0] == stamp for line in history) if stamp else 0
            args = ['logs', '--timestamps', '--tail', '2000']
            if stamp:
                args += ['--since', stamp]
            # Docker writes container stderr to CLI stderr; combine only after checking the exit status.
            p = subprocess.run(['docker', *args, info['Id']], capture_output=True, text=True, timeout=10)
            if p.returncode:
                raise RuntimeError(f'Docker logs failed: {p.stderr.strip()[:200]}')
            incoming = sorted((p.stdout + p.stderr).splitlines(), key=lambda line: line.split(' ', 1)[0])
            fresh = []
            for line in incoming:
                if stamp and line.split(' ', 1)[0] == stamp and same:
                    same -= 1
                else:
                    fresh.append(line)
            if fresh:
                if path.exists() and path.stat().st_size > 50 * 2**20:
                    path.replace(path.with_suffix('.old.log'))
                with path.open('a') as out:
                    out.write('\n'.join(fresh) + '\n')
            self._log_checked = time.monotonic()
