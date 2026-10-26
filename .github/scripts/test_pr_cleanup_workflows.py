"""Render the committed workflow expressions against PR and post-merge events."""

import ast
import json
from pathlib import Path
import re
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
CI_REPO = "AppFlowy-IO/AppFlowy-CI"
DESKTOP_REPO = "AppFlowy-IO/AppFlowy-Premium"
CLOUD_REPO = "AppFlowy-IO/AppFlowy-Cloud-Premium"
SOURCE_WORKFLOWS = {
    **dict.fromkeys(("flutter_ci.yaml", "rust_ci.yaml", "ios_ci.yaml",
                     "mobile_ci.yml", "rust_coverage.yml"), DESKTOP_REPO),
    **dict.fromkeys(("cloud_backend_ci.yaml", "cloud_frontend_ci.yaml",
                     "cloud_docker_ci.yaml", "cloud_e2e_ci.yaml",
                     "cloud_rustlint_ci.yaml", "cloud_integration_ci.yaml",
                     "cloud_commercial_integration_ci.yaml"), CLOUD_REPO),
}


def evaluate_expression(expression, context):
    """Support only the literals, lookups, operators and functions used below.

    Translate tokens before parsing so operators inside quoted strings remain
    untouched. Unsupported syntax fails rather than approximating GitHub policy.
    """
    token_pattern = re.compile(r"\s*('(?:''|[^'])*'|&&|\|\||==|!=|[(),]|[A-Za-z_][\w.]*|[0-9]+)")
    tokens = []
    remaining = expression.strip()
    replacements = {"&&": "and", "||": "or", "true": "True", "false": "False", "null": "None"}
    while remaining:
        match = token_pattern.match(remaining)
        if not match:
            raise ValueError(f"Unsupported GitHub expression: {remaining}")
        token = match[1]
        tokens.append(json.dumps(token[1:-1].replace("''", "'"))
                      if token.startswith("'") else replacements.get(token, token))
        remaining = remaining[match.end():].lstrip()

    def truthy(value):
        # GitHub treats objects and arrays as truthy, including empty ones.
        return True if isinstance(value, (dict, list)) else bool(value)

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in context:
            return context[node.id]
        if isinstance(node, ast.Attribute):
            owner = visit(node.value)
            return owner.get(node.attr, "") if isinstance(owner, dict) else ""
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            value = visit(node.values[0])
            for operand in node.values[1:]:
                if truthy(value) != isinstance(node.op, ast.And):
                    break
                value = visit(operand)
            return value
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(left, str) and isinstance(right, str):
                left, right = left.casefold(), right.casefold()
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            arguments = [visit(argument) for argument in node.args]
            if node.func.id == "format":
                return arguments[0].format(*arguments[1:])
            if node.func.id == "fromJSON":
                return json.loads(arguments[0])
            if node.func.id == "contains":
                values, sought = arguments
                if isinstance(values, str):
                    return str(sought).casefold() in values.casefold()
                return any(str(value).casefold() == str(sought).casefold() for value in values)
        raise ValueError(f"Unsupported GitHub expression node: {ast.dump(node)}")

    return visit(ast.parse(" ".join(tokens), mode="eval").body)


def render_expression(template, context):
    return re.sub(r"\$\{\{(.*?)\}\}",
                  lambda match: str(evaluate_expression(match[1], context)), template)


def event_context(workflow, *, event="repository_dispatch", payload=None, inputs=None,
                  action="", pull_request=None, conclusion="", ref="refs/heads/main"):
    return {
        "github": {
            "repository": CI_REPO, "workflow": workflow["name"], "event_name": event,
            "ref": ref, "run_id": 900, "run_attempt": 2,
            "event": {"action": action, "client_payload": payload or {}, "inputs": inputs or {},
                      "pull_request": pull_request or {}, "workflow_run": {"conclusion": conclusion},
                      "repository": {"default_branch": "main"}},
        },
        "inputs": inputs or {},
    }


class CleanupWorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = {path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader)
                         for path in WORKFLOWS.iterdir()}

    def assert_identity(self, name, source, pr, **event):
        workflow = self.workflows[name]
        title = render_expression(workflow["run-name"], event_context(workflow, **event))
        self.assertTrue(title.startswith(f"[ci-v2 source={source} pr={pr} parent="), title)
        return title

    def test_every_private_source_workflow_has_identity_coverage(self):
        actual = {name for name, workflow in self.workflows.items()
                  if name not in {"webhook_receiver.yaml", "cancel_obsolete_ci.yaml"}
                  and "repository_dispatch" in workflow.get("on", {})}
        self.assertEqual(actual, set(SOURCE_WORKFLOWS))

    def test_source_pr_dispatches_identify_the_correct_repository(self):
        for name, source in SOURCE_WORKFLOWS.items():
            for kind in ("pr_opened", "pr_sync"):
                with self.subTest(workflow=name, event=kind):
                    self.assert_identity(name, source, 41,
                                         payload={"event_type": kind, "pr_number": "41"})

    def test_post_merge_push_with_pr_attribution_is_not_a_pr_test(self):
        for name, source in SOURCE_WORKFLOWS.items():
            for kind in ("push", ""):
                with self.subTest(workflow=name, event=kind):
                    self.assert_identity(name, source, "none", payload={
                        "event_type": kind, "pr_number": "41", "pr_ref": "main",
                    })

    def test_manual_validation_requires_an_explicit_pr(self):
        for name, source in SOURCE_WORKFLOWS.items():
            if "workflow_dispatch" not in self.workflows[name]["on"]:
                continue
            with self.subTest(workflow=name):
                self.assert_identity(name, source, 41, event="workflow_dispatch",
                                     inputs={"pr_number": "41", "pr_ref": "main"})
                self.assert_identity(name, source, "none", event="workflow_dispatch",
                                     inputs={"pr_ref": "main"})

    def test_ci_pull_requests_only_run_workflow_checks(self):
        native = {name for name, workflow in self.workflows.items()
                  if "pull_request" in workflow.get("on", {})}
        self.assertEqual(native, {"workflow_checks.yaml"})
        trigger = self.workflows["workflow_checks.yaml"]["on"]["pull_request"] or {}
        # Every CI PR gets the same check, including documentation and Docker-only changes.
        self.assertNotIn("paths", trigger)
        self.assertNotIn("paths-ignore", trigger)

    def test_desktop_suites_still_accept_source_events_and_manual_validation(self):
        for suite in ("rust", "flutter", "ios"):
            trigger = self.workflows[f"{suite}_ci.yaml"]["on"]
            with self.subTest(suite=suite):
                self.assertEqual(trigger["repository_dispatch"]["types"],
                                 [f"private-repo-{suite}-ci"])
                self.assertIn("pr_ref", trigger["workflow_dispatch"]["inputs"])

    def test_integration_title_retains_the_exact_parent_attempt(self):
        title = self.assert_identity("cloud_integration_ci.yaml", CLOUD_REPO, 41, payload={
            "event_type": "pr_sync", "pr_number": "41",
            "parent_run_id": "801", "parent_run_attempt": "2",
        })
        self.assertIn("parent=801-2]", title)

    def test_receiver_titles_cover_both_sources_and_preserve_main_pushes(self):
        for source, action in ((DESKTOP_REPO, "private-repo-webhook"),
                               (CLOUD_REPO, "cloud-premium-webhook")):
            for kind, expected in (("pr_opened", 41), ("pr_sync", 41), ("push", "none")):
                with self.subTest(source=source, event=kind):
                    self.assert_identity("webhook_receiver.yaml", source, expected, action=action,
                                         payload={"event_type": kind, "pr_number": "41"})
                    self.assert_identity("webhook_receiver.yaml", source, expected,
                                         event="workflow_dispatch", inputs={
                                             "source_repo": source.split("/")[1],
                                             "event_type": kind, "pr_number": "41",
                                         })

    def test_receiver_concurrency_separates_sources_and_pushes_but_groups_pr_updates(self):
        workflow = self.workflows["webhook_receiver.yaml"]

        def group(action, kind, number="41"):
            return render_expression(workflow["concurrency"]["group"], event_context(
                workflow, action=action,
                payload={"event_type": kind, "pr_number": number, "pr_ref": "main"},
            )).casefold()

        desktop = group("private-repo-webhook", "pr_opened")
        cloud = group("cloud-premium-webhook", "pr_opened")
        self.assertNotEqual(desktop, cloud)
        for action in ("private-repo-webhook", "cloud-premium-webhook"):
            self.assertEqual(group(action, "pr_opened"), group(action, "pr_sync"))
            self.assertNotEqual(group(action, "pr_sync"), group(action, "push"))
            self.assertNotEqual(group(action, "pr_sync"), group(action, "pr_sync", "42"))

    def test_cleanup_trigger_requires_a_merge_and_only_trusted_source_notifications(self):
        workflow = self.workflows["cancel_obsolete_ci.yaml"]
        self.assertEqual(workflow["on"]["pull_request_target"]["types"], ["closed"])
        self.assertEqual(workflow["on"]["repository_dispatch"]["types"], ["ci-pr-closed"])

        def enabled(**event):
            return evaluate_expression(workflow["jobs"]["cancel"]["if"],
                                       event_context(workflow, **event))

        self.assertTrue(enabled(event="pull_request_target", pull_request={"merged": True}))
        self.assertFalse(enabled(event="pull_request_target", pull_request={"merged": False}))
        for source in (DESKTOP_REPO, CLOUD_REPO):
            self.assertTrue(enabled(payload={"source_repository": source}))
        self.assertFalse(enabled(payload={"source_repository": "unrelated/repository"}))
        self.assertFalse(enabled(payload={}))
        self.assertTrue(enabled(event="schedule"))
        for conclusion in ("cancelled", "timed_out"):
            self.assertTrue(enabled(event="workflow_run", conclusion=conclusion))
        self.assertFalse(enabled(event="workflow_run", conclusion="success"))

    def test_privileged_cleanup_checks_out_only_the_default_branch(self):
        steps = self.workflows["cancel_obsolete_ci.yaml"]["jobs"]["cancel"]["steps"]
        checkouts = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(checkouts[0]["with"]["ref"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(checkouts[0]["with"]["persist-credentials"], "false")
        self.assertFalse(any(step.get("uses", "").startswith("actions/download-artifact@") for step in steps))


if __name__ == "__main__":
    unittest.main()
