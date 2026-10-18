# super-board triage

`/super-board triage [#n …]` turns thin cards into cards a lane can build. With no numbers it
takes every open issue labelled `needs-triage`; with numbers it takes exactly those.

It edits tickets; it does no product work. That keeps it on the orchestrator side of the
cardinal rule: it reads code to understand the problem, it never changes code, branches or PRs.

## Who sets `needs-triage`

| Where | When |
|---|---|
| Wave planner (`super-board-wave-plan.sh`) | a free Ready card fails `super-board-card-ready.py`: it is reported in `triage` and not dispatched; the orchestrator adds the label and leaves it in Ready (run-workflow.md step 2) |
| super-collect (`super-collect-file.sh`) | the body it files fails card-ready |
| a human | any time |

The label is display and routing only. The planner re-checks every Ready card on every wave, so
a card leaves `triage` the moment its body passes, label or not.

## What "ready" means

`python3 .claude/bin/super-board-card-ready.py --from <issue.json>` (issue = `{number, title, body,
labels}` from `super-board-github-read.py --kind issue -- issue view <N> --json number,title,body,labels`):

- build cards: Problem · Context · Fix · Acceptance Criteria · Risk · Blocked by, each non-empty
  (writing-standard.md § 3); `bug` cards also need Evidence holding a log, stack, link or repro step;
- `qa` cards: Problem · Acceptance Criteria · a target (a Where line, URL or route);
- at least `triage.checkable_ac_min` (default 1) checkable `- [ ]` criteria. "works", "is fixed",
  "looks good" and friends are not checkable (the list is `VAGUE_AC` in the script);
- `## Blocked by` parseable by `super-board-deps.sh`, the same rules the planner uses.

Exit 0 ready, 3 not ready (the JSON names `missing` and `hints`), 64 bad input.

## Steps, per card

1. **Check.** Run card-ready. Ready already → step 6.
2. **Read.** The body, comments, linked PRs, and the evidence the card points at: the stack
   frame's file, the route, the log sample, the trace. Read the code the evidence names.
3. **Rewrite the body** into the ticket format (writing-standard.md § 3), keeping every fact of
   the original. Fill only what the evidence or the code shows:
   - Problem, Context (Where + lettered steps, Who), Evidence rows, Fix (the intended change),
     Risk (from the blast radius you can see), `## Blocked by` (`- None.` only when nothing
     on the board gates it; else `- #N — why`).
   - Acceptance Criteria only for behaviour the evidence fixes ("the 500 no longer happens on an
     empty cart"). NEVER invent criteria for business behaviour (prices, limits, permissions,
     copy, what a user should see): ask.
   - Keep the original text verbatim at the end, folded:

     ```
     <details><summary>Original</summary>

     <the body exactly as it was>

     </details>
     ```

   Keep every machine line (`<!-- super-collect-fingerprint … -->`, `<!-- sb-signature … -->`,
   `<!-- super-qa-meta … -->`) outside the fold, unchanged. Edit with
   `gh issue edit <N> --body-file <file>`, once.
4. **Ask** what you could not derive in ONE comment (writing-standard.md § 4), numbered
   questions, nothing else. Skip it when nothing is missing. Do not post a second question
   comment while one is unanswered; edit it.

   ```
   [triage] [needs-info] 🙋 2 questions before this card can be built
   Did: rewrote the body into the ticket format; original kept under Original
   ❌ Not done: Acceptance Criteria for the business rule, Risk
   Next: Eric
   1. What should a shopper see when the coupon has expired: an error, or the full price?
   2. Should an expired coupon still count toward the per-user limit?
   ```

5. **Re-check** with card-ready. Still not ready → the card keeps `needs-triage` and stays where
   it is (Backlog, or Ready, where the planner keeps holding it out of every wave).
6. **Ready.** Remove `needs-triage`. A card already in Ready is done: the next wave picks it up.
   A card in Backlog:
   - `triage.auto_ready: true` → move it to Ready (`.claude/bin/super-board-card.sh move <N> Ready`).
   - default (`false`) → leave it in Backlog and comment
     `[triage] [report] ✅ ready for a human to move · card-ready passes` /
     `Did: rewrote the body; no open questions` / `Next: Eric`.

Report one line per card to the user: `#N ready | needs-info (k questions) | unchanged`.

## Config

```
"triage": { "enabled": true, "auto_ready": false, "checkable_ac_min": 1 }
```

`enabled: false` turns off the planner's check and super-collect's label; `/super-board triage`
still works when typed.
