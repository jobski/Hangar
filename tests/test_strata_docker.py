"""Docker adoption and lifecycle must never signal a proxy or an unrelated replacement container."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from manager import config
from manager.core import Job, Manager
from manager.engines import create
from manager.engines.base import Profile


def inspection():
    return {'Id': 'a' * 64, 'State': {'Running': True, 'Pid': 12345,
            'StartedAt': '2026-11-05T15:00:00.123456789Z'},
            'Config': {'Image': 'strata:test', 'Env': ['MODEL=IQ3_S', 'API_KEY=private-key', 'CONTEXT=32k'],
                       'Labels': {'com.docker.compose.service': 'strata',
                                  'com.docker.compose.project.config_files': '/srv/strata/compose.yaml'}},
            'HostConfig': {'PortBindings': {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8080'}]}}}


class DockerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.info = inspection()
        self.commands = []
        self.spec = {'plugin': 'strata', 'port': 8080, 'docker': {
            'container': 'strata', 'service': 'strata', 'compose_file': '/srv/strata/compose.yaml'}}
        self.engine = create('strata', self.spec)
        self.engine._docker = self.docker

    def docker(self, *args, **kwargs):
        self.commands.append(args)
        if args[:2] == ('container', 'inspect'):
            return json.dumps([self.info]) if self.info else None
        if args[0] == 'start':
            self.info['State']['Running'] = True
        if args[0] == 'stop':
            self.info['State']['Running'] = False
        if args[0] == 'compose':
            self.info = inspection()
        return ''

    def manager(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        root = Path(self.tmp.name)
        cfg['manager'].update(data_dir=root, logs_dir=root)
        cfg['models']['dir'] = str(root / 'models')
        mgr = Manager(cfg)
        mgr.engines = {'strata': self.engine}
        mgr.refresh_profiles(force=True)
        self.addCleanup(mgr.store.db.close)
        return mgr

    def test_adopts_without_port_owner_and_tracks_container_restart(self):
        mgr = self.manager()
        with patch.object(self.engine, 'health', return_value=(True, 'ready')), \
             patch.object(self.engine, 'runtime_context', return_value=32768), \
             patch('manager.core.resources.port_owner', side_effect=AssertionError('must not inspect proxy')):
            mgr._discover_external()
        inst = mgr.instances['strata']
        self.assertEqual(inst.profile, 'strata-docker')
        self.assertEqual(inst.context, 32768)
        self.assertEqual(inst.state, 'ready')
        self.assertTrue(mgr._alive(inst))
        self.assertFalse(inst.ours)
        self.assertTrue(inst.persist()['runtime_id'].startswith('a' * 64))
        self.info['State']['StartedAt'] = '2026-11-05T16:00:00Z'
        self.assertFalse(mgr._alive(inst))

    def test_manager_stop_uses_docker_and_never_host_signals(self):
        mgr = self.manager()
        runtime = self.engine.external_status()
        mgr._external_instance(self.engine, runtime)
        with patch('manager.core._kill_tree', side_effect=AssertionError('must not kill host PID')):
            mgr._stop(None, 'strata', 'test')
        self.assertNotIn('strata', mgr.instances)
        self.assertTrue(any(c[0] == 'stop' and c[-1] == 'a' * 64 for c in self.commands))

    def test_saved_instance_is_recovered_without_host_process_access(self):
        mgr = self.manager()
        inst = mgr._external_instance(self.engine, self.engine.external_status())
        mgr._save_state()
        restored = self.manager()
        with patch('manager.core.psutil.Process', side_effect=AssertionError('host PID is not an identity')), \
             patch.object(self.engine, 'health', return_value=(True, 'ready')):
            restored.adopt_saved()
        self.assertEqual(restored.instances['strata'].runtime_id, inst.runtime_id)
        self.assertEqual(restored.instances['strata'].session, inst.session)

    def test_replacement_is_not_stopped_even_if_name_is_reused(self):
        mgr = self.manager()
        inst = mgr._external_instance(self.engine, self.engine.external_status())
        self.info['Id'] = 'b' * 64
        with self.assertRaisesRegex(RuntimeError, 'replacement'):
            mgr._stop(None, 'strata', 'test')
        self.assertFalse(any(c[0] == 'stop' for c in self.commands))
        self.assertIn('strata', mgr.instances)
        self.assertNotEqual(inst.state, 'stopping')

    def test_existing_container_start_does_not_recreate_or_pull(self):
        self.info['State']['Running'] = False
        self.engine.start_external(self.engine.discover_profiles()['strata-docker'])
        self.assertIn(('start', 'a' * 64), self.commands)
        self.assertFalse(any(c[0] == 'compose' for c in self.commands))

    def test_missing_container_uses_only_configured_compose_service(self):
        self.info = None
        self.engine.start_external(self.engine.discover_profiles()['strata-docker'])
        command = next(c for c in self.commands if c[0] == 'compose')
        self.assertIn('--no-recreate', command)
        self.assertIn('--no-deps', command)
        self.assertEqual(command[-3:], ('--pull', 'never', 'strata'))

    def test_wrong_compose_service_is_refused_before_lifecycle_changes(self):
        self.info['Config']['Labels']['com.docker.compose.service'] = 'other'
        with self.assertRaisesRegex(RuntimeError, 'does not belong'):
            self.engine.start_external(self.engine.discover_profiles()['strata-docker'])
        self.assertFalse(any(c[0] in ('start', 'stop', 'compose') for c in self.commands))

    def test_launch_overrides_are_rejected_before_stopping_model(self):
        mgr = self.manager()
        p = Profile('strata-docker', 'strata', {'ctx': 16384})
        with patch.object(mgr, '_stop') as stop:
            with self.assertRaisesRegex(ValueError, 'Compose'):
                mgr._start(Job('start', 'test', None), p, False, 'skip')
            stop.assert_not_called()

    def test_logs_continue_without_duplicates_across_manager_restart(self):
        path = Path(self.tmp.name) / 'strata.log'
        first = '2026-11-05T15:00:00.000000001Z ready\n'
        second = '2026-11-05T15:00:01.000000001Z generated\n'
        with patch('manager.engines.strata_docker.subprocess.run', return_value=SimpleNamespace(
                returncode=0, stdout=first, stderr='')):
            self.engine.sync_log(path)
        # A new manager/plugin object must resume from the on-disk log timestamp.
        replacement = create('strata', self.spec)
        replacement._docker = self.docker
        with patch('manager.engines.strata_docker.subprocess.run', return_value=SimpleNamespace(
                returncode=0, stdout=first, stderr=second)):
            replacement.sync_log(path)
        self.assertEqual(path.read_text(), first + second)

    def test_authentication_is_not_exposed_in_profile_or_deployment_metadata(self):
        headers = self.engine.auth_headers()
        self.assertEqual(headers, {'Authorization': 'Bearer private-key'})
        public = json.dumps(self.engine.discover_profiles()['strata-docker'].public())
        public += json.dumps(self.engine.public())
        self.assertNotIn('private-key', public)
        self.assertEqual(self.engine._run_config()['args'], ['--max-context', '32768'])

    def test_resource_usage_combines_docker_stats_and_host_pids(self):
        docker = self.docker

        def outputs(*args, **kwargs):
            if args[0] == 'stats':
                return json.dumps({'MemUsage': '53.64GiB / 251.6GiB', 'CPUPerc': '10.05%'})
            if args[0] == 'top':
                return 'PID\n  12345\n  12346\n'
            return docker(*args, **kwargs)

        self.engine._docker = outputs
        usage = self.engine.external_usage()
        self.assertAlmostEqual(usage['ram_mib'], 53.64 * 1024)
        self.assertEqual(usage['cpu_percent'], 10.05)
        self.assertEqual(usage['pids'], [12345, 12346])

    def test_compose_context_change_invalidates_learned_memory_key(self):
        mgr = self.manager()
        profile = mgr.profile('strata-docker')
        old_key = mgr._profile_key(profile)
        self.info['Config']['Env'].append('CONTEXT=256k')
        self.engine._checked = 0
        self.assertNotEqual(mgr._profile_key(profile), old_key)


if __name__ == '__main__':
    unittest.main()
