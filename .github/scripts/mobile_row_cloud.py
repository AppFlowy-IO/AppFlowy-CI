"""Prepare private, job-owned Cloud and simulator configuration for iOS runner 4.

The simulator cannot rely on the host's environment. Generate its Dart defines
and GoTrue's configuration together so mock-login credentials cannot diverge.
This script never starts Docker or edits an existing deployment configuration.
"""

import argparse
import json
import os
from pathlib import Path
import secrets


ROW_PAGE_TEST = "integration_test/mobile_row_page_cloud_runner.dart"


def private_write(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write(content)
    except OSError:
        # Exclusive creation above proves this is our new file, including when a
        # buffered write or close fails after a partial credential was written.
        path.unlink(missing_ok=True)
        raise


def prepare(cloud: Path, desktop: Path, temporary: Path) -> Path:
    app = desktop / "frontend/appflowy_flutter"
    if not (app / ROW_PAGE_TEST).is_file():
        raise ValueError("The selected Desktop revision has no mobile row-page tests")
    config_source = (app / "integration_test/shared/cloud_test_config.dart").read_text()
    for define in ("APPFLOWY_TEST_GOTRUE_ADMIN_EMAIL", "APPFLOWY_TEST_GOTRUE_ADMIN_PASSWORD"):
        if define not in config_source:
            raise ValueError("The selected Desktop revision lacks simulator credential defines")
    env_path = cloud / ".env"
    defines_path = temporary / "mobile-row-cloud-defines.json"
    if env_path.exists() or defines_path.exists():
        raise ValueError("Refusing to overwrite an existing Cloud or simulator configuration")

    email = f"ios-ci-{secrets.token_hex(8)}@appflowy.io"
    password = secrets.token_urlsafe(32)
    overrides = {
        "FQDN": "localhost",
        "SCHEME": "http",
        "WS_SCHEME": "ws",
        "API_EXTERNAL_URL": "http://localhost",
        "GOTRUE_ADMIN_EMAIL": email,
        "GOTRUE_ADMIN_PASSWORD": password,
        "GOTRUE_MAILER_AUTOCONFIRM": "true",
        "APPFLOWY_SPAM_DETECT_ENABLED": "false",
        "APPFLOWY_DATABASE_BLOB_APPEND_FLUSH_INTERVAL_MS": "500",
        "APPFLOWY_FEATURE_RULES_DOMAIN": "appflowy.io",
    }
    lines = [
        line for line in (cloud / "deploy.env").read_text().splitlines()
        if line.partition("=")[0].strip() not in overrides
    ]
    # Values are generated safe identifiers or fixed test settings. External
    # secrets stay in process environment; never implement dotenv secret escaping.
    # Derived deploy.env values expand while the file is parsed, so these must
    # precede the template's ${SCHEME}/${FQDN}/${API_EXTERNAL_URL} references.
    lines = [f"{key}='{value}'" for key, value in overrides.items()] + [""] + lines
    defines = {
        "CI": "true",
        "APPFLOWY_CLOUD_URL": "http://localhost",
        "APPFLOWY_CLOUD_FORCE_SELF_HOST": "true",
        "APPFLOWY_TEST_GOTRUE_URL": "http://localhost/gotrue",
        "APPFLOWY_TEST_GOTRUE_ADMIN_EMAIL": email,
        "APPFLOWY_TEST_GOTRUE_ADMIN_PASSWORD": password,
    }
    private_write(env_path, "\n".join(lines) + "\n")
    try:
        private_write(defines_path, json.dumps(defines) + "\n")
    except OSError:
        # This file was created by this call; an existing file is never removed.
        env_path.unlink()
        raise
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{email}")
        print(f"::add-mask::{password}")
    return defines_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", type=Path, required=True)
    parser.add_argument("--desktop", type=Path, required=True)
    parser.add_argument("--temporary", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.cloud, args.desktop, args.temporary)
    except (OSError, ValueError) as error:
        # Do not surface configuration contents or provisioning credentials.
        raise SystemExit(f"Mobile Cloud configuration failed: {type(error).__name__}") from None
    print("Prepared private Cloud and simulator configuration for the focused row-page tests")


if __name__ == "__main__":
    main()
