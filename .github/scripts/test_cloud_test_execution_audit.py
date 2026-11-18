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

    @unittest.skipUnless(shutil.which("rustc"), "requires Rust for real libtest output")
    def test_real_should_panic_case_and_child_test_process_keep_their_binary_credited(self):
        # One `#[should_panic]` case or one test that re-runs its own binary used to make the
        # audit discard every case of that test binary.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cases.rs"
            source.write_text('''#[test]
fn before() {}
#[test]
#[should_panic(expected = "boom")]
fn rejects() { panic!("boom") }
#[test]
fn spawns() {
    if std::env::var("AUDIT_CHILD").is_ok() { return; }
    let status = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "spawns", "--nocapture"]).env("AUDIT_CHILD", "1").status().unwrap();
    assert!(status.success());
}
''')
            binary = root / "cases"
            subprocess.run(["rustc", "--test", str(source), "-o", str(binary)], check=True,
                           capture_output=True, timeout=30)
            result = subprocess.run([str(binary), "--test-threads=1", "--nocapture"], text=True,
                                    capture_output=True, check=True, timeout=30)
            self.assertIn("running 1 test", result.stdout, "the child prints its own libtest segment")
            observed, issues = audit.executions("AF_TEST_SOURCE_SHA=" + SHA + "\n" + result.stdout, SHA)
            self.assertEqual(observed, {"before": "ok", "rejects": "ok", "spawns": "ok"})
            self.assertEqual(issues, [])

    def test_should_panic_case_counts_toward_its_segment(self):
        text = log({"tests::passed": "ok", "tests::rejects_zero": "ok"}).replace(
            "test tests::rejects_zero ...", "test tests::rejects_zero - should panic ...")
        records, issues = audit.executions(text, SHA)
        self.assertEqual(records, {"tests::passed": "ok", "tests::rejects_zero": "ok"})
        self.assertEqual(issues, [])

    def test_child_test_process_output_does_not_discard_the_parent_segment(self):
        child = ("running 1 test\ntest tests::spawns ... ok\n"
                 "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 2 filtered out;\n")
        text = log({"tests::before": "ok", "tests::spawns": "ok", "tests::after": "ok"}).replace(
            "test tests::spawns ... ok\n", "test tests::spawns ... \n" + child + child + "ok\n")
        records, issues = audit.executions(text, SHA)
        self.assertEqual(records, {"tests::before": "ok", "tests::spawns": "ok", "tests::after": "ok"})
        self.assertEqual(issues, [])

    def test_unfinished_segment_stays_uncredited_when_another_binary_follows(self):
        aborted = log({"tests::lost": "ok"}).split("test result:")[0]
        records, issues = audit.executions(aborted + log({"tests::kept": "ok"}), SHA)
        self.assertEqual(records, {"tests::kept": "ok"})
        self.assertEqual(issues, ["Incomplete libtest segment"])

    def test_failed_child_summary_cannot_close_the_parent_as_passed(self):
        # The child aborts before its summary, so the parent's summary meets the child's cases.
        text = log({"tests::before": "ok", "tests::spawns": "ok"}).replace(
            "test tests::spawns ... ok\n", "test tests::spawns ... \nrunning 1 test\nok\n")
        records, issues = audit.executions(text, SHA)
        self.assertEqual(records, {})
        self.assertIn("Libtest summary did not match parsed case outcomes", issues)

    def test_changed_inventory_ignores_helpers_and_notifies_on_ignored_or_missing_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "#[test]\nfn ran() {}\n#[test]\n#[ignore]\nfn skipped() {}\n#[test]\nfn missing() {}\nfn helper() {}\n")
            report = audit.audit(root, audit.compare_lines(changes), {"job": log({"tests::ran": "ok", "tests::skipped": "ignored"})}, SHA)
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
            self.assertEqual(len(audit.inventory(root, audit.compare_lines(changes))), 1)
            changes["files"][0]["patch"] = "@@ -2 +2 @@\n-fn existing_helper() {}\n+fn existing_helper() { changed_body(); }"
            self.assertEqual(len(audit.inventory(root, audit.compare_lines(changes))), 1)

    def test_ambiguous_names_and_generated_cases_are_unverified_not_false_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "mod a { #[test] fn same() {} }\nmod b { #[test] fn same() {} }\n#[rstest]\n#[case([1, 2])]\nfn generated() {}\n")
            report = audit.audit(root, audit.compare_lines(changes), {"job": log({"a::same": "ok", "generated": "ok"})}, SHA)
            self.assertEqual(report["counts"], {"unverified_identity": 3})

    def test_exceptions_are_explicit_and_cannot_hide_a_case_without_a_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "#[test]\nfn paid_provider() {}\n")
            exceptions = root / "script/ci/test_execution_exemptions.json"
            exceptions.parent.mkdir(parents=True)
            exceptions.write_text(json.dumps({"src/lib.rs::paid_provider": "Requires a separately authorized paid provider run"}))
            report = audit.audit(root, audit.compare_lines(changes), {}, SHA)
            self.assertEqual(report["counts"], {"exempt": 1})
            exceptions.write_text(json.dumps({"src/lib.rs::paid_provider": "skip"}))
            with self.assertRaises(ValueError):
                audit.audit(root, audit.compare_lines(changes), {}, SHA)

    def test_missing_diff_and_escaping_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changes = self.source(root, "#[test]\nfn case() {}\n")
            del changes["files"][0]["patch"]
            with self.assertRaises(ValueError):
                audit.inventory(root, audit.compare_lines(changes))

    def test_git_inventory_survives_missing_large_patches_and_the_300_file_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

            git("init", "--quiet")
            git("config", "user.name", "CI")
            git("config", "user.email", "ci@example.test")
            original = "struct Row;\n" * 1000 + "#[test]\nfn unchanged() {}\n"
            (root / "old.rs").write_text(original)
            (root / "move.rs").write_text("#[test]\nfn rename_only() {}\n")
            (root / "old.txt").write_text("#[test]\nfn became_rust() {}\n")
            (root / "gone.rs").write_text("#[test]\nfn removed() {}\n")
            git("add", ".")
            git("commit", "-qm", "merge base")
            base = git("rev-parse", "HEAD")
            git("switch", "-qc", "feature")
            git("mv", "old.rs", "moved [with glob].rs")
            git("mv", "move.rs", "pure rename.rs")
            git("mv", "old.txt", "typed.rs")
            git("rm", "-q", "gone.rs")
            (root / "moved [with glob].rs").write_text(original + "#[test]\nfn added_at_end() {}\n")
            (root / "new.rs").write_text("struct Added;\n" * 2000 + "#[test]\nfn added_file() {}\n")
            for i in range(301):
                (root / f"change-{i:03}.txt").touch()
            git("add", ".")
            git("commit", "-qm", "large feature")
            head = git("rev-parse", "HEAD")

            # Base moved independently. Comparing the two branch tips would spuriously
            # inventory unchanged() when its declaration was removed only upstream.
            git("switch", "-qc", "advanced-base", base)
            (root / "old.rs").write_text("struct Row;\n" * 1000)
            git("add", ".")
            git("commit", "-qm", "upstream")
            comparison_base = git("merge-base", "advanced-base", head)
            self.assertEqual(comparison_base, base)
            with self.assertRaisesRegex(ValueError, "differs"):
                audit.git_lines(root, comparison_base, head)
            git("checkout", "-q", head)

            # A partial GitHub result has neither the complete file list nor a usable patch.
            changes = {"files": [{"filename": "new.rs", "status": "added", "additions": 2002}]}
            with self.assertRaisesRegex(ValueError, "truncated"):
                audit.compare_lines(changes)
            changes["files"][0]["patch"] = "@@ -0,0 +1,2002 @@\n+struct Added;"
            with self.assertRaisesRegex(ValueError, "truncated"):
                audit.compare_lines(changes)
            touched = audit.git_lines(root, comparison_base, head)
            report = audit.audit(root, touched, {"job": log({"added_file": "ok"}, sha=head)}, head)
            self.assertEqual({case["name"]: case["status"] for case in report["cases"]},
                             {"added_file": "executed", "added_at_end": "not_observed", "became_rust": "not_observed"})

            # Reproduce checkout's actual depth-one state and verify the separate base fetch.
            with tempfile.TemporaryDirectory() as checkout:
                subprocess.run(["git", "clone", "-q", "--depth=1", "--no-tags", "--branch", "feature",
                                root.as_uri(), checkout], check=True)
                shallow = Path(checkout)
                self.assertEqual(audit.git_lines(shallow, comparison_base, head), touched)
                with self.assertRaises(subprocess.CalledProcessError):
                    audit.git_lines(shallow, "f" * 40, head)

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
                def api(path):
                    if "/pulls/" in path:
                        return {"state": "open", "head": {"sha": SHA}, "base": {"sha": "b" * 40}}
                    if "/compare/" in path:
                        return {"merge_base_commit": {"sha": "c" * 40}}
                    if "/actions/runs?head_sha=" in path:
                        return {"total_count": 0, "workflow_runs": []}
                    if "/jobs?" in path:
                        return {"total_count": 3, "jobs": [
                            {"id": 11, "html_url": "never-started-url", "status": "completed",
                             "conclusion": "cancelled", "runner_id": 0, "steps": []},
                            {"id": 12, "html_url": "job-url", "status": "completed", "conclusion": "success"},
                            {"id": 13, "html_url": "started-url", "status": "completed", "conclusion": "cancelled",
                             "runner_id": 53, "steps": [{"status": "completed"}]}]}
                    self.fail("Unexpected API route: " + path)

                runs = [{"id": 10, "html_url": "run-url", "status": "in_progress" if pending else "completed",
                         "head_sha": "c" * 40, "run_attempt": 1}]
                text = log({"tests::added_case": "ok"}) if executed else "cargo clippy compiled test code"
                with patch.object(audit, "api", side_effect=api), \
                        patch.object(audit, "related_runs", return_value=runs), \
                        patch.object(audit, "job_log", side_effect=lambda job_id, _: text if job_id == 12 else "") as fetch_log, \
                        patch.object(audit, "git_lines", return_value=audit.compare_lines(changes)) as git_diff, \
                        patch.object(audit, "publish") as publish, redirect_stdout(io.StringIO()):
                    result = audit.github_report(root, 1583, SHA, root / "audit.json", publish_status=True)
                self.assertEqual([call.args[0] for call in fetch_log.call_args_list], [12, 13])
                git_diff.assert_called_once_with(root, "c" * 40, SHA)
                state = "pending" if pending else "success" if executed else "failure"
                self.assertEqual(publish.call_args.args[2], state)
                self.assertEqual(result, int(state == "failure"))
                report = json.loads((root / "audit.json").read_text())
                self.assertEqual((report["source_sha"], report["merge_base_sha"]), (SHA, "c" * 40))
                self.assertEqual(report["cancelled_before_start"], ["never-started-url"])

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
