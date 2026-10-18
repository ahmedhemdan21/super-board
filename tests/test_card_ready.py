"""scripts/super-board-card-ready.py: is a card written well enough to dispatch?

Offline. The `## Blocked by` verdict comes from a --deps graph, or from
super-board-deps.sh --from (real script, no network).

    python3 tests/test_card_ready.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "super-board-card-ready.py")

GOOD = """## Problem
Checkout 500s on an empty cart.

## Context
- **Where:** Checkout, `/checkout`
  - a. Empty the cart
  - b. Open `/checkout`
- **Who:** any shopper

## Evidence
| Row | Value |
|---|---|
| Error + stack | `TypeError` · `checkout.ts:12` |

## Fix
Guard the empty cart in CheckoutService.

## Acceptance Criteria
- [ ] Empty cart renders the empty-cart message, no HTTP 500

## Risk
🟢 **Low** · one guard clause.

## Blocked by
- None.
"""


def run(payload, *args, deps=None, config=None):
    extra = list(args)
    files = []
    for flag, obj in (("--deps", deps), ("--config", config)):
        if obj is not None:
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(obj, f)
            f.close()
            files.append(f.name)
            extra += [flag, f.name]
    try:
        r = subprocess.run([sys.executable, SCRIPT, *extra], input=json.dumps(payload), capture_output=True,
                           text=True, encoding="utf-8")
    finally:
        for f in files:
            os.unlink(f)
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None), r.stderr


def issue(body, labels=("bug",), number=7):
    return {"number": number, "title": "t", "body": body, "labels": list(labels)}


OK_DEPS = {"7": {"parseable": True, "why": ""}}


class CardReady(unittest.TestCase):
    def test_good_bug_is_ready(self):
        rc, out, _ = run(issue(GOOD), deps=OK_DEPS)
        self.assertEqual((rc, out), (0, {"number": 7, "ready": True, "missing": [], "hints": []}))

    def test_missing_and_empty_sections(self):
        body = GOOD.replace("## Fix\nGuard the empty cart in CheckoutService.\n", "## Fix\nTBD\n")
        body = body.replace("## Risk\n🟢 **Low** · one guard clause.\n", "")
        rc, out, _ = run(issue(body), deps=OK_DEPS)
        self.assertEqual(rc, 3)
        self.assertEqual(out["missing"], ["Fix", "Risk"])
        self.assertEqual(len(out["hints"]), 2)

    def test_vague_acceptance_criteria(self):
        for ac in ("works", "It is fixed.", "Looks good", "fast"):
            body = GOOD.replace("- [ ] Empty cart renders the empty-cart message, no HTTP 500", f"- [ ] {ac}")
            rc, out, _ = run(issue(body), deps=OK_DEPS)
            self.assertEqual(rc, 3, ac)
            self.assertIn("checkable acceptance criterion", out["missing"])
        body = GOOD.replace("- [ ]", "-")  # prose bullets are not a checklist
        self.assertIn("checkable acceptance criterion", run(issue(body), deps=OK_DEPS)[1]["missing"])

    def test_checkable_ac_min_from_config(self):
        cfg = {"triage": {"checkable_ac_min": 2}}
        rc, out, _ = run(issue(GOOD), deps=OK_DEPS, config=cfg)
        self.assertEqual(rc, 3)
        self.assertIn("checkable acceptance criterion", out["missing"])

    def test_bug_needs_evidence_with_substance(self):
        no_ev = GOOD.split("## Evidence")[0] + "## Fix" + GOOD.split("## Fix")[1]
        self.assertIn("Evidence", run(issue(no_ev), deps=OK_DEPS)[1]["missing"])
        thin = GOOD.replace("| Error + stack | `TypeError` · `checkout.ts:12` |", "| Error | it broke |")
        self.assertIn("Evidence", run(issue(thin), deps=OK_DEPS)[1]["missing"])
        self.assertEqual(run(issue(no_ev, labels=("feature",)), deps=OK_DEPS)[0], 0, "features need no Evidence")

    def test_blocked_by_uses_the_deps_verdict(self):
        rc, out, _ = run(issue(GOOD), deps={"7": {"parseable": False, "why": "the section says None and then names an issue"}})
        self.assertEqual(rc, 3)
        self.assertEqual(out["missing"], ["Blocked by"])
        self.assertIn("names an issue", out["hints"][0])
        rc, out, _ = run(issue(GOOD), deps={})
        self.assertEqual(out["missing"], ["Blocked by"], "unknown to the graph is not ready")

    def test_blocked_by_through_deps_sh(self):
        rc, out, err = run([issue(GOOD), issue(GOOD.replace("- None.", "- None — but #3 first"), number=8)])
        self.assertEqual(rc, 3, err)
        self.assertEqual([o["ready"] for o in out], [True, False])
        self.assertEqual(out[1]["missing"], ["Blocked by"])

    def test_qa_cards_have_a_lighter_rule_set(self):
        qa = "## Problem\nCheckout totals look wrong.\n\n## Acceptance Criteria\n- [ ] Total equals the sum of line items on `/checkout`\n"
        self.assertEqual(run(issue(qa, labels=("qa",)))[0], 0)
        no_target = qa.replace(" on `/checkout`", "")
        rc, out, _ = run(issue(no_target, labels=("qa",)))
        self.assertEqual((rc, out["missing"]), (3, ["target"]))

    def test_no_body_and_bad_input(self):
        rc, out, _ = run(issue(None), deps=OK_DEPS)
        self.assertEqual((rc, out["missing"]), (3, ["body"]))
        self.assertEqual(run({"title": "no number"})[0], 64)

    def test_labels_in_any_shape_and_project_item_shape(self):
        item = {"content": {"number": 7, "title": "t", "body": GOOD, "labels": [{"name": "Bug"}]}}
        self.assertEqual(run(item, deps=OK_DEPS)[0], 0)
        nodes = issue(GOOD)
        nodes["labels"] = {"nodes": [{"name": "qa"}]}
        self.assertEqual(run(nodes)[0], 0)


if __name__ == "__main__":
    unittest.main()
