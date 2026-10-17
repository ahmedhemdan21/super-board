#!/usr/bin/env bash
# Offline workflow boundary for QA pass two: the Deploy step runs only after a
# merge, never changes the card's Done outcome, and a halt from it stops the run.
# Also: every QA lane prompt carries the local-stack start/stop contract.
set -euo pipefail
cd "$(dirname "$0")"
node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const source = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
async function run({ cards, deploy, review = 'merged', deployStatus = 'failed' }) {
  const calls = [], prompts = {}
  const agent = async (prompt, options) => {
    calls.push(options.label); prompts[options.label] = prompt
    if (options.label.startsWith('deploy:')) {
      return { status: deployStatus, bug: deployStatus === 'failed' ? 88 : undefined, detail: 'deploy: ' + deployStatus }
    }
    if (options.label.startsWith('review:')) {
      return review === 'merged'
        ? { status: 'advanced', column: 'Done', detail: 'merged', prUrl: 'https://github.com/acme/app/pull/12' }
        : { status: 'bounced', column: 'Ready', detail: 'bounced' }
    }
    return { status: 'advanced', column: 'Review', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => {
    const results = []
    for (const item of items) {
      let result = await stages[0](item)
      for (const stage of stages.slice(1)) result = await stage(result, item)
      results.push(result)
    }
    return results
  }
  const output = await new AsyncFunction('args', 'agent', 'pipeline', 'log', source)(
    { configPath: 'fixture.json', cards, ...(deploy ? { deploy: true } : {}) }, agent, pipeline, () => {})
  return { calls, prompts, output }
}
const assert = (cond, msg) => { if (!cond) throw Error(msg) }
;(async () => {
  const review = [{ number: 5, title: 'Task', status: 'Review' }]
  // 1. deploy on, merge, deploy fails → bug, card still advanced@Done.
  let r = await run({ cards: review, deploy: true })
  assert(r.calls.includes('deploy:#5'), 'deploy step must run after a merge: ' + r.calls)
  const c = r.output.cards[0]
  assert(c.finalStatus === 'advanced' && c.column === 'Done', 'a failed dev check must not change Done: ' + JSON.stringify(c))
  assert(c.deploy && c.deploy.status === 'failed' && c.deploy.bug === 88, 'summary carries the deploy outcome')
  assert(c.lanesRun.endsWith('deploy:failed'), 'manifest line shows the deploy step: ' + c.lanesRun)
  assert(!r.output.halted, 'a failed dev check is not a halt')
  assert(/super-board-deploy-wait\.sh --config fixture\.json --pr <PR number> --issue 5 --budget-s 540/.test(r.prompts['deploy:#5']), 'deploy prompt runs the script')
  assert(r.prompts['deploy:#5'].includes('pull/12'), 'deploy prompt names the merged PR')
  assert(/never revert/.test(r.prompts['deploy:#5']), 'deploy prompt forbids reverting')
  assert(/Do not run the post-merge dev check yourself/.test(r.prompts['review:#5']), 'reviewer leaves the check to the Deploy step')
  // 2. deploy off → no deploy agent, reviewer not told about it.
  r = await run({ cards: review, deploy: false })
  assert(!r.calls.some(l => l.startsWith('deploy:')), 'no deploy step without args.deploy')
  assert(!/post-merge dev check/.test(r.prompts['review:#5']), 'reviewer prompt unchanged without deploy')
  assert(r.output.cards[0].deploy === null, 'summary deploy null when off')
  // 3. no merge → no deploy.
  r = await run({ cards: review, deploy: true, review: 'bounced' })
  assert(!r.calls.some(l => l.startsWith('deploy:')), 'no deploy step when the card did not merge')
  // 4. deploy halted (exit 79) → wave halted, card summary keeps Done.
  r = await run({ cards: review, deploy: true, deployStatus: 'halted' })
  assert(r.output.halted, 'a deploy read halt stops the run')
  // 5. QA lane prompt: start, env, always stop, remote check, auth.
  r = await run({ cards: [{ number: 6, title: 'Task', status: 'QA' }], deploy: false })
  const qa = r.prompts['qa:#6']
  for (const needle of ['super-board-qa-target.sh start --config fixture.json --issue 6 --worktree',
                        'ALWAYS run bash .claude/bin/super-board-qa-target.sh stop --config fixture.json --issue 6',
                        'super-board-qa-target.sh check --config fixture.json', 'SB_TARGET_<NAME>', 'auth --config fixture.json --issue 6']) {
    assert(qa.includes(needle), 'QA prompt missing: ' + needle)
  }
  assert(!r.prompts['review:#6'].includes('qa-target'), 'only the QA lane gets the target contract')
  console.log('PASS: workflow deploy step + QA targets (5 scenarios)')
})().catch(e => { console.error(e); process.exit(1) })
JS
