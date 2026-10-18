#!/usr/bin/env python3
"""super-board-merge-policy.py category defaults: who merges a PR.

Pure function of config + PR metadata + diff, so these run the CLI on temp
files. The categories decide when a human merges; a miss here merges an auth or
deploy change unreviewed, so the defaults are pinned one path at a time.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts/super-board-merge-policy.py"


def policy(paths, config=None, labels=()):
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "c.json").write_text(json.dumps(config or {"base_branch": "staging"}), encoding="utf-8")
        meta = {"labels": [{"name": l} for l in labels], "body": "", "additions": 1, "deletions": 0,
                "files": [{"path": p, "additions": 1, "deletions": 0} for p in paths]}
        (work / "m.json").write_text(json.dumps(meta), encoding="utf-8")
        (work / "d.diff").write_text("", encoding="utf-8")
        out = subprocess.run([sys.executable, str(POLICY), "--config", str(work / "c.json"),
                              "--meta", str(work / "m.json"), "--diff", str(work / "d.diff")],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout
        return json.loads(out)


def categories(result):
    return sorted({h["category"] for h in result["human"]})


class CategoryDefaults(unittest.TestCase):
    def test_dotnet_pascalcase_auth_folders_are_human(self):
        for path in ["src/Modules/Users/Authentication/LoginHandler.cs",
                     "src/Api/Authorization/PolicyProvider.cs",
                     "src/Infrastructure/Identity/UserStore.cs",
                     "src/Auth/TokenService.cs"]:
            with self.subTest(path=path):
                self.assertEqual(categories(policy([path])), ["auth"])

    def test_category_paths_ignore_case_both_ways(self):
        self.assertEqual(categories(policy(["web/src/AUTH/x.ts"])), ["auth"])
        self.assertEqual(categories(policy(["web/src/features/identity/x.tsx"])), ["auth"])
        self.assertEqual(categories(policy(["src/Billing/Invoice.cs"])), ["money"])

    def test_deploy_files_are_human(self):
        for path in [".github/workflows/deploy-dev.yml", ".github/workflows/release.yml",
                     "infra/docker-compose.prod.yml", "docker-compose.dev.prod.yml",
                     "ops/deploy/k8s.yaml", "deploy/run.sh"]:
            with self.subTest(path=path):
                self.assertEqual(categories(policy([path])), ["deploy"])

    def test_ordinary_paths_stay_automatic(self):
        for path in [".github/workflows/ci.yml", "docker-compose.yml", "src/Api/Program.cs",
                     "src/Modules/Orders/OrderService.cs"]:
            with self.subTest(path=path):
                self.assertEqual(policy([path])["human"], [])

    def test_setting_always_human_replaces_every_default(self):
        cfg = {"base_branch": "staging",
               "merge_policy": {"always_human": {"money": {"paths": ["**/billing/**"]}}}}
        self.assertEqual(policy(["src/Authentication/x.cs", "deploy/x.sh"], cfg)["human"], [])
        self.assertEqual(categories(policy(["src/Billing/x.cs"], cfg)), ["money"])

    def test_migration_globs_stay_case_sensitive(self):
        # Migrations decide what RUNS, so their globs keep their exact meaning.
        self.assertEqual(policy(["db/MIGRATIONS/1.sql"])["migrations"], [])
        self.assertEqual(policy(["db/migrations/1.sql"])["migrations"], ["db/migrations/1.sql"])

    def test_needs_human_label_never_lowers_the_gate(self):
        # Display-only label: it neither satisfies nor creates a category.
        self.assertEqual(categories(policy(["src/Auth/x.cs"], labels=["needs-human"])), ["auth"])
        self.assertEqual(policy(["src/x.cs"], labels=["needs-human"])["human"], [])


if __name__ == "__main__":
    unittest.main()
