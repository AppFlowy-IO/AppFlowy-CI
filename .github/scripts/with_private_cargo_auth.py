#!/usr/bin/env python3
"""Give one CI command private Cargo Git access without persisting credentials."""

import base64
import os
import subprocess
import sys


def command_environment(environment):
    result = dict(environment)
    token = result.pop("PRESENTATION_CARGO_TOKEN", "")
    if not token or any(character in token for character in "\r\n"):
        raise ValueError("PRESENTATION_CARGO_TOKEN is required for private Cargo dependencies")
    count = int(result.get("GIT_CONFIG_COUNT", "0"))
    if not 0 <= count <= 100:
        raise ValueError("Invalid inherited Git configuration count")
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # Cargo uses the Git CLI in this workflow. Git applies this header only to
    # HTTPS repositories in our organization, including transitive dependencies.
    result.update({
        "CARGO_NET_GIT_FETCH_WITH_CLI": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": str(count + 1),
        f"GIT_CONFIG_KEY_{count}": "http.https://github.com/AppFlowy-IO/.extraheader",
        f"GIT_CONFIG_VALUE_{count}": f"AUTHORIZATION: basic {credential}",
    })
    return result, credential


def main():
    command = sys.argv[1:]
    if not command:
        raise ValueError("A command is required")
    environment, credential = command_environment(os.environ)
    # GitHub masks the raw secret automatically, but not its Basic-auth encoding.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{credential}", flush=True)
    return subprocess.run(command, env=environment, check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
