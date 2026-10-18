---
name: super-board
description: GitHub-Project-driven autonomous pipeline. Six verbs — onboard, lint, status, run, stop, link — that take a Project board from empty to drained across Build → QA → Review → Done lanes (labels qa · bug · feature route each card), with graceful shutdown / resume. Use when the user says "super-board", "/super-board", "drain my GitHub project", "set up the autonomous loop", "kick off the headless build/QA pipeline", or "stop super-board".
---

# super-board — autonomous GitHub Project pipeline

## Six verbs

| Verb | Where | What it does |
|---|---|---|
| `super-board onboard` | interactive | one-time setup wizard; writes `.claude/super-board/configs/<slug>.json` |
| `super-board lint` | interactive | walks active-pipeline issues, flags vague ACs, runs pre-flight readiness |
| `super-board status` | interactive (read-only) | snapshot of active config, column counts, in-flight workers |
| `super-board run` | in-session (default) | the autonomous loop: waves of the `super-board-wave` dynamic workflow, run from this session (`worker_backend: "claude-p"` opts into the legacy headless runner `.claude/bin/super-board-run.sh`). Also the resume command — state lives on the board, not in process memory. Accepts a model-tier flag: `--low` (haiku/sonnet/opus ladder), default = medium (sonnet/opus/session), `--high` (opus for every card). `--codex [--low|--high]` overrides the backend for every lane with the matching Codex GPT ladder; `--codex=<model>` or `codex.model` pins one model for every card (see `references/run-workflow.md`). A host with no Workflow tool (a Codex session) runs as `--codex` by itself. |
| `super-board stop` | interactive | graceful shutdown: posts "stopped mid-flight" comments on every in-flight issue + PR, releases assignee mutexes, kills workers + dispatcher. Next `super-board run` resumes. |
| `super-board link` | interactive | cross-board flow: create the counterpart of a card on a linked repo's board (Backlog) and write `Blocked by` / `Linked` refs both ways — `references/link.md` |

If invoked with no verb, ask which one.

**`super-board scorecard [--since 14d]`** (read-only helper, not a lane verb): run
`python3 .claude/bin/super-board-metrics.py --config <cfg> --since 14d --html <scratch>/scorecard.html`
and show its text table: cycle time per lane, QA→Build bounce rate, first-pass merge rate,
human-merge share, reverts, flaky / regression cards, cards per collect source, GitHub quota.
A `null` comes with its reason; say it, never fill it in. In a session that has the Artifact
tool, offer to publish the HTML file (it is self-contained); never publish without a yes.
Scheduled runs and their history: `references/scheduling.md`.

In Codex the skill is not a slash command: it is `$super-board <verb>`, found in `.agents/skills/`.

## Routing

| If user says | Load |
|---|---|
| `super-board onboard ...` | `references/onboard.md` |
| `super-board lint ...` | `references/lint.md` |
| `super-board status ...` | `references/status.md` |
| `super-board run --codex[=<model>] ...`, or any `run` from a host with no Workflow tool (overrides any configured backend) | `references/run-workflow.md` → "Codex runs" |
| `super-board run ...` (default — `worker_backend` unset or `"workflow"`) | `references/run-workflow.md` (lane lifecycles still come from `references/run.md`) |
| `super-board run ...` with config `worker_backend: "claude-p"` (legacy, explicit opt-in) | `references/run.md` |
| `super-board link ...` / "the backend card blocks this one" / "create the frontend half" / `linked_repos` | `references/link.md` |
| `super-board stop ...` / "stop the run" / "pause the loop" / "kill super-board" | `references/stop.md` |
| `super-board scorecard ...` / "how is the board doing" / "board metrics" | the scorecard paragraph above (`scripts/super-board-metrics.py` header has every definition) |
| "run it every night" / "after each deploy" / scheduler / cloud routine | `references/scheduling.md` |
| "resume" / "pick up where I left off" / "restart after stop" | `references/stop.md` (resume = run; no separate verb) |
| Anything about Blocked exits, 🙋 needs you, or dropping a card | `references/block-template.md` |
| Optional learning after a successfully completed run | `references/calibration.md` (external installed skill; bounded receipt; explicit existing-skill edit paths) |
| Config structure questions | `references/config-schema.json` |
| Worker gh-call discipline / rate-limit recovery | `references/rate-limit-etiquette.md` (+ `scripts/super-board-gh-guard.sh`) |

## Orchestrator vs worker — the cardinal rule

super-board is an **autonomous trader**. The interactive Claude session that invokes any of the six verbs is an **orchestrator**, not a worker. The orchestrator:

- Validates preconditions, then dispatches per the config's `worker_backend`: `"workflow"` (default) → stay in-session and run the wave loop in `references/run-workflow.md` (launch workflow, reconcile, repeat); `"claude-p"` (legacy, explicit opt-in only) → `nohup .claude/bin/super-board-run.sh`, report PID + log path, exit. Explicit `--codex` overrides either backend with headless Codex waves; so does a host with no Workflow tool, or `bash .claude/bin/super-board-host.sh` printing `codex`. The orchestrator never does product work itself.
- Delegates all build / QA / review work to workers — headless `claude -p` (claude-p backend), workflow lane agents (workflow backend), or headless `codex exec` (explicit `--codex`).
- Must NOT do product work itself, must NOT patch the dispatcher mid-run, must NOT wait for workers, must NOT hold context for multi-card progress.

If anything goes wrong during a run, the orchestrator captures the symptom and reports back — it does not silently expand the task into a fix. See `references/run.md` "Orchestrator delegation contract" for the full rule.
