"""Run a bounded native Metal control and require a fresh, complete report."""

import argparse
import json
from pathlib import Path
import subprocess


SOURCE = Path(__file__).resolve().parents[1] / "probes/metal_msaa.swift"


def run_probe(output: Path, source: Path = SOURCE, execute=subprocess.run) -> bool:
    # A previous report must never turn a crashed or unexecuted control green.
    output.mkdir(parents=True, exist_ok=False)
    binary = output / "metal-control"
    report = output / "metal-report.json"
    summary = {"success": False, "phase": "compile"}
    try:
        with (output / "compile.log").open("w") as log:
            execute(["xcrun", "swiftc", "-O", str(source), "-o", str(binary)],
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
        summary["phase"] = "render"
        with (output / "render.log").open("w") as log:
            execute([str(binary), str(report)], stdout=log, stderr=subprocess.STDOUT,
                    check=True, timeout=105)
        summary["phase"] = "validate"
        result = json.loads(report.read_text())
        if not (isinstance(result, dict)
                and result.get("success") is True and result.get("sample_count") == 4
                and type(result.get("rendered_iterations")) is int
                and result["rendered_iterations"] >= 100
                and type(result.get("duration_ms")) is int
                and result["duration_ms"] >= 90000 and result.get("errors") == []):
            raise ValueError("Rendering control did not complete its required work")
        summary.update(success=True, phase="complete", report=result)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        summary["error_type"] = type(error).__name__
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary["success"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not run_probe(args.output):
        raise SystemExit("Intel runtime rendering control failed; retained its logs/report")


if __name__ == "__main__":
    main()
