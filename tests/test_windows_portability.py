#!/usr/bin/env python3
"""Runs on every OS; guards what broke super-board on Windows: UTF-8 I/O, the collector lock, Git Bash."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'scripts/super-board-collect-state.py'
CHILD = '''import os,subprocess,sys,time
state=[sys.executable,os.environ['STATE'],'--repo','acme/app']
for action,issue in (('seen','1'),('begin','1'),('success','7')):
    # Like the shell filer, keep the inherited lock descriptor open for children.
    r=subprocess.run(state+['--action',action,'--fingerprint','f','--digest','d','--issue',issue],capture_output=True,text=True,close_fds=False)
    print(action,r.returncode,r.stdout.strip(),r.stderr.strip(),flush=True)
time.sleep(float(os.environ.get('HOLD','0')))
sys.exit(5)
'''


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Utf8(unittest.TestCase):
    def test_setup_help_survives_a_pipe(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-setup.py'), '--help'],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        self.assertIn('→', result.stdout.decode('utf-8'))

    def test_required_read_decodes_gh_output_as_utf8(self):
        # 0x90 in the bug emoji has no cp1252 mapping: an ANSI decode loses the whole read.
        gh = load('github_read', ROOT / 'scripts/super-board-github-read.py')
        real = subprocess.run
        fake = [sys.executable, '-c', "import sys; sys.stdout.buffer.write('🐛 [fix] x'.encode())"]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, SB_GITHUB_HALT_FILE=str(Path(tmp) / 'halt.json'), SB_GITHUB_RETRY_DELAY='0'), \
                patch.object(gh.subprocess, 'run', lambda cmd, **kw: real(fake, **kw)):
            _, value = gh.read(['api', 'repos/acme/app/commits/main', '--jq', '.commit.message'], kind='scalar')
        self.assertEqual(value, '🐛 [fix] x')


class CollectorLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'child.py').write_text(CHILD, encoding='utf-8')
        self.child = [sys.executable, str(root / 'child.py')]
        self.state = root / 'state'
        self.env = dict(os.environ, STATE=str(STATE), SB_COLLECT_STATE_DIR=str(self.state),
                        SB_GITHUB_HALT_FILE=str(root / 'halt.json'))
        for name in ('SB_COLLECT_LOCK_HELD', 'SB_COLLECT_LOCK_FD', 'SB_COLLECT_LOCK_TOKEN'):
            self.env.pop(name, None)

    def tearDown(self):
        self.tmp.cleanup()

    def lock(self, **env):
        return subprocess.run([sys.executable, str(STATE), '--repo', 'acme/app', '--lock', '--', *self.child],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def test_filer_writes_receipts_while_holding_the_lock(self):
        result = self.lock()
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual([line.split()[:2] for line in result.stdout.splitlines()],
                         [['seen', '0'], ['begin', '0'], ['success', '0']])
        receipt = json.loads(next(self.state.glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(receipt['receipts']['f']['issue'], '7')
        self.assertEqual(list(self.state.glob('*.holder')), [])

    def test_second_writer_waits_then_reports_busy(self):
        holder = subprocess.Popen([sys.executable, str(STATE), '--repo', 'acme/app', '--lock', '--', *self.child],
                                  env=dict(self.env, HOLD='3'), stdout=subprocess.PIPE, text=True)
        try:
            self.assertTrue(holder.stdout.readline().startswith('seen 0'))
            busy = self.lock(SB_COLLECT_LOCK_TIMEOUT='.2')
            self.assertEqual(busy.returncode, 4, busy.stderr)
            self.assertIn('collector busy', busy.stderr)
        finally:
            holder.communicate(timeout=30)
        self.assertEqual(holder.returncode, 5)

    def test_receipt_action_without_the_lock_is_refused(self):
        self.state.mkdir()
        key = hashlib.sha256(b'acme/app').hexdigest()
        (self.state / (key + '.holder')).write_text('stale', encoding='utf-8')  # crashed Windows locker
        result = subprocess.run([sys.executable, str(STATE), '--repo', 'acme/app', '--action', 'seen',
                                 '--fingerprint', 'f', '--digest', 'd', '--issue', '7'],
                                env=dict(self.env, SB_COLLECT_LOCK_HELD=key, SB_COLLECT_LOCK_TOKEN='stale'),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 70, result.stderr)


@unittest.skipUnless(os.name == 'nt', 'Windows only')
class GitBash(unittest.TestCase):
    def test_setup_runs_scripts_with_git_bash_not_wsl(self):
        setup = load('board_setup', ROOT / 'scripts/super-board-setup.py')
        bash = setup.bash()
        self.assertNotRegex(bash.lower(), r'system32|windowsapps')
        result = subprocess.run([bash, '-c', 'echo $OSTYPE'], capture_output=True, text=True, timeout=60)
        self.assertIn('msys', result.stdout, result.stderr)


if __name__ == '__main__':
    unittest.main()
