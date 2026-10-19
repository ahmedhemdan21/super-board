#!/usr/bin/env python3
"""Bounded, read-only GitHub evidence. Exit 79 stops this repository's board run.

A logical read gets three total attempts. Success resets its own sequence; other
reads cannot reset it. Permanent query/auth errors stop on the first attempt.
The halt file is shared through the main checkout, including linked worktrees.
Only an explicit new run clears it with --resume after preserving the old reason.
`--check --config C` checks a whole board: the hub checkout and every member's.
Mutations never enter this retry helper. For the agent-facing kinds in FORMS, a
payload that fails its shape on every attempt is the caller's malformed read, not
an outage: that call exits 64 with the valid form and the run keeps going. Scripts
whose commands are fixed set SB_GITHUB_READ_STRICT=1 to keep halting on it.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

HALTED = 79


class ReadHalted(Exception):
    pass


class GitHubErrors(ValueError):
    """GitHub's own error payload: an outage, never the caller's shape."""


# Valid read per --kind, quoted back when a payload fails its shape.
FORMS = {
    'issue': 'issue view <N> --json number,title,body',
    'comments': 'api repos/{owner}/{repo}/issues/<N>/comments --paginate --slurp',
    'json': '<gh args> (JSON output; no --jq that prints a plain string)',
    'head': 'pr view <N> --json headRefName,headRefOid --jq \'"\\(.headRefName) \\(.headRefOid)"\'',
    'body': 'pr view <N> --json body,headRefOid',
    'metadata': 'pr view <N> --json files,labels,body,additions,deletions,changedFiles',
    'state': 'issue view <N> --json state --jq .state',
    'prs-open': 'pr list --state open --limit 100 --json number,title,body,url,headRefName,files',
    'prs-merged': 'pr list --state merged --limit 100 --json number,title,body,url',
    'issues-closed': 'issue list --state closed --limit 100 --json number,title,body,stateReason',
}


def command(name, args=()):
    """argv running a program on PATH. Windows only execs name.exe: this also finds
    npm's .cmd shims and runs an extensionless #! script (a shim or test stub) with
    this Python or with Git Bash. Git Bash would glob and unquote a plain Windows
    command line ({owner} -> owner), so its script gets the args shell-quoted in -c."""
    args = list(args)
    if os.name != 'nt':
        return [name, *args]
    if name == 'bash':
        return [git_bash(), *args]
    exts = [e for e in os.environ.get('PATHEXT', '.EXE').split(os.pathsep) if e]
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        for ext in exts:
            if os.path.isfile(os.path.join(folder, name + ext)):
                return [os.path.join(folder, name + ext), *args]
        script = os.path.join(folder, name)
        if os.path.isfile(script):
            with open(script, 'rb') as stream:
                shebang = stream.readline()
            if b'python' in shebang:
                return [sys.executable, script, *args]
            return [git_bash(), '-c', 'exec "$0" ' + shlex.join(args), script]
    return [name, *args]


def git_bash():
    """Git Bash, not System32's WSL bash.exe that Windows would find first."""
    override = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')
    if override and os.path.isfile(override):
        return override
    git = shutil.which('git')
    if git:
        up = os.path.dirname(os.path.dirname(git))  # <Git>\cmd\git.exe -> <Git>
        for top in (up, os.path.dirname(up)):
            if os.path.isfile(os.path.join(top, 'bin', 'bash.exe')):
                return os.path.join(top, 'bin', 'bash.exe')
    return 'bash'


def halt_path():
    override = os.environ.get('SB_GITHUB_HALT_FILE')
    if override:
        return Path(override)
    cwd = Path(os.environ.get('SB_REPO_PATH') or Path.cwd())
    return board_root(cwd) / '.claude/super-board/github-halt.json'


def board_root(cwd):
    """The checkout that owns .claude/super-board. A linked worktree shares its main
    checkout's (common dir <main>/.git). A submodule's common dir is
    <super>/.git/modules/<name>, whose parent is no checkout: it keeps its own, in the
    submodule checkout (core.worktree of that dir, else the toplevel), so two submodules
    never share one halt file. Same rule as the scheduler's board_root()."""
    def git(*args):
        try:
            r = subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8',
                               timeout=5, cwd=str(cwd))
        except (OSError, subprocess.TimeoutExpired):
            return ''
        return r.stdout.strip() if r.returncode == 0 else ''
    common = git('rev-parse', '--git-common-dir')
    if not common:
        return cwd
    common = (cwd / common).resolve()
    if common.name == '.git':
        return common.parent
    worktree = git('config', '--file', str(common / 'config'), '--get', 'core.worktree')
    if worktree and (common / worktree).resolve().is_dir():
        return (common / worktree).resolve()
    top = git('rev-parse', '--show-toplevel')
    return Path(top).resolve() if top else cwd


def check_halt():
    if halt_path().exists():
        raise ReadHalted('GitHub reads are halted; inspect the saved reason and explicitly restart the run')


def check_board(config):
    """--check --config: the hub checkout's halt file and every member checkout's.
    A halt anywhere stops the whole board run; the message names where."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'sb_config', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'super-board-config.py'))
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    try:
        cfg = resolver.checked(config)
    except resolver.ConfigError as error:
        raise ValueError(f'config: {error}') from None
    except SystemExit as error:
        raise ValueError(str(error.code)) from None
    places = [(m['key'] or 'repo', m['path']) for m in resolver.members_of(cfg, config)]
    if 'members' in cfg:
        places.insert(0, ('hub', resolver.hub_path(cfg, config)))
    halted = []
    for name, path in places:
        halt_file = board_root(Path(path)) / '.claude/super-board/github-halt.json'
        if halt_file.exists():
            halted.append(f'{name} ({halt_file})')
    override = os.environ.get('SB_GITHUB_HALT_FILE')
    if override and Path(override).exists():
        halted.append(f'override ({override})')
    if halted:
        raise ReadHalted('GitHub reads are halted in ' + ', '.join(halted)
                         + '; inspect the saved reason and explicitly restart the run')


def halt(reason, attempts, operation=None):
    path = halt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation keeps the first failure's evidence when workers trip together.
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump({'reason': reason, 'attempts': attempts, 'at': int(time.time()), 'operation': operation}, stream)
    except FileExistsError:
        pass
    raise ReadHalted(reason)


def read_only(args):
    if len(args) < 2:
        return False
    if args[0] in ('pr', 'issue', 'project', 'repo', 'run'):
        return args[1] in ('view', 'list', 'diff', 'checks', 'item-list', 'field-list')
    if args[0] != 'api':
        return False
    if any(a == '--input' or a.startswith('--input=') for a in args):
        return False
    if 'graphql' in args:
        # GraphQL queries use POST, but only a literal query may be retried.
        queries = [arg[6:] for arg in args if arg.startswith('query=')]
        return (len(queries) == 1 and re.match(r'\s*query\b', queries[0]) is not None
                and re.search(r'\b(?:mutation|subscription)\b', queries[0]) is None
                and not any(a.startswith('operationName=') for a in args))
    for i, arg in enumerate(args):
        if arg in ('-X', '--method') and (i + 1 == len(args) or args[i + 1] != 'GET'):
            return False
        if arg.startswith('-X') and arg not in ('-X', '-XGET'):
            return False
        if arg.startswith(('-f', '-F')):
            return False
        if arg.startswith('--method=') and arg != '--method=GET':
            return False
        if arg in ('-f', '-F', '--field', '--raw-field', '--input') or arg.startswith(('--field=', '--raw-field=', '--input=')):
            return False
    return True


def permanent_error(text):
    if re.search(r'rate limit|secondary rate', text, re.I):
        return False
    return re.search(r'Cannot query field|Unknown (?:argument|type)|Syntax Error|Parse error on|'
                     r'GRAPHQL_VALIDATION_FAILED|undefinedField|variableRequiresValidType|'
                     r'HTTP 40[134]|Bad credentials|requires authentication|Resource not accessible|'
                     r'Could not resolve to (?:a|an) |missing required scopes', text, re.I) is not None


def require(condition, why):
    if not condition:
        raise ValueError(why)


def no_errors(value):
    pages = value if isinstance(value, list) else [value]
    if any(isinstance(p, dict) and p.get('errors') for p in pages):
        raise GitHubErrors('GraphQL returned errors with incomplete data')


# Per-ref errors a batched cross-repo state read may carry next to its data: a
# number that does not exist, or a linked repo this token cannot see. The ref is
# unreadable (its card stays blocked); the run is not halted for it.
PARTIAL_OK = ('NOT_FOUND', 'FORBIDDEN')


def tolerated_partial(text):
    """True when a foreign-state payload has data and only per-ref errors."""
    try:
        value = json.loads(text)
    except ValueError:
        return False
    errors = value.get('errors') if isinstance(value, dict) else None
    return (isinstance(value, dict) and isinstance(value.get('data'), dict)
            and isinstance(errors, list) and bool(errors)
            and all(isinstance(e, dict) and e.get('type') in PARTIAL_OK for e in errors))


def validate(text, kind, meta=None):
    if kind == 'foreign-state':
        value = json.loads(text)
        require(isinstance(value, dict) and isinstance(value.get('data'), dict), 'foreign state data missing')
        if value.get('errors') and not tolerated_partial(text):
            raise GitHubErrors('GraphQL returned errors with incomplete data')
        return value
    if kind == 'scalar':
        require(bool(text.strip()), 'required value missing')
        return text
    if kind == 'issue-number':
        require(not text.strip() or re.fullmatch(r'[1-9][0-9]*', text.strip()), 'invalid issue lookup result')
        return text
    if kind == 'head':
        require(re.fullmatch(r'\S+ [0-9a-f]{40}\s*', text) is not None, 'missing PR branch or full head')
        return text
    if kind == 'state':
        require(text.strip() in ('OPEN', 'CLOSED'), 'missing issue state')
        return text
    if kind == 'diff':
        require(isinstance(meta, dict), 'diff metadata missing')
        files = meta['files']
        if not files:
            require(not text.strip() and meta['additions'] == meta['deletions'] == 0, 'diff and file list disagree')
            return text
        lines = text.splitlines()
        require(sum(line.startswith('diff --git ') for line in lines) == len(files), 'diff file list incomplete')
        added = removed = 0
        old_left = new_left = 0
        for line in lines:
            if line.startswith('@@ '):
                require(old_left == new_left == 0, 'diff hunk truncated')
                hunk = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                require(hunk is not None, 'diff hunk malformed')
                old_left = int(hunk[2] or 1)
                new_left = int(hunk[4] or 1)
            elif old_left or new_left:
                if line.startswith('\\ No newline'):
                    continue
                require(bool(line) and line[0] in ' +-', 'diff hunk data missing')
                if line[0] != '+': old_left -= 1
                if line[0] != '-': new_left -= 1
                if line[0] == '+': added += 1
                if line[0] == '-': removed += 1
                require(old_left >= 0 and new_left >= 0, 'diff hunk length disagrees')
            elif line and not line.startswith(('diff --git ', 'index ', '--- ', '+++ ',
                    'new file mode ', 'deleted file mode ', 'old mode ', 'new mode ',
                    'similarity index ', 'dissimilarity index ', 'rename from ', 'rename to ',
                    'copy from ', 'copy to ', 'Binary files ', '\\ No newline')):
                raise ValueError('unexpected content outside diff hunks')
        require(old_left == new_left == 0, 'diff hunk truncated')
        require(added == meta['additions'] and removed == meta['deletions'], 'diff line counts incomplete')
        return text
    value = json.loads(text)
    no_errors(value)
    if kind == 'metadata':
        require(isinstance(value, dict), 'PR metadata is not an object')
        require(isinstance(value.get('files'), list) and isinstance(value.get('labels'), list)
                and isinstance(value.get('body'), str), 'PR policy fields missing')
        require(all(type(value.get(k)) is int and value[k] >= 0 for k in ('additions', 'deletions', 'changedFiles')), 'PR counts missing')
        require(value['changedFiles'] == len(value['files']), 'PR file list truncated')
        require(all(isinstance(f, dict) and isinstance(f.get('path'), str) and f['path'] for f in value['files']), 'PR paths missing')
        require(all(isinstance(l, dict) and isinstance(l.get('name'), str) for l in value['labels']), 'PR labels missing')
    elif kind == 'items':
        require(isinstance(value, dict) and isinstance(value.get('items'), list), 'board items missing')
        require(type(value.get('totalCount')) is int and value['totalCount'] == len(value['items']), 'board item list incomplete')
        require(all(isinstance(i, dict) and isinstance(i.get('id'), str) and i['id'] and isinstance(i.get('content'), dict) for i in value['items']), 'board item data missing')
        for item in value['items']:
            content = item['content']
            require(content.get('type') in ('Issue', 'PullRequest', 'DraftIssue') and isinstance(content.get('title'), str), 'board content identity missing')
            if content['type'] != 'DraftIssue':
                require(type(content.get('number')) is int and content['number'] > 0, 'board issue number missing')
    elif kind == 'issues':
        require(isinstance(value, list) and bool(value), 'dependency pages missing')
        seen = set()
        for index, page in enumerate(value):
            connection = page['data']['repository']['issues']
            nodes, info = connection['nodes'], connection['pageInfo']
            require(isinstance(nodes, list) and type(info['hasNextPage']) is bool, 'dependency page malformed')
            require(info['hasNextPage'] == (index < len(value) - 1), 'dependency pagination incomplete')
            if info['hasNextPage']:
                require(isinstance(info.get('endCursor'), str) and info['endCursor'] and info['endCursor'] not in seen, 'dependency cursor repeated or missing')
                seen.add(info['endCursor'])
            require(all(isinstance(n, dict) and type(n.get('number')) is int and isinstance(n.get('body'), str)
                        and isinstance(n.get('title'), str) and n.get('state') == 'OPEN'
                        and isinstance(n.get('comments', {}).get('nodes'), list)
                        and isinstance(n.get('labels', {}).get('nodes'), list) for n in nodes), 'dependency issue fields missing')
    elif kind == 'issue-history':
        require(isinstance(value, list) and bool(value), 'issue history pages missing')
        numbers, cursors, total = set(), set(), None
        for index, page in enumerate(value):
            connection = page['data']['repository']['issues']
            nodes, info, count = connection['nodes'], connection['pageInfo'], connection['totalCount']
            require(type(count) is int and count >= 0 and (total is None or total == count), 'issue count changed during pagination')
            total = count
            require(isinstance(nodes, list) and type(info['hasNextPage']) is bool, 'issue history page malformed')
            require(info['hasNextPage'] == (index < len(value) - 1), 'issue history pagination incomplete')
            if info['hasNextPage']:
                cursor = info.get('endCursor')
                require(isinstance(cursor, str) and cursor and cursor not in cursors, 'issue history cursor repeated/missing')
                cursors.add(cursor)
            for node in nodes:
                require(isinstance(node, dict) and type(node.get('number')) is int and node['number'] > 0
                        and node['number'] not in numbers and node.get('state') in ('OPEN', 'CLOSED')
                        and isinstance(node.get('body'), str), 'issue history fields missing/duplicate')
                numbers.add(node['number'])
        require(len(numbers) == total, 'issue history count incomplete')
    elif kind == 'quota':
        resources = value['resources']
        for bucket in ('graphql', 'core'):
            require(all(type(resources[bucket].get(k)) is int and resources[bucket][k] >= 0
                        for k in ('remaining', 'reset')), 'quota data missing')
    elif kind == 'projects':
        require(isinstance(value.get('projects'), list), 'project list missing')
    elif kind == 'project':
        require(isinstance(value, dict) and isinstance(value.get('id'), str) and value['id'], 'project ID missing')
    elif kind == 'fields':
        require(isinstance(value.get('fields'), list) and any(isinstance(f, dict) and f.get('name') == 'Status' and f.get('id') and isinstance(f.get('options'), list) for f in value['fields']), 'Status field missing')
        status = next(f for f in value['fields'] if f.get('name') == 'Status')
        require(all(isinstance(o, dict) and isinstance(o.get('id'), str) and o['id']
                    and isinstance(o.get('name'), str) and o['name'] for o in status['options']), 'Status option identity missing')
    elif kind == 'status-field':
        data = value['data']
        owner = data.get('user') or data.get('organization')
        field = owner['projectV2']['field']
        require(isinstance(field.get('id'), str) and field['id'] and isinstance(field.get('options'), list)
                and all(isinstance(o, dict) and o.get('id') and o.get('name') for o in field['options']), 'Status field missing')
    elif kind == 'permission':
        require(value.get('permission') in ('none', 'read', 'triage', 'write', 'maintain', 'admin'), 'collaborator permission missing')
    elif kind == 'comments':
        require(isinstance(value, list) and bool(value) and all(isinstance(p, list) for p in value), 'comment pages missing')
        require(all(isinstance(c, dict) and type(c.get('id')) is int and isinstance(c.get('body'), str)
                    and isinstance(c.get('user'), dict) and c.get('created_at') and c.get('updated_at')
                    for page in value for c in page), 'comment fields missing')
    elif kind == 'approval':
        require(isinstance(value, list) and bool(value), 'approval pages missing')
        heads = set()
        for i, page in enumerate(value):
            pr = page['data']['repository']['pullRequest']
            require(re.fullmatch(r'[0-9a-f]{40}', pr['headRefOid']) and pr['state'] in ('OPEN', 'CLOSED', 'MERGED'), 'PR identity missing')
            heads.add(pr['headRefOid'])
            links = pr['closingIssuesReferences']
            require(type(links['pageInfo']['hasNextPage']) is bool and links['pageInfo']['hasNextPage'] == (i < len(value) - 1), 'approval pagination incomplete')
            require(isinstance(links['nodes'], list), 'linked issues missing')
        require(len(heads) == 1, 'PR changed during approval read')
    elif kind == 'merge-state':
        require(value.get('state') in ('OPEN', 'CLOSED', 'MERGED') and re.fullmatch(r'[0-9a-f]{40}', value.get('headRefOid', '')), 'merge outcome unreadable')
        if value['state'] == 'MERGED':
            require(re.fullmatch(r'[0-9a-f]{40}', (value.get('mergeCommit') or {}).get('oid', '')), 'merged commit missing')
    elif kind == 'body':
        require(isinstance(value.get('body'), str) and re.fullmatch(r'[0-9a-f]{40}', value.get('headRefOid', '')), 'PR body/head missing')
    elif kind == 'dedupe':
        require(isinstance(value, list) and len(value) < 200, 'dedupe list missing or capped; fetch complete issue history before creating')
        require(all(isinstance(i, dict) and type(i.get('number')) is int and i['number'] > 0 and isinstance(i.get('body'), str) for i in value), 'dedupe issue fields missing')
    elif kind in ('prs-open', 'prs-merged', 'issues-closed', 'issue'):
        entries = [value] if kind == 'issue' else value
        require(isinstance(entries, list), 'required issue/PR list missing')
        for entry in entries:
            require(isinstance(entry, dict) and type(entry.get('number')) is int and entry['number'] > 0
                    and isinstance(entry.get('title'), str) and isinstance(entry.get('body'), str), 'issue/PR identity or body missing')
            if kind.startswith('prs-'):
                require(isinstance(entry.get('url'), str), 'PR URL missing')
            if kind == 'prs-open':
                require(isinstance(entry.get('headRefName'), str) and entry['headRefName'] and isinstance(entry.get('files'), list)
                        and all(isinstance(f, dict) and isinstance(f.get('path'), str) and f['path'] for f in entry['files']), 'open PR branch/files missing')
            if kind == 'issues-closed':
                require(entry.get('stateReason') in ('COMPLETED', 'NOT_PLANNED', 'REOPENED', None) and 'stateReason' in entry, 'closed issue reason missing')
    elif kind == 'object':
        require(isinstance(value, dict) and bool(value), 'required object missing')
    elif kind == 'array':
        require(isinstance(value, list), 'required list missing')
    return value


def read(args, kind='json', meta=None, recovering=False):
    if not read_only(args):
        raise ValueError('retry helper accepts read-only GitHub commands only')
    if not recovering:
        check_halt()
    operation = {'args': args, 'kind': kind, 'meta': meta}
    last = 'GitHub read failed'
    shape_only = True  # every attempt reached GitHub and got a payload of the wrong shape
    delay = float(os.environ.get('SB_GITHUB_RETRY_DELAY', '1'))
    for attempt in range(1, 4):
        if not recovering:
            check_halt()
        try:
            result = subprocess.run(command('gh', args), capture_output=True, text=True, encoding='utf-8', timeout=30)
            # gh exits 1 on any GraphQL error, even beside usable data. A cross-repo
            # state read whose only errors are per-ref is an answer, not an outage.
            partial = kind == 'foreign-state' and tolerated_partial(result.stdout)
            if partial:
                result = subprocess.CompletedProcess(result.args, 0, result.stdout, '')
            diagnostic = result.stderr
            if result.returncode:
                diagnostic += result.stdout
            elif partial:
                pass
            elif kind != 'diff':
                try:
                    payload = json.loads(result.stdout)
                    diagnostic += json.dumps([p.get('errors', []) for p in (payload if isinstance(payload, list) else [payload]) if isinstance(p, dict)])
                except ValueError:
                    pass
            if permanent_error(diagnostic):
                halt('GitHub rejected a required read (query, authentication, or permission error); fix it before restarting', attempt, operation)
            if result.returncode:
                shape_only = False
                status = re.search(r'HTTP [0-9]{3}', result.stderr)
                last = 'GitHub required read failed' + (f' ({status[0]})' if status else '')
            else:
                try:
                    value = validate(result.stdout, kind, meta)
                    if not recovering:
                        check_halt()
                    return result.stdout, value
                except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
                    last = f'GitHub required read returned incomplete evidence: {error}'
                    shape_only = shape_only and not isinstance(error, GitHubErrors)
        except (OSError, subprocess.SubprocessError):
            shape_only = False
            last = 'GitHub required read timed out or could not start'
        if attempt < 3:
            print(f'[github-read] {last}; retry {attempt + 1}/3', file=sys.stderr)
            time.sleep(max(0, delay) * attempt)
    if shape_only and kind in FORMS and not os.environ.get('SB_GITHUB_READ_STRICT'):
        raise ValueError(f'{last}; this call failed, the run continues. Valid form: --kind {kind} -- {FORMS[kind]}')
    halt(f'{last}; stopped after 3 failed attempts', 3, operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('foreign-state', 'json','scalar','issue-number','head','state','diff','metadata','items','issues','issue-history','quota','projects','project','fields','status-field','permission','comments','approval','merge-state','body','dedupe','prs-open','prs-merged','issues-closed','issue','object','array'))
    parser.add_argument('--issue-history', help='complete paginated all-state issue history for owner/name (comma list: several repos)')
    parser.add_argument('--meta')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--config', help='with --check: check the hub and every member checkout of this config')
    parser.add_argument('--halt', help='record an unresolved mutation outcome and stop; never retry it')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        if args.halt:
            halt(args.halt, 1)
        if args.resume:
            path = halt_path()
            if path.exists():
                record = json.loads(path.read_text(encoding='utf-8'))
                operation = record.get('operation')
                if not isinstance(operation, dict):
                    raise ReadHalted('saved failure needs manual outcome reconciliation before restart')
                if command:
                    if args.kind != operation['kind']:
                        raise ValueError('replacement recovery read must validate the same evidence kind')
                    meta = json.loads(Path(args.meta).read_text(encoding='utf-8')) if args.meta else operation.get('meta')
                    if args.kind == 'diff':
                        validate(json.dumps(meta), 'metadata')
                    read(command, args.kind, meta, recovering=True)
                else:
                    read(operation['args'], operation['kind'], operation.get('meta'), recovering=True)
                read(['api', 'rate_limit'], 'quota', recovering=True)
                path.replace(path.with_name(f'github-halt-{time.time_ns()}.json'))
            return 0
        if args.check:
            if args.config:
                check_board(args.config)
            else:
                check_halt()
            return 0
        if args.issue_history:
            # owner/name, or a comma list for a whole hub board: every node then carries
            # its `repo`, and one incomplete repo fails the whole read.
            repos = args.issue_history.split(',')
            require(all(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', r) for r in repos), 'issue history requires owner/name')
            query = 'query($owner:String!,$repo:String!,$endCursor:String){repository(owner:$owner,name:$repo){issues(first:100,after:$endCursor,states:[OPEN,CLOSED]){totalCount pageInfo{hasNextPage endCursor} nodes{number state body}}}}'
            nodes = []
            for name in repos:
                owner, repo = name.split('/')
                _, pages = read(['api','graphql','--paginate','--slurp','-f','query='+query,'-F','owner='+owner,'-F','repo='+repo], 'issue-history')
                for page in pages:
                    for node in page['data']['repository']['issues']['nodes']:
                        nodes.append({**node, 'repo': name} if len(repos) > 1 else node)
            print(json.dumps(nodes))
            return 0
        meta = json.loads(Path(args.meta).read_text(encoding='utf-8')) if args.meta else None
        output, _ = read(command, args.kind or 'json', meta)
        sys.stdout.write(output)
        return 0
    except ReadHalted as error:
        where = '' if args.check and args.config else f' Saved at {halt_path()}'
        print(f'[github-read] HALT: {error}.{where}', file=sys.stderr)
        return HALTED
    except (ValueError, OSError) as error:
        print(f'[github-read] {error}', file=sys.stderr)
        return 64


if __name__ == '__main__':
    # Windows pipes default to the ANSI code page; super-board text is UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    sys.exit(main())
