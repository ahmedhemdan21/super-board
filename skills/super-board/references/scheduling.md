# Scheduling super-board

Two ways to run the board and the collector without a person at the keyboard. Both report
into the same place: one open issue per repo titled `super-board runs` (label
`super-board-runs`), one comment per run, in the writing-standard comment format with role
`scheduler`.

| | Local scheduler | Cloud routine (`/schedule`) |
|---|---|---|
| Runs | `.claude/bin/super-board-scheduler.py` on your machine or a server | Claude's cloud, no machine needed |
| Steps | `collect` (every configured source) and `run` (the board) | collect with cloud-safe sources only |
| Reaches | ssh, Docker, local stacks, Loki / Prometheus / Tempo | public APIs only: no ssh, no Docker |
| History | run records + web UI on 127.0.0.1 | the runs issue only |

## (a) The local scheduler

stdlib Python 3.9+, the same file on Windows, Linux and macOS. Full contract in the script
header (`python3 .claude/bin/super-board-scheduler.py --help`).

1. Write the config: `%APPDATA%\super-board\scheduler.json` on Windows,
   `~/.config/super-board/scheduler.json` elsewhere (`--config` overrides).

   ```json
   {"port": 8765, "jobs": [
     {"name": "backend-nightly", "repo": "D:/Work/PrimeCore/Project/Platform/backend",
      "config": "primecore-backend", "steps": ["collect", "run"], "cron": "0 2 * * *"},
     {"name": "backend-after-deploy", "repo": "D:/Work/PrimeCore/Project/Platform/backend",
      "config": "primecore-backend", "steps": ["collect"],
      "after_workflow": {"repo": "PrimeCoreDev/primecore-platform", "workflow": "deploy-dev.yml", "branch": "dev"}}
   ]}
   ```

   `steps` is an enum. There is no command field: the scheduler runs nothing but
   `claude -p "/super-collect --yes"` and `claude -p "/super-board run <config>"`.
   Optional per job: `collect_sources`, `timeout_s` (default 14400), `permission_mode`
   (never `bypassPermissions`), `max_budget_usd`, `runs_issue` (default true),
   `mention_on_failure` (a GitHub login, default none). `check-config` validates it.
2. Let the board run headless. `claude -p` has the Workflow tool, but denies it unless
   allowed; the scheduler passes `--allowedTools Workflow` on the `run` step. A headless
   session has nobody to answer a permission prompt, so every command the lanes use must
   already be in the project's `permissions.allow` (`super-board onboard` → Permissions).
   Only `worker_backend: "workflow"` is scheduled; a `claude-p` config is refused, and a
   session that reports no Workflow tool is stopped with that reason. Nothing falls back.
3. Try it: `super-board-scheduler.py run-now backend-nightly`, then `status` and
   `show <job>/<stamp>`.
4. Keep it running: `super-board-scheduler.py install-service` (as yourself, never admin):
   Windows Task Scheduler task `super-board-scheduler` at logon (`pythonw … serve`, limited
   rights); Linux a systemd `--user` unit; macOS a launchd agent. It prints what it did and
   `uninstall-service` undoes it. `--dry-run` only prints.
5. Watch it: `super-board-scheduler.py ui-url` prints the loopback link with its token.
   Runs list, run detail (summary + log tail), next runs, Run now.

What it guarantees:

- One run per repo at a time. A board already running there (`inflight/workflow-wave.lock`,
  `codex-wave.pid`, a legacy lane lock) or a GitHub halt → the run is recorded `skipped`.
- A cron slot found more than 5 minutes late (machine off or asleep) is recorded `missed`,
  never run late. DST: a skipped wall time fires right after the jump; a repeated one once.
- `after_workflow` fires once per new green run id, and never while GitHub quota is under 500.
- A step past `timeout_s` is killed with its whole process tree (`taskkill /T /F` on Windows,
  the process group elsewhere). Ctrl+C / SIGTERM on `serve` stops running steps the same way.
- `summary.json` comes from board snapshots (`super-board-status.py --json` before and after),
  collect receipts and `gh`, never from Claude's chat. Logs are redacted with the guard hooks'
  key patterns. The last 200 runs per job are kept.
- `super-board scorecard` shows the last runs next to the board metrics.

## (b) A cloud routine (`/schedule`)

The cloud has no ssh and no Docker, so it can only run sources that call public APIs:
`sentry`, `posthog`, `slack`, `github`, `prs`, `architecture`. Never `loki`, `prometheus`, Tempo or
`run`. Secrets come from the routine's environment variables (`SENTRY_AUTH_TOKEN`,
`POSTHOG_PERSONAL_API_KEY`, `SLACK_BOT_TOKEN`), never from a `.env` file, and are never echoed.

Routine prompt template (fill the `<…>`):

> In the `<owner>/<repo>` repository, with super-board installed, run
> `/super-collect --sources sentry,posthog,slack,github,prs,architecture --yes` against config
> `<config slug>`. Use only those sources even if the config lists more; loki, prometheus
> and tempo need ssh or Docker and are not available here. Secrets are in the environment;
> never print them. Verify and dedupe before filing; file into Backlog only. Do not run the
> board, build, merge or deploy. When done, find the open issue titled exactly
> `super-board runs` with label `super-board-runs` (create it if missing) and add one
> comment, at most 8 lines:
> `[scheduler] [report] <✅ ok | ❌ failed> · cloud-collect`
> `Did: job cloud-collect · trigger cloud routine · <duration>`
> `✅ Done: <n> filed (<source> <n>, …) · <n> duplicates`
> `❌ Not done: <unavailable sources or errors>` (omit when nothing)
> `Next: next run <when the routine runs next>`
> If a GitHub read halts or a write outcome is unknown, stop and say so in that comment.

Both feeds then land on the same runs issue: local runs and cloud runs side by side.

## (c) Moving to a Linux server

Same script, same config shape, Linux paths:

1. Install the pack into the repo checkouts on the server (`install.sh <repo>`), log in to
   `gh` and `claude` as the service user (never root), and copy any ssh keys the Loki /
   Prometheus transport needs.
2. Copy `scheduler.json` to `~/.config/super-board/scheduler.json` and change each `repo`
   to its server path. `check-config`.
3. `super-board-scheduler.py install-service`, then `loginctl enable-linger $USER` so the
   user unit keeps running after you log out.
4. Reach the UI through an ssh tunnel (`ssh -L 8765:127.0.0.1:8765 server`), not by binding
   to a public address. `--bind` to a non-loopback address is refused unless you add
   `--allow-remote`, and the token is still required.
5. Stop the scheduler on the old machine (`uninstall-service`) so a job never runs twice.
