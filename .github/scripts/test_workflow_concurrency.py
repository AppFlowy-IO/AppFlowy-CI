"""Exercise the actual concurrency expressions without GitHub jobs."""

from pathlib import Path
import unittest

import yaml

from test_pr_cleanup_workflows import SOURCE_WORKFLOWS, event_context, render_expression


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
CLIENT_WORKFLOWS = ("flutter_ci.yaml", "rust_ci.yaml", "ios_ci.yaml")


def concurrency_group(workflow, *, payload=None, inputs=None, ref="refs/heads/main",
                      event="repository_dispatch", pull_request=None):
    context = event_context(workflow, payload=payload, inputs=inputs, ref=ref,
                            event=event, pull_request=pull_request)
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

    def test_automatic_private_ref_is_independent_of_public_workflow_ref(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    concurrency_group(workflow, payload={"pr_ref": "codex/video"}),
                    concurrency_group(
                        workflow,
                        payload={"pr_ref": "codex/video"},
                        ref="refs/heads/ci-validation",
                    ),
                )

    def test_manual_ci_branch_validation_does_not_cancel_automatic_same_pr_runs(self):
        for name in ("flutter_ci.yaml", "rust_ci.yaml"):
            workflow = self.workflows[name]
            with self.subTest(workflow=name):
                automatic = concurrency_group(workflow, payload={"event_type": "pr_sync",
                                               "pr_number": "1545", "pr_ref": "a" * 40})
                manual = concurrency_group(workflow, event="workflow_dispatch",
                                            inputs={"pr_number": "1545", "pr_ref": "a" * 40},
                                            ref="refs/heads/codex/pdf-export-regression-gate")
                self.assertNotEqual(manual, automatic)

    def test_manual_retries_share_only_their_ci_branch_and_private_pr(self):
        for name in ("flutter_ci.yaml", "rust_ci.yaml"):
            workflow = self.workflows[name]
            with self.subTest(workflow=name):
                original = concurrency_group(workflow, event="workflow_dispatch",
                    inputs={"pr_number": "1545", "pr_ref": "a" * 40}, ref="refs/heads/ci-pdf")
                retry = concurrency_group(workflow, event="workflow_dispatch",
                    inputs={"pr_number": "1545", "pr_ref": "b" * 40}, ref="refs/heads/ci-pdf")
                another_branch = concurrency_group(workflow, event="workflow_dispatch",
                    inputs={"pr_number": "1545", "pr_ref": "a" * 40}, ref="refs/heads/ci-other")
                self.assertEqual(original, retry)
                self.assertNotEqual(original, another_branch)

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

    def test_manual_validation_without_source_ref_keeps_workflow_ref_fallback(self):
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                prefix = (f"ci-branch-refs/heads/ci-validation" if name in ("flutter_ci.yaml", "rust_ci.yaml")
                          else "validation")
                self.assertEqual(
                    concurrency_group(workflow, event="workflow_dispatch",
                                      ref="refs/heads/ci-validation"),
                    f"{workflow['name']}-{prefix}-refs/heads/ci-validation".lower(),
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


class WorkflowChecksConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load((WORKFLOWS / "workflow_checks.yaml").read_text())

    def test_new_commits_supersede_only_the_same_ci_pr(self):
        self.assertTrue(self.workflow["concurrency"]["cancel-in-progress"])
        first = concurrency_group(self.workflow, event="pull_request",
                                  pull_request={"number": 97}, ref="refs/pull/97/merge")
        same_pr = concurrency_group(self.workflow, event="pull_request",
                                    pull_request={"number": 97}, ref="refs/pull/97/head")
        other_pr = concurrency_group(self.workflow, event="pull_request",
                                     pull_request={"number": 98}, ref="refs/pull/98/merge")
        self.assertEqual(first, same_pr)
        self.assertNotEqual(first, other_pr)

    def test_main_push_and_manual_validation_do_not_cancel_each_other(self):
        groups = {
            concurrency_group(self.workflow, event=event, ref="refs/heads/main")
            for event in ("push", "workflow_dispatch")
        }
        self.assertEqual(len(groups), 2)


if __name__ == "__main__":
    unittest.main()
