export const meta = {
  name: 'super-board-wave',
  description: 'Drain one super-board wave: classify, then build → qa → review per card (lifecycles per run.md)',
  whenToUse: 'Launched by the super-board run-workflow backend with args from super-board-wave-plan.sh. Not for direct ad-hoc use.',
  phases: [
    { title: 'Pre-flight', detail: 'per Ready card: already done? in progress? file overlap? unclear? (run.md → Builder pre-flight)' },
    { title: 'Classify', detail: 'haiku router: kind (feature | bug | qa) + complexity per Ready card' },
    { title: 'Build', detail: 'Builder lifecycle (run.md): worktree, branch, draft PR' },
    { title: 'QA', detail: 'Tester lifecycle (run.md): test plan, evidence, screenshots' },
    { title: 'Review', detail: 'Reviewer lifecycle (run.md): gates, rerun tests, merge' },
    { title: 'Deploy', detail: 'Post-merge dev check (run.md): deploy run, health, smoke; failure files a bug, never reverts' },
  ],
}

// args = {
//   configPath: '.claude/super-board/configs/<slug>.json',
//   cards: [{ number, repository, status, title, lane, labels }],  // output of super-board-wave-plan.sh (home-repo cards only)
//     lane: 'build' | 'qa' | 'review' — a Ready card labelled `qa` has lane 'qa'
//     and skips the Builder; every other Ready card is built first (v3.0.0).
//   tier: 'low' | 'medium' | 'high' (optional, default 'medium'),  // run model ladder
//   deploy: true (optional) — config.deploy is set and deploy.branch is the base
//     branch: every card the Reviewer merges gets the post-merge dev check.
// }
// HUB BOARDS (hub.md → Run). configPath is the hub config and each card is one
// member's: the planner adds { member, configPath, checkout, baseBranch, deploy }.
// Every prompt about that card names the card's own config, repo, checkout and base
// branch; its worktrees and branch carry the member key, so `issue-7` of two members
// never share a folder or a branch. A card without `member` is a single-repo card and
// gets exactly the prompts it always did.
// The harness can deliver `args` as a JSON-encoded string (the tool param is
// untyped) — normalize before validating.
const input = (() => {
  if (typeof args !== 'string') return args
  try { return JSON.parse(args) } catch { return args }
})()
if (!input || !Array.isArray(input.cards) || !input.configPath) {
  throw new Error('super-board-wave needs args {configPath, cards:[{number,status,title,lane}]}')
}
// `variant` was removed in v3.0.0; the planner refuses a "qa-only" config, so a
// wave launched with one is a stale caller. Refuse rather than guess.
if (input.variant && input.variant !== 'full') {
  throw new Error(`super-board-wave: variant "${input.variant}" was removed in v3.0.0 — labels route cards; run /super-board onboard to upgrade`)
}
// Lane for a card entering at Ready: the planner's `lane`, else its labels.
const labelsOf = (card) => (card.labels || []).map((l) => String(l).toLowerCase())
const readyLane = (card) => card.lane || (labelsOf(card).includes('qa') ? 'qa' : 'build')
const cfgOf = (card) => (card && card.configPath) || input.configPath
const tagOf = (card) => (card.member ? `${card.member}#${card.number}` : `#${card.number}`)
const keyOf = (card) => `${card.member || ''}#${card.number}`
const deployOn = (card) => (card.member ? Boolean(card.deploy) : Boolean(input.deploy))
// The member block of every prompt about a hub card (hub.md → Run).
const MEMBER = (card) => (card.member ? [
  `Hub board card ${card.repository}#${card.number}, member ${card.member}. Its config is ${card.configPath} (resolved for this member): pass it to every .claude/bin script, never the hub config and never a config found from the cwd.`,
  `Repo ${card.repository}: every gh issue/pr/run call names --repo ${card.repository}. Checkout ${card.checkout}: run git for this card there (git -C "${card.checkout}" …). Base branch ${card.baseBranch}.`,
  `Names carry the member key: worktrees ${card.checkout}/.claude/worktrees/${card.member}-issue-${card.number}-<build|qa|review>/, branch ${card.member}-issue-${card.number}-<slug> off ${card.baseBranch}, PR into ${card.baseBranch}. Wherever run.md says issue-<N>-…, use these.`,
  `Halt check before GitHub writes: python3 .claude/bin/super-board-github-read.py --check --config ${input.configPath} (the hub and every member).`,
] : [])
if (input.tier && !['low', 'medium', 'high'].includes(input.tier)) {
  throw new Error(`super-board-wave: unknown tier "${input.tier}" — use low | medium | high`)
}

let halted = false
// Valid form per --kind, from validate() in scripts/super-board-github-read.py (#32).
const READ_SHAPES = (cfg) => [
  `Read shapes (helper: .claude/bin/super-board-github-read.py --kind <kind> -- <gh args>):`,
  `- issue title+body: --kind issue -- issue view <N> --repo {owner}/{repo} --json number,title,body`,
  `- issue comments: --kind comments -- api repos/{owner}/{repo}/issues/<N>/comments --paginate --slurp (zero comments = [[]])`,
  `- other JSON, incl. maybe-empty lists: --kind json -- <gh args> (no --jq that prints a plain string)`,
  `- PR branch+head: --kind head -- pr view <N> --repo {owner}/{repo} --json headRefName,headRefOid --jq '"\\(.headRefName) \\(.headRefOid)"'`,
  `- PR body+head: --kind body -- pr view <N> --repo {owner}/{repo} --json body,headRefOid`,
  `- PR metadata: --kind metadata -- pr view <N> --repo {owner}/{repo} --json files,labels,body,additions,deletions,changedFiles`,
  `- issue state: --kind state -- issue view <N> --repo {owner}/{repo} --json state --jq .state`,
  `A read whose output misses its shape exits 64 for that call only and prints the valid form: fix the read, do not halt.`,
  // A lane once queried a guessed repo name (owner + folder name) and halted the run (seen 2026-10-18).
  `{owner}/{repo} (REST paths, GraphQL owner/name) is exactly the owner/name in repo.remote of ${cfg}; never derive it from folder names, the git root or memory. Pass --repo {owner}/{repo} on every gh issue/pr/run command, wherever it runs.`,
].join('\n')
const READ_FAILURE = (cfg = input.configPath) => READ_SHAPES(cfg) + '\n' + `Verification-readiness exit 9 means missing/unusable config.verify_commands: preserve card/worktree/approval/claims, report status=halted (preflight verdict=halted), stop dispatch, repair project configuration before explicit restart; no Build bounce or automatic retries. ` + `Required GitHub reads use .claude/bin/super-board-github-read.py: three total attempts, exit 79 means run halted. Check --check before any GitHub write, migration, or merge. On exit 79 preserve worktree/card/approval/claims, make no more GitHub calls, and report status=halted (preflight verdict=halted). Never retry a mutation. Config: ${cfg}.`

const CLASSIFY_SCHEMA = {
  type: 'object',
  properties: {
    kind: { type: 'string', enum: ['feature', 'bug', 'qa'] },
    status: { type: 'string', enum: ['ok', 'halted'] },
    complexity: { type: 'string', enum: ['low', 'medium', 'high'] },
  },
  required: ['kind', 'complexity'],
}

const STAGE_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['advanced', 'bounced', 'blocked', 'human-gate', 'failed', 'halted'] },
    column: { type: 'string' },
    detail: { type: 'string' },
    prUrl: { type: 'string' },
    branch: { type: 'string' },
    // Review lane only, on a re-review: how round 1 graded the prior report.
    priorFindings: {
      type: 'object',
      properties: {
        fixed: { type: 'integer' },
        notFixed: { type: 'integer' },
        noLongerApplies: { type: 'integer' },
      },
    },
  },
  required: ['status', 'column', 'detail'],
}

const LANE = {
  build:  { skill: 'super-build',  section: 'Builder',  phase: 'Build' },
  qa:     { skill: 'super-qa',     section: 'Tester',   phase: 'QA' },
  review: { skill: 'super-review', section: 'Reviewer', phase: 'Review' },
}

// The merge-race guard used to live here, as a promise chain around the whole
// Review lane. It is gone on purpose (2026-08-20).
//
// It guarded the right thing in the wrong place. Squash-merges into one base
// branch do race — but serialising the entire lane also serialised reading the
// diff, rerunning the suite and the truth-check, none of which touch the base.
// On a wide wave that made Review the throughput ceiling for work that could
// have run in parallel. It was also invisible to the `claude-p` backend and to a
// second orchestrator on another machine, because a JS promise cannot be seen
// from another process.
//
// The mutex now sits at the merge step itself, in super-board-merge-gate.sh, as
// an atomic mkdir lock the Reviewer takes for the seconds it needs — and the
// freshness check runs INSIDE that lock, so nothing can move the base between
// the proof and the merge. See run.md → Reviewer lifecycle step 5.

// Review remembers: a card back in Review after a bounce is graded against the
// last Reviewer report first (run.md → Reviewer step 3b). The lookup is one gh
// call by marker, so the lane does it — this script never sees PR comments.
const REVIEW_MEMORY = [
  `Before reviewing, load prior_report: the newest PR comment containing "<!-- super-review:report -->" (run.md → Reviewer step 3b).`,
  `None → first review, behave as usual. Found → round 1 marks each prior finding fixed / not fixed / no longer applies;`,
  `any not fixed → bounce again listing them. Edit that report comment in place (keep R-ids), never post a second one. Report the counts as priorFindings.`,
  // Independent pass (run.md → Reviewer step 3): hypotheses before the builder's account.
  `Read the issue ACs and the diff before the builder's PR summary; form your own hypotheses, then check their claims.`,
  `Class every finding Gap / Bug / Verification miss / Scope drift / Over-engineering, and list what you verified correct.`,
  // Simplest-solution pass (super-review step 3): Should fix at most, never a bounce on its own.
  `Run ponytail:ponytail-review on the merge-base diff (inline ladder if the plugin is absent); Over-engineering never blocks merge alone.`,
  // Truth-check brief is fixed (super-review → Adversarial mode); a hand-written one once made over-building blocking.
  `Truth-check sub-agents get the fixed brief from super-review → "Adversarial mode", verbatim. After collecting, reclass any Over-engineering Blocker to Should fix; it never decides merge or bounce.`,
  // Merge policy + migrations live in the gate (run.md → Merge protocol step 5).
  `Merge only via super-board-merge-gate.sh. Exit 7 = merge_policy says a human merges (money/auth/schema/deploy/size): Blocked with the 🙋 template quoting its human-gate lines; To unblock = merge it yourself, or comment done to approve. Add label needs-human to issue and PR (display only; remove it once merged or approved).`,
  `Gate stdout FLAKY: {cmd,test,fingerprint} lines (any exit) = a test passed only on rerun: after the gate returns, file each with super-qa-file-bug.sh --kind bug --label flaky --fingerprint <fingerprint> (super-review SKILL.md → Merge protocol).`,
  `Exit 8 = 🙋 needs you (migration for a DB the robot may not touch, failed migrate, declared human step): Blocked with the 🙋 template, exact commands from its needs-you lines, label needs-you.`,
]

// GitHub budget (rate-limit-etiquette.md → "Price list"). Measured 2026-10-04: a
// 3-card wave spent ~2,850 of 5,000 GraphQL points/hr, nearly all of it lanes
// finding a card with `gh project item-list` (~203 points on a 131-card board)
// and its Status options with `gh project field-list` (101) before each move.
const BOARD_IO = (cfg = input.configPath) =>
  `Board moves and reads: .claude/bin/super-board-card.sh --config ${cfg} move <issue> <Status> ` +
  `(1 GraphQL point) · status <issue> · items. Comments, labels, PR reads: REST per .claude/skills/super-board/references/rate-limit-etiquette.md → "Price list". Read GitHub once per decision, never in a sleep loop.`

// The orchestrator resolves every remaining chain before claims. The runtime has
// no subprocess API; each worker repeats the deterministic CLI before its writes.
const SKILLS_CHECK = (cards, lane) => [
  `First operation, before worktree creation, classification, comments, labels or moves: run python3 .claude/bin/super-board-skills-ready.py --root "$PWD" --host claude --cards - --hydrate${lane ? ' --lane ' + lane : ''},`,
  `with this JSON array as stdin (data, never interpolate a body into shell code): ${JSON.stringify(cards)}.`,
  `Exit 10 means status=halted (preflight verdict=halted), detail beginning "skills-readiness:"; preserve all existing claims/worktrees/approval evidence. Exit 64 means invalid input: halt and repair it. Exit 79 uses the read halt policy.`,
  `Load the exact resolved_path files. Only declared fallback IDs are allowed. Before using a conditional/custom helper repeat this CLI with --require <exact-name>.`,
].join('\n')

// QA targets (run.md → Tester step 2): pass one runs the PR's app locally when
// config.qa.local is set, else tests the configured target URLs. The lane's Bash
// calls are fresh shells, so the target script reprints the env on demand.
const QA_TARGETS = (card) => {
  const cmd = `bash .claude/bin/super-board-qa-target.sh`
  const opts = `--config ${cfgOf(card)} --issue ${card.number}`
  return [
    `QA targets (run.md → Tester step 2): config qa.local set → ${cmd} start ${opts} --worktree <your qa worktree>, then eval "$(${cmd} env ${opts})" at the top of every later Bash call.`,
    `start exit 4 = the PR's app never became healthy: QA fail (bounce to Ready with the log path), not a Block. ALWAYS run ${cmd} stop ${opts} before you exit — pass, fail, block or error.`,
    `No qa.local → ${cmd} check --config ${cfgOf(card)}; exit 3 = a target is unhealthy → Block. Targets reach your tests as SB_TARGET_URL / SB_TARGET_<NAME> / BASE_URL.`,
    `qa.auth_command set → eval "$(${cmd} auth ${opts})" and log in from SB_AUTH_FILE (JSON or Playwright storageState) instead of the Supabase admin-key bootstrap. Never print that file.`,
  ]
}

const lanePrompt = (lane, card) => [
  SKILLS_CHECK([card], lane),
  READ_FAILURE(cfgOf(card)),
  `Run ${LANE[lane].skill} on issue ${card.member ? card.repository : ''}#${card.number} ("${card.title}") for a super-board workflow wave.`,
  `Read .claude/skills/super-board/references/run.md → "${LANE[lane].section}" lifecycle and follow it EXACTLY:`,
  `create your own worktree under .claude/worktrees/, work on the issue branch, post the required PR/issue comments,`,
  `move the project card yourself, clean up the worktree on exit. Config: ${cfgOf(card)}.`,
  ...MEMBER(card),
  BOARD_IO(cfgOf(card)),
  `Commits, PR title, PR body blocks, comments: .claude/skills/super-board/references/writing-standard.md (config writing.commit_format "conventional" → conventional headers, check with .claude/bin/super-board-commit-header.py). Rewrite only your own PR body blocks, with .claude/bin/super-board-pr-body.sh.`,
  ...(lane === 'review' ? REVIEW_MEMORY : []),
  ...(lane === 'review' && deployOn(card)
    ? [`Do not run the post-merge dev check yourself: after you return Done, this wave's Deploy step runs it.`]
    : []),
  ...(lane === 'qa' ? QA_TARGETS(card) : []),
  ...(lane === 'qa' && card.status === 'Ready'
    ? [`This card is labelled qa: it skips Building. Move it Ready → QA yourself, create the issue branch from the base branch, and test what is already there (run.md → "qa cards").`]
    : []),
  ``,
  `Report your exit via structured output:`,
  `- status=advanced  → card moved forward (Building→QA, QA→Review, Review→Done/merged)`,
  `- status=bounced   → card moved backward (QA fail → Ready, Reviewer bounce → Ready/QA)`,
  `- status=blocked or human-gate → you wrote the Block template and moved the card to Blocked`,
  `- status=halted    → required service evidence or verification configuration is unavailable; leave card state unchanged and stop the run`,
  `- status=failed    → you could not complete the lifecycle (say why in detail)`,
  `column = the column the card is in when you exit. detail = one line. Include prUrl/branch when they exist.`,
].join('\n')

// Run-tier model ladders. Card complexity indexes into the active ladder;
// undefined = inherit the session model (the strongest available — e.g.
// Fable/Opus). Cards entering past Ready (cls null, never classified)
// always inherit the session model.
//   low    (run --low):  haiku / sonnet / opus
//   medium (default):    sonnet / opus / session
//   high   (run --high): opus for every card
const LADDERS = {
  low: { low: 'haiku', medium: 'sonnet', high: 'opus' },
  medium: { low: 'sonnet', medium: 'opus', high: undefined },
  high: { low: 'opus', medium: 'opus', high: 'opus' },
}
const ladder = LADDERS[input.tier || 'medium']
const tierFor = (cls) => (cls ? ladder[cls.complexity] : undefined)
// The classify router writes no code — haiku is fine except on --high runs.
const classifyModel = (input.tier || 'medium') === 'high' ? 'sonnet' : 'haiku'

const runLane = async (lane, card, model, history) => {
  if (halted) {
    const result = { status: 'halted', column: card.status, detail: 'run halted; preserve work and repair the reported readiness or evidence problem' }
    history.push({ lane, ...result })
    return result
  }
  const r = await agent(lanePrompt(lane, card), {
    label: `${lane}:${tagOf(card)}`,
    phase: LANE[lane].phase,
    schema: STAGE_SCHEMA,
    ...(model ? { model } : {}),
  })
  const result = r || { status: 'failed', column: 'unknown', detail: `${lane} agent returned no result` }
  if (result.status === 'halted') halted = true
  history.push({ lane, ...result })
  return result
}

// Builder pre-flight (run.md → "Builder pre-flight"). Before ANY Ready card
// reaches runLane('build'), a fresh cheap agent asks: already merged? already
// being built (open PR or another card)? same files as an open PR? unclear?
// One agent per batch of PREFLIGHT_BATCH cards, so the three gh list calls in
// super-board-preflight.sh are paid once per batch, not once per card.
// Outcomes: proceed · hold (agent wrote the Block template, card → Blocked) ·
// sequence (card → Blocked with `blocked-by:` the overlapping PR's issue, so the
// wave-start sweep frees it; or proceed with a conflict note when that PR closes
// no issue) · skipped (pre-flight blind — card stays Ready for the next wave).
// A card the agent did not answer for is skipped, never built unchecked.
const PREFLIGHT_SCHEMA = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          number: { type: 'integer' },
          verdict: { type: 'string', enum: ['proceed', 'hold', 'sequence', 'skipped', 'halted'] },
          column: { type: 'string' },
          blockedBy: { type: 'array', items: { type: 'integer' } },
          detail: { type: 'string' },
        },
        required: ['number', 'verdict', 'detail'],
      },
    },
  },
  required: ['verdicts'],
}
const PREFLIGHT_BATCH = 5
const preflightModel = (input.tier || 'medium') === 'low' ? 'haiku' : 'sonnet'
// One batch = one member's cards (one config, one repo): a hub wave never mixes them.
const preflightPrompt = (batch) => [
  SKILLS_CHECK(batch),
  READ_FAILURE(cfgOf(batch[0])),
  `Builder pre-flight for issues ${batch.map((c) => `#${c.number} ("${c.title}")`).join(', ')}. Config: ${cfgOf(batch[0])}.`,
  `Read .claude/skills/super-board/references/run.md → "Builder pre-flight" and follow it EXACTLY. You write no code.`,
  `Other cards in this wave: ${JSON.stringify(input.cards
    .filter((c) => (c.member || '') === (batch[0].member || ''))
    .map(({ number, status, title }) => ({ number, status, title })))}`,
  `Run .claude/bin/super-board-preflight.sh once for the whole batch, then judge its candidates semantically.`,
  ...(batch[0].member ? [
    `Hub board: these are member ${batch[0].member}'s cards in ${batch[0].repository}. Run the pre-flight with --repo ${batch[0].repository} --member ${batch[0].member} (its own branch is ${batch[0].member}-issue-<N>-*), every gh call with --repo ${batch[0].repository}.`,
    `Halt check before GitHub writes: python3 .claude/bin/super-board-github-read.py --check --config ${input.configPath} (the hub and every member).`,
  ] : []),
  `hold / sequence-behind → post the Block template (block-template.md) and move the card to Blocked yourself.`,
  `Two Ready cards overlapping each other with no open PR: the lower-numbered one proceeds; sequence only the other, blockedBy [the lower]. Never both.`,
  `A sequence with nothing to wait on (empty blockedBy) leaves the card in Ready: return column Ready and it builds.`,
  BOARD_IO(cfgOf(batch[0])),
  `Return one verdict per issue: proceed | hold | sequence | skipped (pre-flight blind). Never drop a card silently.`,
].join('\n')

// Only cards the Builder will take are pre-flighted; `qa` cards skip Building.
const ready = input.cards.filter((c) => c.status === 'Ready' && readyLane(c) === 'build')
const verdicts = new Map()
const batches = []
const byMember = new Map()
for (const c of ready) byMember.set(c.member || '', [...(byMember.get(c.member || '') || []), c])
for (const group of byMember.values()) {
  for (let i = 0; i < group.length; i += PREFLIGHT_BATCH) batches.push(group.slice(i, i + PREFLIGHT_BATCH))
}
await Promise.all(batches.map(async (batch) => {
  const r = await agent(preflightPrompt(batch), {
    label: `preflight:${batch.map(tagOf).join(',')}`,
    phase: 'Pre-flight',
    model: preflightModel,
    schema: PREFLIGHT_SCHEMA,
  })
  // Verdicts name a number; within one batch (one member) that is one card.
  for (const v of (r && r.verdicts) || []) {
    const card = batch.find((c) => c.number === v.number)
    if (card) verdicts.set(keyOf(card), v)
    if (v.verdict === 'halted') halted = true
  }
}))
// sequence with nothing to wait on = proceed with a conflict note (the agent posted it).
// No column and an empty blockedBy is the same case: nothing to wait on never parks a
// card (2026-10-03: #180/#181 each "sequenced" behind the other, neither built).
const preflightGo = (card) => {
  const v = verdicts.get(keyOf(card))
  if (!v) return false
  if (v.verdict === 'proceed') return true
  return v.verdict === 'sequence' && (v.column === 'Ready' ||
    (!v.column && Array.isArray(v.blockedBy) && v.blockedBy.length === 0))
}
const preflightExit = (card) => {
  const v = verdicts.get(keyOf(card)) ||
    { verdict: 'skipped', detail: 'pre-flight returned no verdict — not built unchecked; retried next wave' }
  return {
    lane: 'preflight',
    status: v.verdict === 'halted' ? 'halted' : v.verdict === 'skipped' ? 'failed' : 'blocked',
    column: v.column || (['skipped', 'halted'].includes(v.verdict) ? 'Ready' : 'Blocked'),
    detail: `${v.verdict}: ${v.detail}`,
  }
}

// Post-merge dev check (run.md → "Post-merge dev check"). QA pass two: after the
// Reviewer merges into the deploy branch, a cheap agent waits for the deploy
// workflow run of the merge commit, checks health + version and runs smoke —
// all inside super-board-deploy-wait.sh. A failure files a bug card linked to
// this card and PR; the PR stays merged and the card stays Done. The script's
// `post_deploy_checks` function is where later checks on the deployed build go.
const DEPLOY_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['healthy', 'skipped', 'failed', 'halted'] },
    bug: { type: 'integer' },
    detail: { type: 'string' },
  },
  required: ['status', 'detail'],
}
const deployModel = (input.tier || 'medium') === 'low' ? 'haiku' : 'sonnet'
const deployPrompt = (card, review) => [
  SKILLS_CHECK([card]),
  READ_FAILURE(cfgOf(card)),
  `Post-merge dev check for issue ${card.member ? card.repository : ''}#${card.number} ("${card.title}"), merged PR ${review.prUrl || '(the merged PR that closes this issue)'}. Config: ${cfgOf(card)}.`,
  ...MEMBER(card),
  `Read .claude/skills/super-board/references/run.md → "Post-merge dev check" and follow it. You write no code and move no card.`,
  `Run: bash .claude/bin/super-board-deploy-wait.sh --config ${cfgOf(card)} --pr <PR number> --issue ${card.number} --budget-s 540`,
  `Exit 75 = still waiting: run the same command again (the timeout clock carries over) until it exits with anything else.`,
  `Exit 0 → status healthy, or skipped when its output says skipped. 10 → failed, bug = the number after "bug: #". 11 → failed, no bug filed (say why). 79 → halted.`,
  `In every case the PR stays merged and the card stays Done: never revert, reopen, block or move anything. detail = the script's last line.`,
].join('\n')
const runDeploy = async (card, review) => {
  if (halted) return { status: 'halted', detail: 'run halted before the post-merge dev check' }
  const r = await agent(deployPrompt(card, review), {
    label: `deploy:${tagOf(card)}`,
    phase: 'Deploy',
    model: deployModel,
    schema: DEPLOY_SCHEMA,
  })
  const result = r || { status: 'failed', detail: 'deploy agent returned no result' }
  if (result.status === 'halted') halted = true
  return result
}
const merged = (r) => r && r.status === 'advanced' && String(r.column || '').toLowerCase() === 'done'

const results = await pipeline(
  input.cards,
  // Stage 1: classify cards entering at Ready (router for model tiering)
  async (card) => {
    if (halted) return { card, cls: null }
    if (card.status !== 'Ready') return { card, cls: null }
    const labels = labelsOf(card)
    const typed = ['qa', 'bug', 'feature'].find((l) => labels.includes(l))
    const cls = await agent(
      SKILLS_CHECK([card]) + '\n' + READ_FAILURE(cfgOf(card)) + '\n' + MEMBER(card).map((l) => l + '\n').join('') + `Read GitHub issue #${card.number} ("${card.title}") — body (--kind issue) and all comments (--kind comments) — with the read shapes above. ` +
      `Classify it: kind (feature|bug|qa) and complexity (low|medium|high) judged by the scope of change required. ` +
      (typed
        ? `Its label says "${typed}": return kind "${typed}" — the label routes the card, you never override it.`
        : `It has no type label, so it is built (no label = Building). Return feature or bug — never qa, ` +
          `which only a person sets. Existing docs/ux/tests/refactor/tech-debt rows keep their label chain: classify complexity without adding feature/bug. For a truly untyped card, re-run skills-ready against a copy of this card with the proposed label before any label write, then add that label: gh issue edit ${card.number}${card.repository ? ` --repo ${card.repository}` : ''} --add-label <kind>.`),
      { label: `classify:${tagOf(card)}`, phase: 'Classify', model: classifyModel, schema: CLASSIFY_SCHEMA }
    )
    if (cls && cls.status === 'halted') halted = true
    return { card, cls }
  },
  // Stage 2: lane chain — entry point depends on the card's current column.
  // A non-'advanced' exit ends the chain; the next wave re-selects the card
  // from wherever it landed (the board is the loop state, not this script).
  async (prev, card) => {
    const history = []
    const who = { number: card.number, ...(card.member ? { member: card.member, repository: card.repository } : {}) }
    if (halted) return { ...who, history: [{ lane: 'none', status: 'halted', column: card.status, detail: 'run halted; preserve current work and resume explicitly' }] }
    const model = tierFor(prev && prev.cls)
    let at = card.status

    if (at === 'Ready' && readyLane(card) === 'build') {
      if (!preflightGo(card)) {
        history.push(preflightExit(card))
        return { ...who, history }
      }
      const b = await runLane('build', card, model, history)
      if (b.status !== 'advanced') return { ...who, history }
      at = 'QA'
    }
    // A `qa`-labelled card skips Building: Ready → QA, Tester first
    // (run.md "Lanes and label routing").
    if (at === 'Ready' && readyLane(card) === 'qa') at = 'QA'
    if (at === 'QA') {
      const q = await runLane('qa', card, model, history)
      if (q.status !== 'advanced') return { ...who, history }
      at = 'Review'
    }
    if (at === 'Review') {
      // Reviewer always on session model. No lane-level serialisation: the merge
      // gate holds the mutex for the merge alone (see the note above).
      const rv = await runLane('review', card, undefined, history)
      if (deployOn(card) && merged(rv)) {
        return { ...who, history, deploy: await runDeploy(card, rv) }
      }
    }
    return { ...who, history }
  }
)

const summary = results.filter(Boolean).map((r) => {
  const last = r.history[r.history.length - 1] ||
    { lane: 'none', status: 'failed', column: 'unknown', detail: 'no lane ran' }
  return {
    number: r.number,
    // Hub cards: which member and repo (the wave report groups by member).
    ...(r.member ? { member: r.member, repository: r.repository } : {}),
    finalStatus: last.status,
    lastLane: last.lane,
    column: last.column,
    detail: last.detail,
    prUrl: last.prUrl || null,
    priorFindings: last.priorFindings || null,
    // The dev check never changes finalStatus: a merged card stays Done.
    deploy: r.deploy || null,
    lanesRun: [...r.history.map((h) => `${h.lane}:${h.status}`), ...(r.deploy ? [`deploy:${r.deploy.status}`] : [])].join(' → ') || 'none',
  }
})
// One line, grouped by member on a hub wave (`backend: #7=advanced@Done · frontend: …`).
const groups = new Map()
for (const s of summary) groups.set(s.member || '', [...(groups.get(s.member || '') || []), s])
log(`wave complete: ${summary.length} cards — ` + [...groups.entries()].map(([m, list]) =>
  (m ? `${m}: ` : '') + list.map((s) => `#${s.number}=${s.finalStatus}@${s.column}`).join(', ')).join(' · '))
return { cards: summary, halted }
