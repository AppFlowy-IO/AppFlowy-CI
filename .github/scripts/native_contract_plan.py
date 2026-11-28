"""Select native Cloud contracts from the verified private PR diff."""

import json
import os
from pathlib import Path
import re
import subprocess
import tomllib


ALL = {"cache", "permission", "presentation"}
PERMISSION = {"script/permission_model_test_runner.py", "script/test_permission_model_test_runner.py"}
PRESENTATION = {
    "script/test_presentation_acceptance.py", "script/test_presentation_gateway_contract.py",
    "script/presentation_service_images.py", "script/test_presentation_service_images.py",
    "tests/presentation_acceptance.rs",
}


def changed_paths(request, sha, pr):
    """The first compare page includes at most 300 files, including both sides of renames."""
    pull = request(f"pulls/{pr}")
    if pull["head"]["sha"] != sha or not re.fullmatch(r"[0-9a-f]{40}", pull["base"]["sha"]):
        return None
    comparison = request(f"compare/{pull['base']['sha']}...{sha}?per_page=1")
    files = comparison["files"]
    if not isinstance(files, list) or not 0 < len(files) < 300:
        return None
    paths = []
    for file in files:
        paths.append(file["filename"])
        if file["status"] == "renamed":
            paths.append(file["previous_filename"])
        elif file["status"] not in ("added", "removed", "modified", "changed"):
            return None
    return paths


def standalone_ai(source):
    packages = tomllib.loads((source / "Cargo.lock").read_text())["package"]
    return (any(p["name"] == "appflowy-ai" for p in packages)
            and not any(d.split()[0] == "appflowy-ai" for p in packages
                        for d in p.get("dependencies", [])))


def select(paths, source):
    if not paths:
        return ALL
    selected = set()
    for path in paths:
        if path.startswith("doc/context/") and path.endswith(".md"):
            continue
        if path in PERMISSION:
            selected.add("permission")
        elif path in PRESENTATION or path.startswith("tests/presentation_acceptance/"):
            selected.add("presentation")
        elif path == "script/test_durable_recovery_services.py":
            selected.add("cache")
        elif path.startswith("services/appflowy-ai/src/image/") and standalone_ai(source):
            selected.add("presentation")
        else:
            return ALL
    return selected


def main():
    env = os.environ
    phases = json.loads(env["AVAILABLE_PHASES"])
    selected = ALL
    pr = env.get("SOURCE_PR", "")
    if (env["GITHUB_EVENT_NAME"] == "repository_dispatch" and phases == ["cache", "permission"]
            and env["SOURCE_EVENT"] in ("pr_opened", "pr_sync") and re.fullmatch(r"[1-9]\d*", pr)
            and re.fullmatch(r"[0-9a-f]{40}", env["SOURCE_SHA"])):
        def request(endpoint):
            command = ["gh", "api", "repos/AppFlowy-IO/AppFlowy-Cloud-Premium/" + endpoint]
            return json.loads(subprocess.check_output(command, text=True, timeout=45))
        try:
            selected = select(changed_paths(request, env["SOURCE_SHA"], pr), Path.cwd())
        except (OSError, KeyError, ValueError, TypeError, subprocess.SubprocessError):
            print("::notice::Private PR diff unavailable or incomplete. Running all native contracts.")
    matrix = phases if selected == ALL else [phase for phase in phases if phase in selected]
    print("Selected native contracts: " + (", ".join(sorted(selected)) or "none"))
    with open(env["GITHUB_OUTPUT"], "a") as output:
        print(f"cache={str(bool(matrix)).lower()}", file=output)
        print(f"phases={json.dumps(matrix or ['all'])}", file=output)
        print(f"share={str(matrix == ['cache', 'permission']).lower()}", file=output)
        print(f"presentation={str('presentation' in selected).lower()}", file=output)


if __name__ == "__main__":
    main()
