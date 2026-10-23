"""Exercise the actual concurrency expressions without GitHub jobs."""

from pathlib import Path
import unittest

import yaml

from test_pr_cleanup_workflows import SOURCE_WORKFLOWS, event_context, render_expression


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
CLIENT_WORKFLOWS = ("flutter_ci.yaml", "rust_ci.yaml", "ios_ci.yaml")


def concurrency_group(workflow, *, payload=None, inputs=None, ref="refs/heads/main"):
    context = event_context(workflow, payload=payload, inputs=inputs, ref=ref)
    rendered = render_expression(workflow["concurrency"]["group"], context)
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
                    f"{workflow['name']}-validation-refs/pull/29/merge".lower(),
                )

    def test_client_workflows_do_not_cancel_each_other(self):
        groups = {
            concurrency_group(workflow, inputs={"pr_ref": "same-sha"})
            for workflow in self.workflows.values()
        }
        self.assertEqual(len(groups), len(CLIENT_WORKFLOWS))

    def test_late_pr_dispatch_cannot_cancel_post_merge_push_for_any_source_workflow(self):
        for name in SOURCE_WORKFLOWS:
            workflow = yaml.safe_load((WORKFLOWS / name).read_text())
            with self.subTest(workflow=name):
                push = concurrency_group(workflow, payload={
                    "event_type": "push", "pr_number": "1329", "pr_ref": "main",
                })
                groups = [concurrency_group(workflow, payload={
                    "event_type": kind, "pr_number": "1329", "pr_ref": "old-sha",
                }) for kind in ("pr_opened", "pr_sync")]
                self.assertNotIn(push, groups)
                self.assertEqual(groups[0], groups[1])


if __name__ == "__main__":
    unittest.main()
