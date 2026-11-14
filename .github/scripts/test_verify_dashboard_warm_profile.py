"""Exercise warm performance evidence and the actual workflow shell."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

import verify_dashboard_warm_profile as profile


ROOT = Path(__file__).resolve().parents[2]


def report():
    return {
        "buildMode": "profile",
        "observation": {
            "openedAt": 10000, "deadlineMs": 3000, "metDeadline": True,
            "widgetCount": 12,
            "visibleViews": [{"sourceId": "employees", "firstDataAt": 13000}],
        },
        "backend": {"sourceOpens": {}, "rowLoadPasses": {}, "rowsReadFromStorage": {}},
    }


class WarmProfileTest(unittest.TestCase):
    def test_accepts_the_original_deadline_with_active_native_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "warm-1.json").write_text(json.dumps(report()))
            self.assertEqual(profile.verify(root), [{"visible_widgets": 1, "slowest_first_data_ms": 3000}])

    def test_rejects_missing_measurements_and_invalid_mode_deadline_or_counters(self):
        original = report()
        invalid = []
        for key, value in (("buildMode", "debug"), ("backend", {}), ("observation", None)):
            item = copy.deepcopy(original)
            item[key] = value
            invalid.append(item)
        for key, value in (("deadlineMs", 4000), ("metDeadline", False),
                           ("widgetCount", 6), ("visibleViews", []), ("openedAt", True)):
            item = copy.deepcopy(original)
            item["observation"][key] = value
            invalid.append(item)
        for value in (13001, 9999, None, "12000"):
            item = copy.deepcopy(original)
            item["observation"]["visibleViews"][0]["firstDataAt"] = value
            invalid.append(item)
        for key in original["backend"]:
            for value in (1, False):
                item = copy.deepcopy(original)
                item["backend"][key]["employees"] = value
                invalid.append(item)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                profile.verify(root)
            for item in invalid:
                with self.subTest(report=item):
                    (root / "warm-1.json").write_text(json.dumps(item))
                    with self.assertRaises(ValueError):
                        profile.verify(root)

    def test_rejects_symlinked_or_oversized_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "unrelated.json"
            source.write_text(json.dumps(report()))
            target = root / "warm-1.json"
            target.symlink_to(source)
            with self.assertRaises(ValueError):
                profile.verify(root)
            target.unlink()
            target.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            with self.assertRaises(ValueError):
                profile.verify(root)

    def run_workflow(self, directory, *, code=0, evidence=True, stale=False):
        root = Path(directory)
        workflow = yaml.safe_load((ROOT / ".github/workflows/flutter_ci.yaml").read_text())
        job = workflow["jobs"]["cloud_integration_test"]
        step = next(step for step in job["steps"] if step.get("id") == "cloud-test-run")
        self.assertEqual(step["timeout-minutes"], 45)
        self.assertEqual(job["timeout-minutes"], 60)
        upload = next(step for step in job["steps"] if step.get("name") == "Upload cloud test output")
        self.assertIn("${{ runner.temp }}/dashboard-warm-profile/", upload["with"]["path"])
        (root / "frontend/appflowy_flutter").mkdir(parents=True)
        helper = root / ".desktop-ci-tools/.github/scripts/verify_dashboard_warm_profile.py"
        helper.parent.mkdir(parents=True)
        shutil.copyfile(profile.__file__, helper)
        temporary = root / "temporary"
        temporary.mkdir()
        if stale:
            (temporary / "dashboard-warm-profile").mkdir()
        stub = root / "flutter.py"
        stub.write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "Path(os.environ['CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
            "if os.environ['EVIDENCE']=='true':\n"
            " out=next(a.split('=',2)[2] for a in sys.argv if a.startswith('--dart-define=DASHBOARD_LOADING_REPORT_DIR='))\n"
            " (Path(out)/'warm-1.json').write_text(os.environ['REPORT'])\n"
            "print('All tests passed.')\n"
            "sys.exit(int(os.environ['STUB_EXIT']))\n"
        )
        script = (
            'flutter() { if [ "$1" = pub ]; then return 0; fi; python3 "$STUB" "$@"; }\n'
            'dart() { printf "%s\\n" "$*" >> "$DART_CALLS"; }\n'
            'docker() { :; }\nsudo() { :; }\n'
            + step["run"].replace("${{ matrix.cloud_test_suite }}", "dashboard_loading_warm")
            .replace("/tmp/test_output.txt", str(root / "output.log"))
            .replace("/tmp/cloud-test-events.jsonl", str(root / "events.jsonl"))
        )
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
            env={**os.environ, "RUNNER_TEMP": str(temporary), "GITHUB_WORKSPACE": directory,
                 "APPFLOWY_CLOUD_URL": "https://localhost", "STUB": str(stub),
                 "STUB_EXIT": str(code), "EVIDENCE": str(evidence).lower(),
                 "REPORT": json.dumps(report()), "CALLS": str(root / "calls"),
                 "DART_CALLS": str(root / "dart-calls")},
            capture_output=True, text=True, timeout=5,
        )

    def test_workflow_preserves_driver_failure_and_requires_evidence_for_success(self):
        for code, evidence, expected in ((0, True, 0), (0, False, 1), (17, True, 17)):
            with self.subTest(code=code, evidence=evidence), tempfile.TemporaryDirectory() as directory:
                result = self.run_workflow(directory, code=code, evidence=evidence)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                root = Path(directory)
                args = json.loads((root / "calls").read_text())
                self.assertEqual(args[:2], ["drive", "--profile"])
                self.assertIn("--driver=integration_test/desktop/perf/dashboard_perf_driver.dart", args)
                self.assertIn("--target=integration_test/desktop/cloud/cloud_runner.dart", args)
                self.assertIn("--dart-define=CLOUD_TEST_SUITE=dashboard_loading_warm", args)
                self.assertIn("--dart-define=APPFLOWY_CLOUD_URL=https://localhost", args)
                self.assertNotIn("--coverage", args)
                self.assertNotIn("All 0 cloud integration tests passed", result.stdout)
                self.assertIn("pdfrx:remove_wasm_modules --revert", (root / "dart-calls").read_text())

    def test_workflow_refuses_stale_timing_reports_before_invoking_the_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_workflow(directory, stale=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / "calls").exists())


if __name__ == "__main__":
    unittest.main()
