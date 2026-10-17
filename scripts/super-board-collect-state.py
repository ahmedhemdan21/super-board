#!/usr/bin/env python3
"""Same-host collector serialization and private evidence receipts; no scheduler.

The lock is inherited by the real shell filer and its synchronous children.
On Windows, which has no exec, this process holds it while the filer runs as a child.
A pending write survives interruption and blocks retry until explicitly reconciled.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

spec = importlib.util.spec_from_file_location('github_read', Path(__file__).with_name('super-board-github-read.py'))
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)


def paths(repo):
    root = Path(os.environ.get('SB_COLLECT_STATE_DIR') or gh.halt_path().parent / 'collect')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ignore = root / '.gitignore'
    if not ignore.exists():
        ignore.write_text('*\n', encoding='utf-8')
    key = hashlib.sha256(repo.lower().encode()).hexdigest()
    return key, root / (key + '.lock'), root / (key + '.json')


def try_lock(fd):
    """Take the exclusive lock without waiting; False while another process holds it."""
    if os.name == 'nt':
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def held_by_parent(lock_path):
    """Windows: the --lock process wrote our token and still holds the lock."""
    holder = lock_path.with_suffix('.holder')
    token = os.environ.get('SB_COLLECT_LOCK_TOKEN')
    if not token or not holder.exists() or holder.read_text(encoding='utf-8') != token:
        return False
    fd = os.open(lock_path, os.O_RDWR)
    try:
        if try_lock(fd):  # nobody holds it: the locker is gone
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        return True
    finally:
        os.close(fd)


def git_bash():
    """Git Bash, not System32's WSL bash.exe that Windows finds first."""
    override = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')
    if override and os.path.isfile(override):
        return override
    git = shutil.which('git')
    if git:
        up = os.path.dirname(os.path.dirname(git))
        for top in (up, os.path.dirname(up)):
            cand = os.path.join(top, 'bin', 'bash.exe')
            if os.path.isfile(cand):
                return cand
    return 'bash'


def run_holding(key, lock_path, args):
    """Windows: an exiting process drops its locks, so stay alive while the filer runs."""
    token = secrets.token_hex(16)
    holder = lock_path.with_suffix('.holder')
    holder.write_text(token, encoding='utf-8')
    if args[0].endswith('.sh'):
        args = [git_bash(), *args]
    env = dict(os.environ, SB_COLLECT_LOCK_HELD=key, SB_COLLECT_LOCK_TOKEN=token)
    try:
        return subprocess.run(args, env=env).returncode
    finally:
        holder.unlink(missing_ok=True)


def load(path):
    if not path.exists():
        return {'receipts': {}}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('receipts'), dict):
        raise ValueError('collector state is malformed; inspect it before retrying')
    return value


def save(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', required=True)
    p.add_argument('--lock', action='store_true')
    p.add_argument('--action', choices=['seen','begin','success','known-failure','reconcile'])
    p.add_argument('--fingerprint', default='')
    p.add_argument('--digest', default='')
    p.add_argument('--issue', default='')
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args()
    key, lock_path, state_path = paths(a.repo)
    args = a.command[1:] if a.command[:1] == ['--'] else a.command
    try:
        if a.lock:
            limit = float(os.environ.get('SB_COLLECT_LOCK_TIMEOUT', '15'))
            if not math.isfinite(limit) or not 0 <= limit <= 300:
                raise ValueError('SB_COLLECT_LOCK_TIMEOUT must be between 0 and 300 seconds')
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            deadline = time.monotonic() + limit
            while True:
                gh.check_halt()
                if try_lock(fd):
                    break
                if time.monotonic() >= deadline:
                    print('collector busy; no writes attempted', file=sys.stderr)
                    return 4
                time.sleep(.05)
            state = load(state_path)
            if state.get('pending'):
                gh.halt('Collector write outcome unresolved; reconcile pending receipt before retrying', 1)
            if not args:
                raise ValueError('--lock needs the existing filer command')
            if os.name == 'nt':
                return run_holding(key, lock_path, args)
            os.set_inheritable(fd, True)
            env = dict(os.environ, SB_COLLECT_LOCK_HELD=key, SB_COLLECT_LOCK_FD=str(fd))
            os.execvpe(args[0], args, env)
        elif a.action:
            fd = int(os.environ.get('SB_COLLECT_LOCK_FD', '-1'))
            held = os.environ.get('SB_COLLECT_LOCK_HELD') == key
            if held:
                if os.name == 'nt':
                    held = held_by_parent(lock_path)
                else:
                    held = os.fstat(fd).st_ino == lock_path.stat().st_ino
            # Explicit manual reconciliation acquires the same lock; never clears a live writer.
            stream = None
            if a.action == 'reconcile' and not held:
                stream = lock_path.open('a+', encoding='utf-8')
                if not try_lock(stream.fileno()):
                    return 4
                held = True
            if not held:
                raise ValueError('collector receipt operations require the held repository lock')
            state = load(state_path)
            if a.action == 'seen':
                receipt = state['receipts'].get(a.fingerprint, {})
                print('unchanged' if receipt.get('digest') == a.digest and receipt.get('issue') == a.issue else 'changed')
            elif a.action == 'begin':
                state['pending'] = {'fingerprint': a.fingerprint, 'digest': a.digest, 'issue': a.issue, 'at': int(time.time())}
                save(state_path, state)
            elif a.action == 'success':
                if not a.issue.isdigit():
                    raise ValueError('successful receipt requires an issue number')
                state['receipts'][a.fingerprint] = {'digest': a.digest, 'issue': a.issue, 'at': int(time.time())}
                state.pop('pending', None)
                save(state_path, state)
            elif a.action in ('known-failure','reconcile'):
                if state.get('pending', {}).get('fingerprint') != a.fingerprint:
                    raise ValueError('pending fingerprint differs; inspect state before resolving')
                state.pop('pending', None)
                save(state_path, state)
            return 0
        raise ValueError('choose --lock or --action')
    except gh.ReadHalted as exc:
        print(str(exc), file=sys.stderr)
        return 79
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 70

if __name__ == '__main__':
    # Windows pipes default to the ANSI code page; super-board text is UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    sys.exit(main())
