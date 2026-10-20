#!/usr/bin/env bash
# Hub wave (hub.md → Run): one workflow wave drains cards of several members. No gh
# calls: the script body runs under node with agent/pipeline/log stubbed.
#
# The rule: every prompt about a member card names that card's own resolved config,
# repo, checkout and base branch, and its worktrees/branch carry the member key, so
# backend #7 and frontend #7 never meet. A single-repo card's prompts are unchanged.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const run = (args, verdicts = {}) => {
  const labels = [], prompts = {}, logs = []
  const agent = async (prompt, opts) => {
    labels.push(opts.label); prompts[opts.label] = prompt
    if (opts.label.startsWith('preflight')) {
      // label preflight:backend#7,backend#8 → verdicts keyed by the same tag
      const tags = opts.label.slice('preflight:'.length).split(',')
      return { verdicts: tags.filter((t) => verdicts[t]).map((t) => ({ number: Number(t.split('#')[1]), ...verdicts[t] })) }
    }
    if (opts.label.startsWith('classify')) return { kind: 'feature', complexity: 'low' }
    if (opts.label.startsWith('deploy')) return { status: 'healthy', detail: 'ok' }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  return new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(args, agent, pipeline, (l) => logs.push(l))
    .then((out) => ({ out, labels, prompts, logs }))
}
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }
const member = (key, repo, base, deploy) => ({
  member: key, repository: repo, configPath: `/hub/.claude/super-board/resolved/pc.${key}.json`,
  checkout: `/hub/${key}`, baseBranch: base, deploy })
const BE = member('backend', 'acme/api', 'dev', true)
const FE = member('frontend', 'acme/web', 'main', false)
const card = (m, number, status = 'Ready', extra = {}) => ({ number, status, title: `t${number}`, lane: status === 'Ready' ? 'build' : undefined, ...m, ...extra })

;(async () => {
  // 1 — backend #7 and frontend #7 in one wave: two chains, two labels, never one.
  const { out, labels, prompts, logs } = await run({ configPath: '/hub/.claude/super-board/configs/pc.json',
    cards: [card(BE, 7), card(FE, 7), card(FE, 9, 'Review')] },
    { 'backend#7': { verdict: 'proceed', detail: 'clean' }, 'frontend#7': { verdict: 'proceed', detail: 'clean' } })
  for (const l of ['build:backend#7', 'build:frontend#7', 'qa:backend#7', 'qa:frontend#7', 'review:frontend#9'])
    labels.includes(l) || fail(`missing lane ${l}: ${labels}`)
  // 2 — pre-flight batches never mix members: one agent per member, each with its repo.
  const pf = labels.filter((l) => l.startsWith('preflight'))
  ;(pf.length === 2 && pf.includes('preflight:backend#7') && pf.includes('preflight:frontend#7')) || fail('one pre-flight per member: ' + pf)
  ;/--repo acme\/api --member backend/.test(prompts['preflight:backend#7']) || fail('backend pre-flight names its repo and member')
  ;/pc\.backend\.json/.test(prompts['preflight:backend#7']) && !/pc\.frontend\.json/.test(prompts['preflight:backend#7'])
    || fail('backend pre-flight uses the backend config only')
  // 3 — the lane prompt names the card's own config, repo, checkout, base and member-prefixed names.
  const b = prompts['build:backend#7']
  for (const needle of ['Config: /hub/.claude/super-board/resolved/pc.backend.json', '--repo acme/api', 'git -C "/hub/backend"',
                        '/hub/backend/.claude/worktrees/backend-issue-7-<build|qa|review>/', 'backend-issue-7-<slug> off dev',
                        'super-board-card.sh --config /hub/.claude/super-board/resolved/pc.backend.json',
                        'repo.remote of /hub/.claude/super-board/resolved/pc.backend.json',
                        '--check --config /hub/.claude/super-board/configs/pc.json'])
    b.includes(needle) || fail('backend build prompt missing: ' + needle)
  ;/pc\.frontend\.json|acme\/web|frontend-issue/.test(b) && fail('backend prompt must not name the frontend')
  const f = prompts['build:frontend#7']
  ;(f.includes('frontend-issue-7-<slug> off main') && f.includes('--repo acme/web')) || fail('frontend prompt uses its own base and repo')
  // 4 — QA targets and classify take the member config.
  prompts['qa:backend#7'].includes('super-board-qa-target.sh start --config /hub/.claude/super-board/resolved/pc.backend.json --issue 7')
    || fail('QA target uses the member config')
  ;/--repo acme\/api --add-label/.test(prompts['classify:backend#7']) || fail('classifier labels in the member repo')
  // 5 — deploy per member: backend's flag on, frontend's off, whatever the wave flag says.
  const dep = await run({ configPath: 'hub.json', cards: [card(BE, 3, 'Review'), card(FE, 4, 'Review')] })
  dep.labels.includes('deploy:backend#3') || fail('backend deploys after merge: ' + dep.labels)
  dep.labels.includes('deploy:frontend#4') && fail('frontend has no deploy on its base')
  dep.prompts['deploy:backend#3'].includes('super-board-deploy-wait.sh --config /hub/.claude/super-board/resolved/pc.backend.json')
    || fail('deploy waits on the member config')
  // 6 — the summary carries member and repo; the log groups by member.
  const s = out.cards.find((c) => c.member === 'frontend' && c.number === 7)
  ;(s && s.repository === 'acme/web' && s.finalStatus === 'advanced') || fail('summary carries member + repo: ' + JSON.stringify(out.cards))
  ;/backend: #7=advanced@Done · frontend: #7=advanced@Done, #9=advanced@Done/.test(logs.join('\n')) || fail('log groups by member: ' + logs)
  // 7 — a held verdict for frontend #7 never stops backend #7 (verdicts keyed per member).
  const held = await run({ configPath: 'hub.json', cards: [card(BE, 7), card(FE, 7)] },
    { 'backend#7': { verdict: 'proceed', detail: 'clean' }, 'frontend#7': { verdict: 'hold', column: 'Blocked', detail: 'dup' } })
  ;(held.labels.includes('build:backend#7') && !held.labels.includes('build:frontend#7')) || fail('verdicts are per member: ' + held.labels)
  // 8 — single-repo cards are unchanged: no member block, wave config everywhere.
  const one = await run({ configPath: 'c.json', cards: [{ number: 5, status: 'Review', title: 't5' }] })
  const r = one.prompts['review:#5']
  ;(r && r.includes('Config: c.json') && !/Hub board|member/.test(r)) || fail('single-repo prompt unchanged: ' + r)
  ;(one.out.cards[0].member === undefined) || fail('single-repo summary has no member')
})().catch((e) => fail(e.stack))
JS
echo "PASS: test-wave-hub.sh (8 scenarios)"
