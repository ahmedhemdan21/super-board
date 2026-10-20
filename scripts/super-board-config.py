#!/usr/bin/env python3
"""super-board-config.py — one resolver for single-repo and hub (members) configs.

A hub config lists member repos under `members` (hub.md → Config). Every script that
needs one repo's settings takes a resolved single-repo config from here, so the merge
gate, deploy wait, preflight, card.sh and the planner never learn about members.

    super-board-config.py validate --config C                 exit 65 + reasons when invalid
    super-board-config.py member <key> --config C [--out F | --path]
        the member as a full single-repo config: board-wide keys + the member's keys
        (member wins; `collect` merges one level), `repo.path` absolute, `member`,
        `hub_config`, and every other member in `linked_repos`. --path writes
        <board root>/.claude/super-board/resolved/<slug>.<key>.json (regenerated when
        the source config is newer) and prints its path.
    super-board-config.py hub --config C [--out F]
        the hub repo as a single-repo config (P5 intake files parents there): board-wide
        keys, `repo` = hub.remote at the absolute hub path, `hub_config`, every member
        in `linked_repos`.
    super-board-config.py members --config C                  [{key, repo, path}]; a
        single-repo config is one pseudo-member with key "".
    super-board-config.py for-repo <owner/name> --config C    the member key; exit 3 if none
    super-board-config.py run-gate --config C [--backend B]   exit 78 for a members config on
        the legacy `claude-p` runner or the Codex wave (B = workflow | claude-p | codex;
        default the config's worker_backend, else workflow). A hub runs on the workflow
        backend (P4); a single-repo config always passes.

Board root = the folder holding `.claude/super-board/configs/<slug>.json`; a config
elsewhere uses its git toplevel, else its own folder. Member and repo paths that are
relative resolve against it.
"""
import argparse
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
# Keys that describe the board, never one member. A member setting one is an error.
# `max_workers` is both: top level caps the whole wave, `members.<k>.max_workers` caps
# one member's share of it (the planner, hub.md → Run).
BOARD_ONLY = {"project", "columns", "labels", "triage", "worker_backend",
              "model_tier", "no_progress_cycles", "rebuild_cap", "notifications",
              "members", "hub", "split", "version", "description"}
HUB_KEYS = ("members", "hub", "split")
NOT_MEMBER, INVALID, MISSING, USAGE, RUN_REFUSED = 3, 65, 66, 64, 78


def repo_name(remote):
    """owner/name from a GitHub URL, ssh remote or owner/name; '' when none."""
    text = str(remote or "").strip()
    text = re.sub(r"^(https?://github\.com/|git@github\.com:)", "", text)
    text = re.sub(r"\.git$", "", text).strip("/")
    return text if re.fullmatch(r"[\w.-]+/[\w.-]+", text) else ""


def load(path):
    try:
        with open(path, encoding="utf-8") as stream:
            cfg = json.load(stream)
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}") from None
    except ValueError as exc:
        raise ConfigError([f"config is not JSON: {exc}"]) from None
    if not isinstance(cfg, dict):
        raise ConfigError(["config must be a JSON object"])
    return cfg


class ConfigError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def board_root(config_path):
    path = Path(config_path).resolve()
    if path.parent.name == "configs" and path.parent.parent.name == "super-board" \
            and path.parent.parent.parent.name == ".claude":
        return path.parent.parent.parent.parent
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(path.parent),
                             capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return path.parent


def absolute(root, path):
    p = Path(os.path.expanduser(str(path or ".")))
    return str((p if p.is_absolute() else root / p).resolve())


def validate(cfg):
    """Errors for a config's hub shape; [] for a valid one. Single-repo configs only
    get the check that they do not half-declare a hub."""
    errors = []
    members = cfg.get("members")
    if members is None:
        for key in ("hub", "split"):
            if key in cfg:
                errors.append(f"`{key}` needs `members` (a single-repo config has neither)")
        return errors
    if not isinstance(members, dict) or not members:
        return ["`members` must be an object with at least one member"]
    if "repo" in cfg:
        errors.append("a members config names no top-level `repo`: the hub repo goes in `hub`, "
                      "each code repo in `members.<key>.repo`")
    hub = cfg.get("hub")
    hub_repo = ""
    if not isinstance(hub, dict) or not repo_name(hub.get("remote")):
        errors.append("`hub.remote` must name the hub repo (https://github.com/<owner>/<repo>)")
    else:
        hub_repo = repo_name(hub["remote"]).lower()
        if "path" in hub and (not isinstance(hub["path"], str) or not hub["path"].strip()):
            errors.append("`hub.path` must be a non-empty path")
    seen = {}
    for key, member in members.items():
        where = f"members.{key}"
        if not KEY_RE.match(key):
            errors.append(f"member key {key!r} must match {KEY_RE.pattern}")
        elif key == "hub":
            errors.append("member key 'hub' is reserved: it names the hub repo (--target hub)")
        if not isinstance(member, dict):
            errors.append(f"{where} must be an object")
            continue
        repo = member.get("repo")
        if not isinstance(repo, dict):
            errors.append(f"{where}.repo must be {{path, remote}}")
        else:
            name = repo_name(repo.get("remote"))
            if not name:
                errors.append(f"{where}.repo.remote must be a GitHub repo URL")
            elif name.lower() in seen:
                errors.append(f"{where}.repo.remote is also members.{seen[name.lower()]}'s: remotes must be unique")
            elif name.lower() == hub_repo:
                errors.append(f"{where}.repo.remote is the hub repo: a member is a code repo")
            else:
                seen[name.lower()] = key
            if not isinstance(repo.get("path"), str) or not repo["path"].strip():
                errors.append(f"{where}.repo.path must be the member's checkout (relative to the hub checkout, or absolute)")
        if not isinstance(member.get("base_branch"), str) or not member["base_branch"].strip():
            errors.append(f"{where}.base_branch is required")
        workers = member.get("max_workers")
        if workers is not None and (type(workers) is not int or workers < 0):
            errors.append(f"{where}.max_workers must be a non-negative integer")
        board = sorted(BOARD_ONLY & set(member))
        if board:
            errors.append(f"{where} sets board-wide key(s) {', '.join(board)}: set them at the top level")
    split = cfg.get("split")
    if split is not None:
        if not isinstance(split, dict):
            errors.append("`split` must be an object")
        else:
            order = split.get("default_order", [])
            if not isinstance(order, list) or any(k not in members for k in order) or len(set(order)) != len(order):
                errors.append("`split.default_order` must list member keys, each once")
            if "confirm" in split and not isinstance(split["confirm"], bool):
                errors.append("`split.confirm` must be true or false")
    return errors


def checked(path):
    cfg = load(path)
    errors = validate(cfg)
    if errors:
        raise ConfigError(errors)
    return cfg


def members_of(cfg, path):
    root = board_root(path)
    if "members" not in cfg:
        repo = cfg.get("repo") or {}
        return [{"key": "", "repo": repo_name(repo.get("remote")), "path": absolute(root, repo.get("path", "."))}]
    return [{"key": k, "repo": repo_name(m["repo"]["remote"]), "path": absolute(root, m["repo"]["path"])}
            for k, m in cfg["members"].items()]


def hub_path(cfg, path):
    """The hub checkout (where the hub's own .claude/super-board lives)."""
    return absolute(board_root(path), (cfg.get("hub") or {}).get("path", "."))


def resolve_member(cfg, path, key):
    if "members" not in cfg:
        raise ConfigError(["not a members config: it is already one repo's config"])
    if key not in cfg["members"]:
        raise ConfigError([f"no member {key!r} (members: {', '.join(cfg['members'])})"])
    member = cfg["members"][key]
    out = {k: copy.deepcopy(v) for k, v in cfg.items() if k not in HUB_KEYS}
    for k, v in member.items():
        if k == "collect" and isinstance(v, dict) and isinstance(out.get("collect"), dict):
            out["collect"] = {**out["collect"], **copy.deepcopy(v)}
        else:
            out[k] = copy.deepcopy(v)
    out["repo"] = {**member["repo"], "path": absolute(board_root(path), member["repo"]["path"])}
    # Members trust each other's issues as blockers, like linked_repos.
    linked, have = [], set()
    others = [repo_name(m["repo"]["remote"]) for k, m in cfg["members"].items() if k != key]
    own = repo_name(member["repo"]["remote"]).lower()
    for repo in list(out.get("linked_repos") or []) + others:
        if isinstance(repo, str) and repo.lower() not in have and repo.lower() != own:
            have.add(repo.lower())
            linked.append(repo)
    out["linked_repos"] = linked
    out["member"] = key
    out["hub_config"] = str(Path(path).resolve())
    return out


def resolve_hub(cfg, path):
    """The hub repo as one repo's config: where intake files parents (hub.md → Intake)."""
    if "members" not in cfg:
        raise ConfigError(["not a members config: it has no hub"])
    out = {k: copy.deepcopy(v) for k, v in cfg.items() if k not in HUB_KEYS}
    out["repo"] = {"remote": cfg["hub"]["remote"], "path": hub_path(cfg, path)}
    out["linked_repos"] = [repo_name(m["repo"]["remote"]) for m in cfg["members"].values()]
    out["hub_config"] = str(Path(path).resolve())
    return out


def cache_path(path, key):
    return board_root(path) / ".claude" / "super-board" / "resolved" / f"{Path(path).stem}.{key}.json"


def cached_member(cfg, path, key):
    target = cache_path(path, key)
    if not target.exists() or Path(path).stat().st_mtime_ns > target.stat().st_mtime_ns:
        target.parent.mkdir(parents=True, exist_ok=True)
        ignore = target.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(resolve_member(cfg, path, key), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=("validate", "member", "hub", "members", "for-repo", "run-gate"))
    parser.add_argument("arg", nargs="?")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out")
    parser.add_argument("--path", action="store_true")
    parser.add_argument("--backend", choices=("workflow", "claude-p", "codex"))
    args = parser.parse_args(argv)
    try:
        cfg = checked(args.config)
        if args.cmd == "validate":
            return 0
        if args.cmd == "run-gate":
            backend = args.backend or cfg.get("worker_backend") or "workflow"
            if "members" in cfg and backend != "workflow":
                print(f"run refused: {args.config} is a hub (members) config and hub boards run "
                      f"on the workflow backend only, not {backend} (hub.md → Run). Run it with "
                      "/super-board run on Claude, or run one member's own single-repo config.",
                      file=sys.stderr)
                return RUN_REFUSED
            return 0
        if args.cmd == "members":
            print(json.dumps(members_of(cfg, args.config)))
            return 0
        if args.cmd == "for-repo":
            want = (args.arg or "").lower()
            for m in members_of(cfg, args.config):
                if m["repo"] and m["repo"].lower() == want:
                    print(m["key"])
                    return 0
            print(f"{args.arg} is not a member of this board", file=sys.stderr)
            return NOT_MEMBER
        if args.cmd == "hub":
            text = json.dumps(resolve_hub(cfg, args.config), indent=2) + "\n"
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            return 0
        if not args.arg:
            print("member needs a key", file=sys.stderr)
            return USAGE
        if args.path:
            print(cached_member(cfg, args.config, args.arg))
            return 0
        text = json.dumps(resolve_member(cfg, args.config, args.arg), indent=2) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    except ConfigError as exc:
        for error in exc.errors:
            print(f"config: {error}", file=sys.stderr)
        return INVALID
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return MISSING
        raise


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
