"""Exercise the actual concurrency fallback expressions without GitHub jobs.

The three client workflows use only dotted context lookups and logical OR.
Reject unsupported syntax rather than silently modeling a different expression.
"""

from pathlib import Path
import re
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
CLIENT_WORKFLOWS = ("flutter_ci.yaml", "rust_ci.yaml", "ios_ci.yaml")


def concurrency_group(workflow, *, payload=None, inputs=None, ref="refs/heads/main"):
    context = {
        "github": {
            "workflow": workflow["name"],
            "ref": ref,
            "event": {"client_payload": payload or {}, "inputs": inputs or {}},
        },
    }

    def evaluate(match):
        for operand in match.group(1).split("||"):
            path = operand.strip()
            if not re.fullmatch(r"github(?:\.[a-z_]+)+", path):
                raise ValueError(f"Unsupported concurrency expression: {path}")
            value = context
            for part in path.split("."):
                value = value.get(part, "") if isinstance(value, dict) else ""
            if value:
                return str(value)
        return ""

    rendered = re.sub(r"\$\{\{(.*?)\}\}", evaluate, workflow["concurrency"]["group"])
    # GitHub compares concurrency groups case-insensitively.
    return rendered.lower()


class ClientWorkflowConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = {
            name: yaml.safe_load((WORKFLOWS / name).read_text())
            for name in CLIENT_WORKFLOWS
        }

    def test_main_push_does_not_cancel_pinned_manual_validation(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertNotEqual(
                    concurrency_group(workflow, payload={"pr_ref": "main"}),
                    concurrency_group(workflow, inputs={"pr_ref": "ef0831184c"}),
                )

    def test_distinct_manual_refs_do_not_cancel_each_other(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertNotEqual(
                    concurrency_group(workflow, inputs={"pr_ref": "codex/video"}),
                    concurrency_group(workflow, inputs={"pr_ref": "codex/sharing"}),
                )

    def test_private_ref_is_independent_of_public_workflow_ref(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    concurrency_group(workflow, payload={"pr_ref": "codex/video"}),
                    concurrency_group(
                        workflow,
                        inputs={"pr_ref": "codex/video"},
                        ref="refs/heads/ci-validation",
                    ),
                )

    def test_same_pr_still_cancels_superseded_commits(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertTrue(workflow["concurrency"]["cancel-in-progress"])
                self.assertEqual(
                    concurrency_group(
                        workflow, payload={"pr_number": "1329", "pr_ref": "old-sha"},
                    ),
                    concurrency_group(
                        workflow, inputs={"pr_number": "1329", "pr_ref": "new-sha"},
                    ),
                )

    def test_different_prs_remain_isolated(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertNotEqual(
                    concurrency_group(
                        workflow, payload={"pr_number": "1329", "pr_ref": "same-sha"},
                    ),
                    concurrency_group(
                        workflow, inputs={"pr_number": "1330", "pr_ref": "same-sha"},
                    ),
                )

    def test_public_pull_requests_keep_their_ref_fallback(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    concurrency_group(workflow, ref="refs/pull/29/merge"),
                    f"{workflow['name']}-refs/pull/29/merge".lower(),
                )

    def test_client_workflows_do_not_cancel_each_other(self):
        groups = {
            concurrency_group(workflow, inputs={"pr_ref": "same-sha"})
            for workflow in self.workflows.values()
        }
        self.assertEqual(len(groups), len(CLIENT_WORKFLOWS))


if __name__ == "__main__":
    unittest.main()
