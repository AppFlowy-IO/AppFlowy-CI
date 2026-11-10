"""Require an executed profile-mode warm deadline and active native counters.

The integration driver propagates the entire suite's result. This additional
evidence prevents an empty/skipped suite or a build without native counters from
being accepted as a performance pass.
"""

import argparse
import json
from pathlib import Path


def verify(directory):
    reports = sorted(directory.glob("warm-*.json"))
    if not reports:
        raise ValueError("No warm-dashboard timing report was produced")
    measurements = []
    for path in reports:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Invalid warm-dashboard timing report")
        report = json.loads(path.read_text())
        if not isinstance(report, dict) or report.get("buildMode") != "profile":
            raise ValueError("Warm-dashboard timing requires a profile build")
        observation = report.get("observation")
        backend = report.get("backend")
        if not isinstance(observation, dict) or not isinstance(backend, dict):
            raise ValueError("Warm-dashboard observation or native counters are missing")
        opened = observation.get("openedAt")
        views = observation.get("visibleViews")
        if (type(opened) is not int or observation.get("deadlineMs") != 3000
                or observation.get("metDeadline") is not True
                or observation.get("widgetCount") != 12
                or not isinstance(views, list) or not views):
            raise ValueError("Warm-dashboard fixture or three-second deadline was not verified")
        for key in ("sourceOpens", "rowLoadPasses", "rowsReadFromStorage"):
            if not isinstance(backend.get(key), dict):
                raise ValueError("Required native load counters are unavailable")
        elapsed = []
        for view in views:
            if not isinstance(view, dict):
                raise ValueError("Invalid warm-dashboard visible-view observation")
            source = view.get("sourceId")
            first = view.get("firstDataAt")
            if (not isinstance(source, str) or not source or type(first) is not int
                    or not 0 <= first - opened <= 3000):
                raise ValueError("A visible warm-dashboard widget missed its deadline")
            for key in ("sourceOpens", "rowLoadPasses", "rowsReadFromStorage"):
                value = backend[key].get(source, 0)
                if type(value) is not int or value != 0:
                    raise ValueError("A warm-dashboard source performed additional native loading")
            elapsed.append(first - opened)
        measurements.append({"visible_widgets": len(views), "slowest_first_data_ms": max(elapsed)})
    return measurements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    args = parser.parse_args()
    try:
        measurements = verify(args.reports)
    except (OSError, ValueError, TypeError) as error:
        # Native report payloads and paths are not needed in the public log.
        parser.exit(1, f"Warm-dashboard profile evidence failed: {type(error).__name__}\n")
    print("Warm-dashboard profile suite passed; verified timing measurements: "
          + json.dumps(measurements))


if __name__ == "__main__":
    main()
