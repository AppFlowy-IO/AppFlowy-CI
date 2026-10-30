"""Exercise the complete PDF target selection and its cross-repository status boundary."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import desktop_pdf_status as status
import run_desktop_pdf_tests as gate
import run_desktop_pdf_ui_tests as ui_gate
import select_desktop_toolchain as toolchain


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
NAMES = ["wide_pdf_repeats_the_name_column_on_readable_pages",
         "af_cloud_incomplete_rows_refuse_export"]


class PdfGateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-pdf-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace, self.output = self.root / "rust-lib", self.root / "evidence"
        self.source = self.workspace / gate.TEST_SOURCE
        self.source.parent.mkdir(parents=True)
        self.source.write_text("// fixture target\n")
        self.contract = self.workspace / gate.CONTRACT
        self.contract.write_text(json.dumps({"version": 1, "package": gate.PACKAGE,
                                             "target": gate.TARGET, "required_tests": NAMES}))
        self.all_names, self.ignored = list(NAMES), []
        self.run_exit, self.run_count = 0, len(NAMES)
        self.commands = []

    def cargo(self, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(kwargs["cwd"], self.workspace.resolve())
        if "--list" in command:
            names = self.ignored if "--ignored" in command else self.all_names
            return subprocess.CompletedProcess(command, 0, "".join(f"{n}: test\n" for n in names))
        self.assertEqual(command[-2:], ["--", "--test-threads=1"])
        self.assertEqual(kwargs["env"]["APPFLOWY_PDF_TEST_OUTPUT_DIR"],
                         str((self.output / "saved-exports").resolve()))
        kwargs["stdout"].write(f"test result: ok. {self.run_count} passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n")
        return subprocess.CompletedProcess(command, self.run_exit)

    def run_gate(self):
        with patch.object(gate.subprocess, "run", side_effect=self.cargo), \
             patch.object(gate, "desktop_revision", return_value="a" * 40):
            code = gate.run(self.workspace, self.output)
        self.result = json.loads((self.output / "gate-result.json").read_text())
        return code

    def test_runs_every_test_unfiltered_including_names_missed_by_database_filter(self):
        self.assertEqual(self.run_gate(), 0)
        self.assertEqual(len(self.commands), 3)
        self.assertEqual(self.result["tests"], sorted(NAMES))
        self.assertEqual(self.result["status"], "passed")
        self.assertEqual(self.result["command"], ["cargo", "test", "-p", "event-integration-test",
                                                  "--test", "database_pdf_export", "--", "--test-threads=1"])

    def test_new_cases_run_without_having_to_edit_the_contract(self):
        self.all_names.append("new_field_type_regression")
        self.run_count += 1
        self.assertEqual(self.run_gate(), 0)
        self.assertIn("new_field_type_regression", self.result["tests"])

    def test_missing_required_case_fails_before_running_tests(self):
        self.all_names.pop()
        self.assertEqual(self.run_gate(), 1)
        self.assertEqual(len(self.commands), 2)
        self.assertIn("missing=", self.result["error"])

    def test_ignored_required_case_fails_before_running_tests(self):
        self.ignored = [NAMES[0]]
        self.assertEqual(self.run_gate(), 1)
        self.assertIn("ignored=", self.result["error"])

    def test_zero_inventory_is_not_a_passing_gate(self):
        self.all_names = []
        self.assertEqual(self.run_gate(), 1)
        self.assertIn("no non-ignored tests", self.result["error"])

    def test_zero_executed_tests_cannot_pass_after_nonzero_inventory(self):
        self.run_count = 0
        self.assertEqual(self.run_gate(), 1)
        self.assertEqual(self.result["status"], "failed")

    def test_native_failure_is_preserved_with_artifacts(self):
        self.run_exit = 101
        self.assertEqual(self.run_gate(), 101)
        self.assertEqual(self.result["status"], "failed")
        self.assertTrue((self.output / "test.log").exists())

    def test_deleting_a_contracted_target_fails(self):
        self.source.unlink()
        self.assertEqual(self.run_gate(), 1)
        self.assertEqual(self.commands, [])

    def test_failed_setup_replaces_a_previous_successful_result(self):
        self.output.mkdir()
        (self.output / "gate-result.json").write_text('{"status":"passed"}')
        self.source.unlink()
        self.assertEqual(self.run_gate(), 1)
        self.assertEqual(self.result["status"], "failed")

    def test_legacy_refs_without_target_or_contract_are_explicitly_skipped(self):
        self.source.unlink()
        self.contract.unlink()
        self.assertEqual(self.run_gate(), 0)
        self.assertEqual(self.result["status"], "legacy_skipped")
        self.assertEqual(self.commands, [])

    def test_legacy_target_without_contract_still_runs_all_cases(self):
        self.contract.unlink()
        self.assertEqual(self.run_gate(), 0)
        self.assertEqual(len(self.commands), 3)


class PdfStatusTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-pdf-status-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / gate.TEST_SOURCE).parent.mkdir(parents=True)
        (self.root / gate.TEST_SOURCE).touch()
        self.sha = "a" * 40
        self.event = {"client_payload": {"pr_number": "1545", "pr_ref": self.sha}}
        self.pages = {1: []}
        self.posts = []

    def api(self, method, path, data=None):
        if method == "POST":
            self.posts.append((path, data))
            return {}
        if "files?" in path:
            return self.pages[int(path.rsplit("=", 1)[1])]
        return {"head": {"sha": self.sha}}

    def publish(self, *, state="completed", result=None, actual_pr_sha=None):
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps(self.event))
        result_path = self.root / "gate.json"
        if result is not None:
            result_path.write_text(json.dumps(result))
        environment = {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": "repository_dispatch",
                       "GITHUB_REPOSITORY": "AppFlowy-IO/AppFlowy-CI", "GITHUB_RUN_ID": "123",
                       "GH_TOKEN": "fixture-token"}
        def api(method, path, data=None):
            if actual_pr_sha is not None and method == "GET" and "files?" not in path:
                return {"head": {"sha": actual_pr_sha}}
            return self.api(method, path, data)
        with patch.dict(os.environ, environment), \
             patch.object(status.subprocess, "check_output", return_value=self.sha), \
             patch.object(status, "github_json", side_effect=api):
            status.publish(self.root, state, result_path)

    def source_sha(self, actual=None, event_name="repository_dispatch"):
        with patch.object(status.subprocess, "check_output", return_value=(actual or self.sha) + "\n"):
            return status.source_sha(self.root, self.event, event_name)

    def test_actual_checkout_must_match_immutable_requested_sha(self):
        self.assertEqual(self.source_sha(), self.sha)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.source_sha("b" * 40)

    def test_mutable_branch_cannot_receive_a_required_passing_status(self):
        self.event["client_payload"]["pr_ref"] = "main"
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.source_sha()

    def test_public_ci_pr_does_not_publish_a_private_status(self):
        self.assertIsNone(self.source_sha(event_name="pull_request"))

    def test_legacy_ref_without_pdf_target_has_explicit_not_applicable_status(self):
        (self.root / gate.TEST_SOURCE).unlink()
        self.assertEqual(self.source_sha(), self.sha)
        state, description = status.outcome(self.root, "rust", "completed", {}, self.sha, "success")
        self.assertEqual(state, "success")
        self.assertIn("Not applicable", description)

    def test_current_gate_cannot_report_success_for_wrong_revision_kind_or_empty_run(self):
        result = {"status": "passed", "exit_code": 0, "kind": "rust", "desktop_sha": self.sha,
                  "runnable_count": 12}
        self.assertEqual(status.outcome(self.root, "rust", "completed", result, self.sha, "success")[0], "success")
        for changed in ({"desktop_sha": "b" * 40}, {"kind": "ui"}, {"runnable_count": 0},
                        {"status": "legacy_skipped"}, {"exit_code": 101}):
            with self.subTest(changed=changed):
                self.assertEqual(status.outcome(self.root, "rust", "completed", {**result, **changed}, self.sha, "success")[0], "failure")
        self.assertEqual(status.outcome(self.root, "rust", "completed", result, self.sha, "failure")[0], "failure")
        for step_outcome in ("skipped", "failure", "cancelled", ""):
            self.assertEqual(status.outcome(self.root, "rust", "completed", result, self.sha,
                                            "success", step_outcome)[0], "failure")

    def test_missing_contracted_target_never_becomes_not_applicable(self):
        (self.root / gate.TEST_SOURCE).unlink()
        (self.root / gate.CONTRACT).write_text("{}")
        state, description = status.outcome(self.root, "rust", "completed", {}, self.sha, "success")
        self.assertEqual(state, "failure")
        self.assertNotIn("Not applicable", description)

    def test_missing_result_reports_failure_even_if_other_steps_passed(self):
        self.publish()
        body = self.posts[0][1]
        self.assertEqual(body["state"], "failure")
        self.assertEqual(body["context"], "PDF export regression (Rust)")

    def test_legacy_success_is_explicit_and_bound_to_the_pr_sha(self):
        (self.root / gate.TEST_SOURCE).unlink()
        self.publish()
        self.assertEqual(self.posts[0][0], f"statuses/{self.sha}")
        self.assertEqual(self.posts[0][1]["state"], "success")
        self.assertIn("Not applicable", self.posts[0][1]["description"])

    def test_removed_contract_target_or_bdd_never_becomes_legacy_success(self):
        (self.root / gate.TEST_SOURCE).unlink()
        for path in status.PROTECTED:
            with self.subTest(path=path):
                self.posts = []
                self.pages = {1: [{"filename": path, "status": "removed"}]}
                self.publish()
                self.assertEqual(self.posts[0][1]["state"], "failure")

    def test_protected_removal_on_a_later_changed_files_page_is_checked(self):
        (self.root / gate.TEST_SOURCE).unlink()
        self.pages = {1: [{"filename": f"file-{n}", "status": "modified"} for n in range(100)],
                      2: [{"filename": next(iter(status.PROTECTED)), "status": "removed"}]}
        self.publish()
        self.assertEqual(self.posts[0][1]["state"], "failure")

    def test_old_run_does_not_publish_after_the_pr_has_advanced(self):
        self.publish(actual_pr_sha="b" * 40)
        self.assertEqual(self.posts, [])


class PdfUiGateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-pdf-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "frontend/appflowy_flutter"
        self.source = self.project / ui_gate.LEGACY_ENTRYPOINT
        self.source.parent.mkdir(parents=True)
        self.source.touch()
        self.output = self.root / "evidence"
        self.contract = self.project.parent / "rust-lib" / gate.CONTRACT
        self.contract.parent.mkdir(parents=True)
        self.name = "Database export all saved field values"
        self.contract.write_text(json.dumps({"ui": {"entrypoint": ui_gate.LEGACY_ENTRYPOINT,
                                                   "required_tests": [self.name]}}))
        self.skipped, self.suite_success, self.exit_code = False, True, 0
        self.extra_error = False
        self.emit_events = True
        self.commands = []

    def flutter(self, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(command[:5], ["flutter", "test", ui_gate.LEGACY_ENTRYPOINT, "-d", "macos"])
        events = Path(command[5].removeprefix("--file-reporter=json:"))
        data = [{"type": "testStart", "test": {"id": 1, "name": "(setUpAll)"}},
                {"type": "testDone", "testID": 1, "result": "success", "hidden": True},
                {"type": "testStart", "test": {"id": 2, "name": self.name}},
                {"type": "testDone", "testID": 2, "result": "success", "skipped": self.skipped},
                {"type": "done", "success": self.suite_success}]
        if self.extra_error:
            data.append({"type": "error", "error": "fixture native error"})
        if self.emit_events:
            events.write_text("".join(json.dumps(event) + "\n" for event in data))
        return subprocess.CompletedProcess(command, self.exit_code)

    def run_gate(self):
        with patch.object(ui_gate.subprocess, "run", side_effect=self.flutter), \
             patch.object(ui_gate, "desktop_revision", return_value="a" * 40):
            code = ui_gate.run(self.project, self.output)
        self.result = json.loads((self.output / "gate-result.json").read_text())
        return code

    def test_real_bdd_requires_successful_machine_events_and_exact_source(self):
        self.assertEqual(self.run_gate(), 0)
        self.assertEqual(self.result["completed_tests"], [self.name])
        self.assertEqual(self.result["desktop_sha"], "a" * 40)

    def test_skipped_bdd_cannot_pass_even_with_successful_exit_and_suite(self):
        self.skipped = True
        self.assertEqual(self.run_gate(), 1)

    def test_successful_test_cannot_hide_failed_suite_or_native_error(self):
        for flag in ("suite_success", "extra_error"):
            with self.subTest(flag=flag):
                self.suite_success, self.extra_error = True, False
                setattr(self, flag, flag == "extra_error")
                self.assertEqual(self.run_gate(), 1)

    def test_native_process_failure_is_preserved(self):
        self.exit_code = 1
        self.assertEqual(self.run_gate(), 1)

    def test_stale_events_do_not_make_a_new_empty_run_pass(self):
        self.assertEqual(self.run_gate(), 0)
        self.emit_events = False
        self.assertEqual(self.run_gate(), 1)
        self.assertEqual(self.result["status"], "failed")

    def test_missing_contracted_entrypoint_fails_without_running_flutter(self):
        self.source.unlink()
        self.assertEqual(self.run_gate(), 1)
        self.assertEqual(self.commands, [])

    def test_legacy_refs_skip_without_installing_or_running_a_test(self):
        self.source.unlink()
        self.contract.unlink()
        self.assertEqual(self.run_gate(), 0)
        self.assertEqual(self.result["status"], "legacy_skipped")
        self.assertEqual(self.commands, [])


class CloudPdfEventsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-cloud-pdf-events-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "frontend/appflowy_flutter"
        source = self.project / ui_gate.CLOUD_ENTRYPOINT
        source.parent.mkdir(parents=True)
        source.touch()
        contract = self.project.parent / "rust-lib" / gate.CONTRACT
        contract.parent.mkdir(parents=True)
        self.name = "Cloud database export A fresh reader exports all persisted values"
        contract.write_text(json.dumps({"cloud_ui": {"entrypoint": ui_gate.CLOUD_ENTRYPOINT,
                                                    "required_tests": [self.name]}}))
        self.events = self.root / "cloud-test-events.jsonl"
        self.output = self.root / "evidence"

    def verify(self, *, skipped=False, name=None, process_status=0):
        self.events.write_text("\n".join(json.dumps(event) for event in [
            {"type": "testStart", "test": {"id": 1, "name": name or self.name}},
            {"type": "testDone", "testID": 1, "result": "success", "skipped": skipped},
            {"type": "done", "success": True},
        ]))
        with patch.object(ui_gate, "desktop_revision", return_value="a" * 40):
            code = ui_gate.verify_cloud(self.project, self.output, self.events, process_status)
        self.result = json.loads((self.output / "gate-result.json").read_text())
        return code

    def test_actual_cloud_suite_events_must_include_the_contracted_scenario(self):
        self.assertEqual(self.verify(), 0)
        self.assertEqual(self.result["kind"], "cloud_ui")
        self.assertEqual(self.result["completed_tests"], [self.name])
        self.assertTrue((self.output / "test-events.jsonl").exists())

    def test_skipped_or_unregistered_cloud_scenario_cannot_report_success(self):
        self.assertEqual(self.verify(skipped=True), 1)
        self.assertEqual(self.verify(name="Unrelated smoke test"), 1)

    def test_native_failure_cannot_be_normalized_to_successful_events(self):
        self.assertEqual(self.verify(process_status=1), 1)
        self.assertEqual(self.result["status"], "failed")


class SourceToolchainTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-toolchain-")
        self.addCleanup(temporary.cleanup)
        self.frontend = Path(temporary.name) / "frontend"
        self.frontend.mkdir()

    def test_source_rust_pin_overrides_the_old_workflow_default(self):
        (self.frontend / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.98.0"\n')
        self.assertEqual(toolchain.select(self.frontend, "1.92.0"), {"RUST_TOOLCHAIN": "1.98.0"})

    def test_legacy_refs_keep_the_explicit_workflow_fallback(self):
        self.assertEqual(toolchain.select(self.frontend, "1.92.0"), {"RUST_TOOLCHAIN": "1.92.0"})

    def test_explicit_flutter_pin_is_honored_but_absence_does_not_invent_one(self):
        project = self.frontend / "appflowy_flutter"
        project.mkdir()
        (project / ".fvmrc").write_text('{"flutter": "3.41.9"}')
        self.assertEqual(toolchain.select(self.frontend, "1.92.0")["FLUTTER_VERSION"], "3.41.9")

    def test_nonpinned_or_environment_injecting_toolchain_is_rejected(self):
        for version in ("stable", "1.98.0\nGH_TOKEN=bad"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    toolchain.pinned_version(version, "Rust")


class PdfWorkflowTest(unittest.TestCase):
    def test_pdf_target_has_a_dedicated_unfiltered_gate_and_retained_evidence(self):
        workflow = yaml.safe_load((WORKFLOWS / "rust_ci.yaml").read_text())
        steps = workflow["jobs"]["test"]["steps"]
        gates = [step for step in steps if step.get("id") == "pdf-regression"]
        self.assertEqual(len(gates), 1, "module filters must not substitute for the full PDF target")
        gate_step = gates[0]
        self.assertEqual(gate_step["if"], "matrix.test_group == 'integration_group2'")
        self.assertIn("run_desktop_pdf_tests.py", gate_step["run"])
        self.assertNotIn("GH_TOKEN", gate_step.get("env", {}))
        artifact = next(step for step in steps if step.get("name") == "Upload PDF regression evidence")
        self.assertIn("always()", artifact["if"])
        self.assertEqual(artifact["with"]["retention-days"], 7)

    def test_sdk_selection_precedes_installs_in_both_rust_jobs(self):
        jobs = yaml.safe_load((WORKFLOWS / "rust_ci.yaml").read_text())["jobs"]
        for name in ("lint", "test"):
            with self.subTest(job=name):
                steps = jobs[name]["steps"]
                select = next(i for i, step in enumerate(steps) if step.get("name") == "Select SDK pins from the tested Desktop source")
                install = next(i for i, step in enumerate(steps) if step.get("name") == "Install Rust toolchain")
                self.assertLess(select, install)
                self.assertEqual(steps[install]["uses"], "dtolnay/rust-toolchain@stable")

    def test_macos_pdf_job_uses_the_matching_prepared_native_artifact(self):
        jobs = yaml.safe_load((WORKFLOWS / "flutter_ci.yaml").read_text())["jobs"]
        prepare, job = jobs["prepare-macos"], jobs["pdf_export_ui"]
        self.assertEqual(prepare["strategy"]["matrix"]["os"], ["macos-15-intel"])
        self.assertEqual(job["runs-on"], "macos-15-intel")
        self.assertEqual(job["needs"], ["prepare-macos"])
        self.assertEqual(job["if"], "always()")
        steps = job["steps"]
        checkout = next(step for step in steps if step.get("name") == "Checkout private repository")
        self.assertFalse(checkout["with"]["persist-credentials"])
        test = next(step for step in steps if step.get("id") == "pdf-ui-regression")
        self.assertIn("run_desktop_pdf_ui_tests.py", test["run"])
        self.assertEqual(test["if"], "steps.pdf-ui-source.outputs.enabled == 'true'")
        prepare_names = [step.get("name") for step in prepare["steps"]]
        self.assertLess(prepare_names.index("Build AppFlowy"),
                        prepare_names.index("Clean cargo build artifacts"))
        restore = next(step for step in steps if step.get("name") == "Restore the exact prepared macOS application and native FFI")
        self.assertIn("lipo -verify_arch x86_64", restore["run"])
        self.assertIn("libdart_ffi.a", restore["run"])
        upload = next(step for step in steps if step.get("name") == "Upload PDF UI regression evidence")
        self.assertEqual(upload["if"], "always()")

    def test_cloud_pdf_status_uses_actual_selected_suite_events_without_cleanup_override(self):
        jobs = yaml.safe_load((WORKFLOWS / "flutter_ci.yaml").read_text())["jobs"]
        steps = jobs["cloud_integration_test"]["steps"]
        test = next(step for step in steps if step.get("id") == "cloud-test-run")
        self.assertIn("--verify-cloud-events /tmp/cloud-test-events.jsonl", test["run"])
        self.assertIn("--process-status", test["run"])
        self.assertIn("!= pdf_export", test["run"])
        final = next(step for step in steps if step.get("name") == "Publish the Desktop cloud PDF UI result")
        self.assertEqual(final["if"], "always() && matrix.cloud_test_suite == 'pdf_export'")
        self.assertIn("steps.cloud-test-run.outcome", final["env"]["PDF_GATE_OUTCOME"])
        self.assertIn("--kind cloud_ui", final["run"])
        availability = jobs["pdf_cloud_ui_availability"]
        self.assertIn("'pdf_export'", availability["if"])
        self.assertIn("always()", availability["if"])

    def test_both_gates_publish_pending_and_always_final_results(self):
        for name, job_name in (("rust_ci.yaml", "test"), ("flutter_ci.yaml", "pdf_export_ui")):
            with self.subTest(workflow=name):
                steps = yaml.safe_load((WORKFLOWS / name).read_text())["jobs"][job_name]["steps"]
                pending = next(step for step in steps if "--state pending" in step.get("run", ""))
                final = next(step for step in steps if "--state completed" in step.get("run", ""))
                self.assertLess(steps.index(pending), steps.index(final))
                self.assertIn("always()", final["if"])
                self.assertIn("--job-status", final["run"])


if __name__ == "__main__":
    unittest.main()
