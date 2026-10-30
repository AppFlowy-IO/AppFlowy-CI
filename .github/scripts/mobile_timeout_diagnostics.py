"""Read bounded SDK lifecycle evidence only from the selected test app's sandbox."""

from collections import deque
from pathlib import Path
import re


BUNDLE_ID = "com.appflowy.appflowy.flutter"
SDK_PHASES = (
    "init_sdk: cleaning up existing core before restart",
    "cleanup_sdk: shutting down AppFlowy Core...",
    "cleanup_sdk: cleaning up resources (WebSocket, HTTP clients, etc.)",
    "cleanup_sdk: dropping sender to stop event loop",
    "cleanup_sdk: joining background thread",
    "cleanup_sdk: background thread joined successfully",
    "cleanup_sdk: AppFlowyCore dropped",
    "cleanup_sdk: complete",
)
MAX_FILES = 8
MAX_TAIL_BYTES = 128 * 1024


def runner_pid(processes):
    matches = re.findall(
        rf"^(\d+)\s+\S+\s+UIKitApplication:{re.escape(BUNDLE_ID)}\[[^\r\n]+$",
        processes, re.MULTILINE)
    return int(matches[0]) if len(matches) == 1 else None


def read_sdk_phases(container):
    temporary = Path(container) / "tmp"
    events = deque(maxlen=64)
    files_read = 0
    bytes_read = 0
    candidates = temporary.glob(
        "appflowy_bdd_paths_*/temporary/appflowy_integration_test/*/log.*")
    # Raw messages, spans, account values and file paths never leave the app.
    # A fixed lifecycle allowlist is enough to locate an unfinished SDK shutdown.
    for index, file in enumerate(candidates):
        if index >= 128 or files_read >= MAX_FILES:
            break
        if not re.fullmatch(r"log\.\d{4}-\d{2}-\d{2}", file.name):
            continue
        owned_path = (file, *file.parents[:6])
        if any(part.is_symlink() for part in owned_path):
            continue
        if not file.is_file():
            continue
        try:
            with file.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - MAX_TAIL_BYTES))
                content = stream.read(MAX_TAIL_BYTES)
        except OSError:
            continue
        files_read += 1
        bytes_read += len(content)
        for line in content.decode("utf-8", errors="replace").splitlines():
            for phase in SDK_PHASES:
                if phase in line:
                    timestamp = re.search(r"\b\d{2}-\d{2} \d{2}:\d{2}:\d{2}\b", line)
                    events.append({"phase": phase,
                                   "local_timestamp": timestamp[0] if timestamp else None})
                    break
    return {"files_read": files_read, "bytes_read": bytes_read, "phases": list(events)}
