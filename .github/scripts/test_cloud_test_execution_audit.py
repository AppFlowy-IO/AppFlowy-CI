from contextlib import redirect_stdout
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import yaml

import cloud_test_execution_audit as audit

SHA = "a" * 40


def log(cases, sha=SHA, summary=None):
    counts = {state: sum(value == state for value in cases.values()) for state in ("ok", "FAILED", "ignored")}
    summary = summary or f"test result: ok. {counts['ok']} passed; {counts['FAILED']} failed; {counts['ignored']} ignored;"
    return f"AF_TEST_SOURCE_SHA={sha}\nrunning {len(cases)} tests\n" + "".join(
        f"test {name} ... {state}\n" for name, state in cases.items()) + summary + "\n"


class ExecutionAuditTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("rustc"), "requires Rust for real libtest output")
    def test_real_libtest_output_credits_executed_bodies_and_preserves_ignored_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cases.rs"
            source.write_text("#[test]\nfn actual_body() {}\n#[test]\n#[ignore]\nfn manual_case() {}\n")
            binary = root / "cases"
            subprocess.run(["rustc", "--test", str(source), "-o", str(binary)], check=True,
                           capture_output=True, timeout=30)
            result = subprocess.run([str(binary), "--test-threads=1"], text=True, capture_output=True,
                                    check=True, timeout=30)
            observed, issues = audit.executions("AF_TEST_SOURCE_SHA=" + SHA + "\n" + result.stdout, SHA)
            self.assertEqual(observed, {"actual_body": "ok", "manual_case": "ignored"})
            self.assertEqual(issues, [])

    def source(self, root, text):
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        path = root / "src/lib.rs"
        path.parent.mkdir()
        path.write_text(text)
        subprocess.run(["git", "add", "src/lib.rs"], cwd=root, check=True)
        return {"files": [{"filename": "src/lib.rs", "status": "added", "patch":
                          f"@@ -0,0 +1,{len(text.splitlines())} @@\n" +
                          "\n".join("+" + line for line in text.splitlines())}]}

    def test_rust_masking_nested_attributes_and_parameterized_cases(self):
        source = '''// #[test] fn comment() {}
const TEXT: &str = r##"#[test] fn string() {}"##;
/* outer /* #[test] fn nested() {} */ end */
#[tokio::test(flavor = "multi_thread")]
async fn real_case() {}
#[rstest]
#[case([1, 2])]
fn generated_case() {}
#[cfg_attr(feature = "test", test)]
fn conditional_case() {}
'''
        cases = list(audit.functions(source, "src/lib.rs"))
        self.assertEqual([case["name"] for case in cases], ["real_case", "generated_case", "conditional_case"])
        self.assertEqual([case["parameterized"] for case in cases], [False, True, True])

    def test_only_same_sha_complete_count_consistent_executions_are_credited(self):
        records, issues = audit.executions(log({"tests::passed": "ok", "tests::skipped": "ignored"}), SHA)
        self.assertEqual(records, {"tests::passed": "ok", "tests::skipped": "ignored"})
        self.assertEqual(issues, [])
        for text in (log({"tests::passed": "ok"}, sha="b" * 40),
                     log({"tests::passed": "ok"}).split("test result:")[0],
                     log({"tests::passed": "ok"}, summary="test result: ok. 2 passed; 0 failed; 0 ignored;"),
                     "tests::passed: test\nFinished compiling tests\n",
                     log({"tests::passed": "ok"}) + "AF_TEST_SOURCE_SHA=" + "b" * 40):
            with self.subTest(text=text):
                self.assertEqual(audit.executions(text, SHA)[0], {})

    def test_nocapture_and_actions_timestamps_do_not_hide_real_execution(self):
        text = log({"tests::measured": "ok"}).replace("... ok", "... workload metrics\nok")
        text = "\n".join("2026-11-12T01:00:00Z " + line for line in text.splitlines())
        self.assertEqual(audit.executions(text, SHA)[0], {"tests::measured": "ok"})

    def test_changed_inventory_ignores_helpers_and_notifies_on_ignored_or_missing_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "#[test]\nfn ran() {}\n#[test]\n#[ignore]\nfn skipped() {}\n#[test]\nfn missing() {}\nfn helper() {}\n")
            report = audit.audit(root, changes, {"job": log({"tests::ran": "ok", "tests::skipped": "ignored"})}, SHA)
            self.assertEqual(report["counts"], {"executed": 1, "ignored_only": 1, "not_observed": 1})
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertTrue(audit.summarize(report))
            self.assertIn("::warning file=src/lib.rs", output.getvalue())
            report["pending_runs"] = ["running"]
            with redirect_stdout(output := io.StringIO()):
                audit.summarize(report)
            self.assertNotIn("::warning", output.getvalue())

    def test_new_test_attribute_and_diff_hunks_are_inventoried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "#[test]\nfn existing_helper() {}\n")
            changes["files"][0]["patch"] = "@@ -1 +1,2 @@\n+#[test]\n fn existing_helper() {}"
            self.assertEqual(len(audit.inventory(root, changes)), 1)
            changes["files"][0]["patch"] = "@@ -2 +2 @@\n-fn existing_helper() {}\n+fn existing_helper() { changed_body(); }"
            self.assertEqual(len(audit.inventory(root, changes)), 1)

    def test_ambiguous_names_and_generated_cases_are_unverified_not_false_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "mod a { #[test] fn same() {} }\nmod b { #[test] fn same() {} }\n#[rstest]\n#[case([1, 2])]\nfn generated() {}\n")
            report = audit.audit(root, changes, {"job": log({"a::same": "ok", "generated": "ok"})}, SHA)
            self.assertEqual(report["counts"], {"unverified_identity": 3})

    def test_exceptions_are_explicit_and_cannot_hide_a_case_without_a_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "#[test]\nfn paid_provider() {}\n")
            exceptions = root / "script/ci/test_execution_exemptions.json"
            exceptions.parent.mkdir(parents=True)
            exceptions.write_text(json.dumps({"src/lib.rs::paid_provider": "Requires a separately authorized paid provider run"}))
            report = audit.audit(root, changes, {}, SHA)
            self.assertEqual(report["counts"], {"exempt": 1})
            exceptions.write_text(json.dumps({"src/lib.rs::paid_provider": "skip"}))
            with self.assertRaises(ValueError):
                audit.audit(root, changes, {}, SHA)

    def test_missing_diff_and_escaping_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "#[test]\nfn case() {}\n")
            del changes["files"][0]["patch"]
            with self.assertRaises(ValueError):
                audit.inventory(root, changes)

    def test_advanced_pr_cannot_receive_a_stale_success_status(self):
        with patch.object(audit, "api", return_value={"state": "open", "head": {"sha": "b" * 40}}), \
                patch.object(audit.subprocess, "run") as run:
            audit.publish(1583, SHA, "success", "old run")
            run.assert_not_called()

    def test_private_test_merge_requires_matching_head_parent_and_recorded_checkout(self):
        merge = "b" * 40
        text = "repository: " + audit.CLOUD_REPO + "\n[command]/usr/bin/git log -1 --format=%H\n" + merge
        with patch.object(audit, "api", return_value={"parents": [{"sha": "c" * 40}, {"sha": SHA}]}):
            attested, proof = audit.attest_private_log(text, {"event": "pull_request"}, SHA, {})
            self.assertIn("AF_TEST_SOURCE_SHA=" + SHA, attested)
            self.assertEqual(proof["kind"], "github_pr_test_merge")
        for run, parents in (({"event": "workflow_dispatch"}, ["c" * 40, SHA]),
                             ({"event": "pull_request"}, ["c" * 40, "d" * 40])):
            with patch.object(audit, "api", return_value={"parents": [{"sha": value} for value in parents]}):
                self.assertIsNone(audit.attest_private_log(text, run, SHA, {})[1])
        self.assertIsNone(audit.attest_private_log("random SHA " + SHA, {}, SHA, {})[1])
        explicit_other = "AF_TEST_SOURCE_SHA=" + "c" * 40 + "\n" + text
        self.assertEqual(audit.attest_private_log(explicit_other, {}, SHA, {})[0], explicit_other)

    def test_private_completion_dispatch_is_bound_to_the_current_head(self):
        event = {"client_payload": {"event_type": "pr_sync", "pr_number": "1583", "source_sha": SHA}}
        with patch.object(audit, "api", return_value={"state": "open", "head": {"sha": SHA}}):
            self.assertIsNotNone(audit.resolve(event))
        with patch.object(audit, "api", return_value={"state": "open", "head": {"sha": "b" * 40}}):
            self.assertIsNone(audit.resolve(event))

    def test_signed_log_download_does_not_receive_the_repository_token(self):
        redirect = urllib.error.HTTPError("https://api.github.com/log", 302, "redirect",
                                         {"Location": "https://logs.example/signed"}, None)
        with patch.dict(os.environ, GH_TOKEN="test-only-token"), \
                patch.object(audit.urllib.request, "build_opener") as opener, \
                patch.object(audit.urllib.request, "urlopen", return_value=io.BytesIO(b"job log")) as download:
            opener.return_value.open.side_effect = redirect
            self.assertEqual(audit.job_log(123), "job log")
            self.assertIsNone(download.call_args.args[0].get_header("Authorization"))

    def test_github_audit_publishes_pending_failure_or_success_from_execution_evidence(self):
        for pending, executed in ((True, False), (False, False), (False, True)):
            with self.subTest(pending=pending, executed=executed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                changes = self.source(root, "#[test]\nfn added_case() {}\n")
                original_git = subprocess.check_output

                def git(command, **kwargs):
                    return SHA if command == ["git", "rev-parse", "HEAD"] else original_git(command, **kwargs)

                def api(path):
                    if "/pulls/" in path:
                        return {"state": "open", "head": {"sha": SHA}, "base": {"sha": "b" * 40}}
                    if "/compare/" in path:
                        return changes
                    if "/actions/runs?head_sha=" in path:
                        return {"total_count": 0, "workflow_runs": []}
                    if "/jobs?" in path:
                        return {"total_count": 1, "jobs": [{"id": 12, "html_url": "job-url",
                                "status": "completed", "conclusion": "success"}]}
                    self.fail("Unexpected API route: " + path)

                runs = [{"id": 10, "html_url": "run-url", "status": "in_progress" if pending else "completed",
                         "head_sha": "c" * 40, "run_attempt": 1}]
                text = log({"tests::added_case": "ok"}) if executed else "cargo clippy compiled test code"
                with patch.object(audit, "api", side_effect=api), \
                        patch.object(audit, "related_runs", return_value=runs), \
                        patch.object(audit, "job_log", return_value=text), \
                        patch.object(audit.subprocess, "check_output", side_effect=git), \
                        patch.object(audit, "publish") as publish, redirect_stdout(io.StringIO()):
                    result = audit.github_report(root, 1583, SHA, root / "audit.json", publish_status=True)
                state = "pending" if pending else "success" if executed else "failure"
                self.assertEqual(publish.call_args.args[2], state)
                self.assertEqual(result, int(state == "failure"))
                self.assertEqual(json.loads((root / "audit.json").read_text())["source_sha"], SHA)

    def test_workflow_uses_trusted_tooling_and_pins_the_private_source(self):
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load((root / ".github/workflows/cloud_test_execution_audit.yaml").read_text())
        steps = workflow["jobs"]["audit"]["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "main")
        source = next(step for step in steps if step.get("with", {}).get("repository") == audit.CLOUD_REPO)
        self.assertEqual(source["with"]["ref"], "${{ steps.source.outputs.sha }}")
        commands = "\n".join(step.get("run", "") for step in steps)
        self.assertNotIn("cloud-source/script/", commands)
        self.assertIn("--publish", commands)
        for name in ("cloud_integration_ci.yaml", "cloud_commercial_integration_ci.yaml"):
            jobs = yaml.safe_load((root / ".github/workflows" / name).read_text())["jobs"]
            for job in jobs.values():
                for index, step in enumerate(job.get("steps", [])):
                    if step.get("with", {}).get("repository") == audit.CLOUD_REPO and step.get("name") == "Checkout Private Repo":
                        self.assertEqual(job["steps"][index + 1]["name"], "Record Cloud test source")


if __name__ == "__main__":
    unittest.main()
