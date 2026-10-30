"""Keep Desktop CI Linux-only without dropping the focused PDF UI gate."""

from pathlib import Path
import io
import subprocess
import tarfile
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/flutter_ci.yaml"


class DesktopLinuxOnlyTest(unittest.TestCase):
    def setUp(self):
        self.jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]

    def test_desktop_workflow_has_no_windows_or_macos_jobs(self):
        self.assertNotIn("prepare-windows", self.jobs)
        self.assertNotIn("prepare-macos", self.jobs)
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                matrix = job.get("strategy", {}).get("matrix", {})
                if "os" in matrix:
                    self.assertEqual(matrix["os"], ["ubuntu-latest"])
                    self.assertTrue(all(item.get("os", "ubuntu-latest") == "ubuntu-latest"
                                        for item in matrix.get("include", [])))
                else:
                    self.assertEqual(job["runs-on"], "ubuntu-latest")

    def test_all_dependencies_and_artifacts_have_a_linux_producer(self):
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                for dependency in job.get("needs", []):
                    self.assertIn(dependency, self.jobs)
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/download-artifact@"):
                        artifact = step["with"]["name"]
                        self.assertNotRegex(artifact.lower(), r"windows|macos")
                        self.assertIn("${{ github.run_id }}", artifact)
                        if "matrix.os" in artifact:
                            self.assertEqual(job["strategy"]["matrix"]["os"], ["ubuntu-latest"])
                        else:
                            self.assertEqual(artifact, "${{ github.run_id }}-ubuntu-latest")
        prepare = self.jobs["prepare-linux"]["steps"]
        upload = next(step for step in prepare if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["name"], "${{ github.run_id }}-${{ matrix.os }}")

    def test_pdf_gate_uses_native_linux_ffi_pdfium_and_a_virtual_display(self):
        job = self.jobs["pdf_export_ui"]
        self.assertEqual(job["needs"], ["prepare-linux"])
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["if"], "always() && !cancelled()")
        steps = job["steps"]
        install = next(step for step in steps if step.get("name") == "Install prerequisites")
        self.assertEqual(install["timeout-minutes"], 10)
        for dependency in ("libgtk-3-dev", "libmpv-dev", "libepoxy-dev", "xvfb", "xauth"):
            self.assertIn(dependency, install["run"])
        restore = next(step for step in steps if step.get("name") == "Restore the exact prepared Linux application and native FFI")
        self.assertIn("linux/flutter/dart_ffi/libdart_ffi.so", restore["run"])
        self.assertIn("Expected an AMD64 Linux FFI library", restore["run"])
        pdfium = next(step for step in steps if step.get("name") == "Prepare PDFium for Linux integration tests")
        self.assertIn("APPFLOWY_PDFIUM_MODULE_PATH", pdfium["run"])
        test = next(step for step in steps if step.get("id") == "pdf-ui-regression")
        self.assertIn("--device linux", test["run"])
        self.assertIn("xvfb-run", test["run"])
        self.assertLess(steps.index(install), steps.index(test))
        self.assertLess(steps.index(restore), steps.index(test))
        self.assertLess(steps.index(pdfium), steps.index(test))

    def test_expensive_pdf_setup_remains_conditional_for_legacy_refs(self):
        steps = self.jobs["pdf_export_ui"]["steps"]
        names = {"Configure Ubuntu package downloads", "Install prerequisites",
                 "Enable Flutter Desktop", "Install Rust toolchain", "Install Flutter",
                 "Restore the exact prepared Linux application and native FFI",
                 "Resolve Flutter packages for the restored source",
                 "Prepare PDFium for Linux integration tests"}
        for step in steps:
            if step.get("name") in names or step.get("id") == "pdf-ui-regression":
                with self.subTest(step=step.get("name")):
                    self.assertEqual(step["if"], "steps.pdf-ui-source.outputs.enabled == 'true'")

    def test_restore_accepts_linux_amd64_and_rejects_a_macos_native_artifact(self):
        steps = self.jobs["pdf_export_ui"]["steps"]
        restore = next(step for step in steps if step.get("name") == "Restore the exact prepared Linux application and native FFI")
        for header, succeeds in ((b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\0", True),
                                 (b"\xcf\xfa\xed\xfe" + b"\0" * 16, False)):
            with self.subTest(succeeds=succeeds), tempfile.TemporaryDirectory(prefix="pdf-linux-artifact-") as temporary:
                root = Path(temporary)
                with tarfile.open(root / "appflowy_flutter.tar.gz", "w:gz") as archive:
                    item = tarfile.TarInfo("frontend/appflowy_flutter/linux/flutter/dart_ffi/libdart_ffi.so")
                    item.size = len(header)
                    archive.addfile(item, io.BytesIO(header))
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", restore["run"]],
                                        cwd=root, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                if not succeeds:
                    self.assertIn("Expected an AMD64 Linux FFI library", result.stderr)

    def test_pdf_statuses_and_evidence_are_preserved(self):
        steps = self.jobs["pdf_export_ui"]["steps"]
        final = next(step for step in steps if step.get("name") == "Publish the Desktop PDF UI result")
        self.assertEqual(final["if"], "always()")
        self.assertIn("--kind ui", final["run"])
        self.assertIn("steps.pdf-ui-regression.outcome", final["env"]["PDF_GATE_OUTCOME"])
        artifact = next(step for step in steps if step.get("name") == "Upload PDF UI regression evidence")
        self.assertEqual(artifact["with"]["retention-days"], 7)
        cloud = self.jobs["cloud_integration_test"]["steps"]
        cloud_result = next(step for step in cloud if step.get("name") == "Publish the Desktop cloud PDF UI result")
        self.assertIn("--kind cloud_ui", cloud_result["run"])


if __name__ == "__main__":
    unittest.main()
