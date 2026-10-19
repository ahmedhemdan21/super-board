#!/usr/bin/env python3
"""Hub configs (hub.md → Config): validation, the member resolver, the cache, the run gate."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / 'scripts/super-board-config.py'
READY = ROOT / 'scripts/super-board-verification-ready.py'


def hub_config():
    return {
        'project': {'owner': 'acme', 'title': 'Acme', 'number': 3},
        'hub': {'remote': 'https://github.com/acme/docs', 'path': '.'},
        'members': {
            'backend': {'repo': {'path': 'backend', 'remote': 'https://github.com/acme/api.git'},
                        'base_branch': 'dev', 'verify_commands': ['make test'],
                        'collect': {'sources': ['loki'], 'loki': {'url': 'http://loki'}},
                        'linked_repos': ['acme/infra']},
            'frontend': {'repo': {'path': 'frontend', 'remote': 'https://github.com/acme/web'},
                         'base_branch': 'main'},
        },
        'split': {'default_order': ['backend', 'frontend'], 'confirm': True},
        'collect': {'sources': ['slack', 'github'], 'window_days': 14},
        'verify_commands': ['npm test'],
        'max_workers': 6,
    }


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # The documented layout: <root>/.claude/super-board/configs/<slug>.json
        self.root = Path(self.temp.name).resolve()
        self.configs = self.root / '.claude/super-board/configs'
        self.configs.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, cfg, slug='hub'):
        path = self.configs / f'{slug}.json'
        path.write_text(json.dumps(cfg), encoding='utf-8')
        return path

    def run_resolver(self, *args):
        return subprocess.run(['python3', str(RESOLVER), *args], text=True, capture_output=True)

    def errors(self, cfg):
        result = self.run_resolver('validate', '--config', str(self.write(cfg)))
        return result.returncode, result.stderr

    def test_valid_hub_and_single_repo_configs_pass(self):
        self.assertEqual(self.errors(hub_config()), (0, ''))
        single = {'project': {'owner': 'acme', 'number': 1},
                  'repo': {'path': '.', 'remote': 'https://github.com/acme/api'}}
        self.assertEqual(self.errors(single), (0, ''))
        one = hub_config()
        del one['members']['frontend']
        one['split']['default_order'] = ['backend']
        self.assertEqual(self.errors(one)[0], 0, 'one member is a valid hub (onboard one repo at a time)')

    def test_bad_configs_are_refused_with_the_reason(self):
        cases = {
            'top-level `repo`': lambda c: c.update(repo={'path': '.', 'remote': 'https://github.com/acme/x'}),
            'member key': lambda c: c['members'].update({'Back_End': c['members'].pop('backend')}),
            'remotes must be unique': lambda c: c['members']['frontend']['repo'].update(remote='https://github.com/ACME/api'),
            'is the hub repo': lambda c: c['members']['frontend']['repo'].update(remote='acme/docs'),
            'base_branch is required': lambda c: c['members']['frontend'].pop('base_branch'),
            'repo.path': lambda c: c['members']['frontend']['repo'].pop('path'),
            'repo.remote': lambda c: c['members']['frontend']['repo'].update(remote='not a repo'),
            'hub.remote': lambda c: c.pop('hub'),
            'board-wide key': lambda c: c['members']['frontend'].update(max_workers=2),
            'default_order': lambda c: c['split'].update(default_order=['backend', 'mobile']),
            'at least one member': lambda c: c.update(members={}),
        }
        for why, mutate in cases.items():
            cfg = hub_config()
            mutate(cfg)
            rc, err = self.errors(cfg)
            self.assertEqual(rc, 65, why)
            self.assertIn(why, err)
        rc, err = self.errors({'project': {}, 'repo': {'path': '.'}, 'split': {}})
        self.assertEqual(rc, 65)
        self.assertIn('`split` needs `members`', err)

    def test_member_merges_board_and_member_keys(self):
        path = self.write(hub_config())
        result = self.run_resolver('member', 'backend', '--config', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = json.loads(result.stdout)
        self.assertEqual(cfg['member'], 'backend')
        self.assertEqual(cfg['hub_config'], str(path.resolve()))
        self.assertEqual(cfg['project'], hub_config()['project'], 'board-wide keys kept')
        self.assertEqual(cfg['max_workers'], 6)
        self.assertEqual(cfg['base_branch'], 'dev')
        self.assertEqual(cfg['verify_commands'], ['make test'], 'member wins')
        self.assertEqual(cfg['collect'], {'sources': ['loki'], 'window_days': 14, 'loki': {'url': 'http://loki'}},
                         'collect merges one level')
        self.assertEqual(cfg['repo']['remote'], 'https://github.com/acme/api.git')
        self.assertEqual(Path(cfg['repo']['path']), self.root / 'backend')
        self.assertTrue(Path(cfg['repo']['path']).is_absolute())
        self.assertEqual(cfg['linked_repos'], ['acme/infra', 'acme/web'], 'every other member is linked')
        for key in ('members', 'hub', 'split'):
            self.assertNotIn(key, cfg)
        front = json.loads(self.run_resolver('member', 'frontend', '--config', str(path)).stdout)
        self.assertEqual(front['verify_commands'], ['npm test'], 'board value when the member sets none')
        self.assertEqual(front['linked_repos'], ['acme/api'])
        self.assertEqual(self.run_resolver('member', 'mobile', '--config', str(path)).returncode, 65)

    def test_resolved_member_passes_single_repo_gates(self):
        path = self.write(hub_config())
        out = self.root / 'backend.json'
        self.assertEqual(self.run_resolver('member', 'backend', '--config', str(path), '--out', str(out)).returncode, 0)
        self.assertEqual(self.run_resolver('validate', '--config', str(out)).returncode, 0)
        ready = subprocess.run(['python3', str(READY), '--config', str(out)], capture_output=True, text=True)
        self.assertEqual(ready.returncode, 0, ready.stderr)

    def test_verification_ready_checks_every_member(self):
        cfg = hub_config()
        self.assertEqual(subprocess.run(['python3', str(READY), '--config', str(self.write(cfg))]).returncode, 0)
        cfg['members']['frontend']['verify_commands'] = []
        ready = subprocess.run(['python3', str(READY), '--config', str(self.write(cfg))], capture_output=True, text=True)
        self.assertEqual(ready.returncode, 9)
        self.assertIn('members.frontend', ready.stderr)

    def test_members_lists_single_and_hub_configs_uniformly(self):
        hub = json.loads(self.run_resolver('members', '--config', str(self.write(hub_config()))).stdout)
        self.assertEqual(hub, [{'key': 'backend', 'repo': 'acme/api', 'path': str(self.root / 'backend')},
                               {'key': 'frontend', 'repo': 'acme/web', 'path': str(self.root / 'frontend')}])
        single = {'project': {'owner': 'acme', 'number': 1},
                  'repo': {'path': '.', 'remote': 'git@github.com:acme/api.git'}}
        one = json.loads(self.run_resolver('members', '--config', str(self.write(single, 'one'))).stdout)
        self.assertEqual(one, [{'key': '', 'repo': 'acme/api', 'path': str(self.root)}])

    def test_for_repo(self):
        path = str(self.write(hub_config()))
        hit = self.run_resolver('for-repo', 'ACME/Web', '--config', path)
        self.assertEqual((hit.returncode, hit.stdout.strip()), (0, 'frontend'))
        self.assertEqual(self.run_resolver('for-repo', 'acme/docs', '--config', path).returncode, 3)
        self.assertEqual(self.run_resolver('for-repo', 'acme/other', '--config', path).returncode, 3)

    def test_resolved_cache_regenerates_when_the_source_is_newer(self):
        path = self.write(hub_config())
        first = self.run_resolver('member', 'backend', '--config', str(path), '--path')
        self.assertEqual(first.returncode, 0, first.stderr)
        cached = Path(first.stdout.strip())
        self.assertEqual(cached, self.root / '.claude/super-board/resolved/hub.backend.json')
        self.assertEqual((cached.parent / '.gitignore').read_text(), '*\n')
        self.assertEqual(json.loads(cached.read_text())['base_branch'], 'dev')
        # Unchanged source: the cache is reused as is.
        cached.write_text(json.dumps({'marker': True}))
        old = time.time() - 60
        os.utime(path, (old, old))
        self.run_resolver('member', 'backend', '--config', str(path), '--path')
        self.assertEqual(json.loads(cached.read_text()), {'marker': True})
        # A newer source regenerates it.
        cfg = hub_config()
        cfg['members']['backend']['base_branch'] = 'staging'
        self.write(cfg)
        os.utime(path, (time.time() + 60, time.time() + 60))
        self.run_resolver('member', 'backend', '--config', str(path), '--path')
        self.assertEqual(json.loads(cached.read_text())['base_branch'], 'staging')

    def test_run_gate_refuses_a_members_config_only(self):
        refused = self.run_resolver('run-gate', '--config', str(self.write(hub_config())))
        self.assertEqual(refused.returncode, 78)
        self.assertIn('P4', refused.stderr)
        single = {'project': {'owner': 'acme', 'number': 1}, 'repo': {'path': '.', 'remote': 'acme/api'}}
        self.assertEqual(self.run_resolver('run-gate', '--config', str(self.write(single, 'one'))).returncode, 0)


if __name__ == '__main__':
    unittest.main()
