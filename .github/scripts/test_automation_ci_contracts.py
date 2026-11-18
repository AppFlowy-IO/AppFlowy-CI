"""Exercise CI's automation selections with a recording Cargo shim."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from test_pr_cleanup_workflows import event_context, render_expression

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github/scripts"
MEMBERS = SCRIPTS / "cloud_member_tests.sh"
GATE = SCRIPTS / "commercial_automation_gate.sh"
DISABLED = "automation::feature_gate_test::disabled_server_rejects_all_automation_operations"
PROVIDERS = [
    "biz::automation_delivery::tests::webhook_signing_keys_are_encrypted_rotatable_and_workspace_scoped",
    "biz::automation_delivery::tests::smtp_accepts_plain_text_with_the_stable_delivery_id",
    "biz::integrations::native::runtime_tests::slack_delivery_requires_the_owner_workspace_and_a_current_write_grant",
    "biz::integrations::native::runtime_tests::gmail_delivery_requires_the_owner_scope_and_verified_sender_identity",
]
OUTBOUND = "libs/appflowy-cloud-integrations/src/biz/automation_delivery.rs"
GATE_SOURCE = "tests/automation/feature_gate_test.rs"


class AutomationCiContracts(unittest.TestCase):
    def run_script(self, command, sources=(), names=(), environment=None):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            for name in sources:
                path = cwd / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            cargo = cwd / "cargo"
            cargo.write_text(f"#!{sys.executable}\n" + """import json, os, sys
args = sys.argv[1:]
with open('calls','a') as output:
    print(json.dumps([args,os.getenv('INTEGRATIONS_TEST_DATABASE_URL'),os.getenv('INTEGRATIONS_TEST_REDIS_URL')]),file=output)
if '--list' in args:
    print(os.environ['INVENTORY'])
elif '--include-ignored' in args or '--ignored' in args:
    sys.exit(int(os.environ.get('FIXTURE_EXIT','0')))
""")
            cargo.chmod(0o755)
            env = dict(os.environ, PATH=f"{cwd}:{os.environ['PATH']}",
                       INVENTORY="\n".join(name + ": test" for name in names),
                       DATABASE_URL="postgres://localhost/ci", APPFLOWY_TEST_REDIS_URL="redis://127.0.0.1:42612",
                       APPFLOWY_DATABASE_AUTOMATIONS_ENABLED="false")
            env.update(environment or {})
            result = subprocess.run(["bash", *command], cwd=cwd, env=env, text=True, capture_output=True)
            calls = cwd / "calls"
            return result, [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    def test_member_provider_inventory_and_service_backed_execution(self):
        prepared, calls = self.run_script([MEMBERS, "prepare"], [OUTBOUND], PROVIDERS)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(calls[0][0], ["test", "--locked", "-p", "appflowy-cloud-integrations", "--lib", "--", "--list"])
        missing, _ = self.run_script([MEMBERS, "prepare"], [OUTBOUND], PROVIDERS[:-1])
        self.assertNotEqual(missing.returncode, 0)
        executed, calls = self.run_script([MEMBERS, "run"], [OUTBOUND], environment={"FIXTURE_EXIT": "9"})
        self.assertEqual(executed.returncode, 9)
        self.assertEqual(calls[-1], [
            ["test", "--locked", "-p", "appflowy-cloud-integrations", "--lib", "--", "--include-ignored", "--test-threads=1"],
            "postgres://localhost/ci", "redis://127.0.0.1:42612"])
        unavailable, calls = self.run_script(
            [MEMBERS, "run"], [OUTBOUND], environment={"APPFLOWY_TEST_REDIS_URL": ""})
        self.assertNotEqual(unavailable.returncode, 0)
        self.assertEqual(len(calls), 1, "only the ordinary member packages ran")
        old, calls = self.run_script([MEMBERS, "run"])
        self.assertEqual(old.returncode, 0)
        self.assertFalse(any("appflowy-cloud-integrations" in args for args, _, _ in calls))

    def test_commercial_gate_requires_exact_ignored_test_and_propagates_failure(self):
        old, calls = self.run_script([GATE])
        self.assertEqual((old.returncode, calls), (0, []))
        missing, calls = self.run_script([GATE], [GATE_SOURCE], [DISABLED + "_renamed"])
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(len(calls), 1)
        enabled, calls = self.run_script([GATE], [GATE_SOURCE], [DISABLED],
                                         {"APPFLOWY_DATABASE_AUTOMATIONS_ENABLED": "true"})
        self.assertNotEqual(enabled.returncode, 0)
        self.assertEqual(calls, [])
        gate, calls = self.run_script([GATE], [GATE_SOURCE], [DISABLED], {"FIXTURE_EXIT": "4"})
        self.assertEqual(gate.returncode, 4)
        prefix = [
            "test", "--locked", "-p", "appflowy-cloud", "--features=commercial-ci",
            "--test", "main", DISABLED, "--", "--ignored", "--exact",
        ]
        self.assertEqual([args for args, _, _ in calls], [prefix + ["--list"], prefix + ["--test-threads=1"]])

    def test_enabled_database_namespace_and_single_disabled_selfhost_lane(self):
        workflows = ROOT / ".github/workflows"
        hosted = yaml.safe_load((workflows / "cloud_integration_ci.yaml").read_text())["jobs"]["test"]
        lanes = hosted["strategy"]["matrix"]["include"]
        databases = next(lane for lane in lanes if lane.get("topic") == "Databases")
        self.assertIn("automation", databases["test_modules"].split())
        self.assertNotIn("automation", databases["test_skips"])
        self.assertTrue(any("APPFLOWY_DATABASE_AUTOMATIONS_ENABLED=true" in step.get("run", "") for step in hosted["steps"]))
        commercial = yaml.safe_load((workflows / "cloud_commercial_integration_ci.yaml").read_text())
        self.assertEqual(commercial["env"]["APPFLOWY_DATABASE_AUTOMATIONS_ENABLED"], "false")
        gate_steps = [step for step in commercial["jobs"]["test"]["steps"] if "commercial_automation_gate.sh" in step.get("run", "")]
        self.assertEqual([step["if"] for step in gate_steps], ["matrix.test_service == 'appflowy_cloud_commercial'"])

    def test_manual_commercial_run_pins_source_and_isolates_branch_concurrency(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/cloud_commercial_integration_ci.yaml").read_text())
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]), {"pr_ref", "pr_number"})
        sha = "a" * 40
        manual = event_context(workflow, event="workflow_dispatch", ref="refs/heads/stacked-ci",
                               inputs={"pr_ref": sha, "pr_number": "123"})
        auto = event_context(workflow, event="repository_dispatch",
                             payload={"event_type": "pr_sync", "pr_ref": sha, "pr_number": "123"})
        for context in (manual, auto):
            self.assertIn("pr=123 ", render_expression(workflow["run-name"], context))
            for job in workflow["jobs"].values():
                checkout = next(step for step in job["steps"] if step.get("name") == "Checkout Private Repo")
                self.assertEqual(render_expression(checkout["with"]["ref"], context), sha)
        self.assertNotEqual(render_expression(workflow["concurrency"]["group"], auto),
                            render_expression(workflow["concurrency"]["group"], manual))
        guard = workflow["jobs"]["setup"]["steps"][0]["run"]
        for ref, number, valid in [(sha, "123", True), ("main", "123", False), (sha, "0", False)]:
            result = subprocess.run(["bash", "-euc", guard], env={"PR_REF": ref, "PR_NUMBER": number})
            self.assertEqual(result.returncode == 0, valid)
