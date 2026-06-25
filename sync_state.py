"""Cross-device state sync via a user-provided sync folder.

Uses a shared folder (Dropbox, iCloud, Google Drive, or any file-syncing service)
to keep breaktimer state consistent across devices. Last-write-wins by timestamp.

Shared fields: remaining_time, daily_work_totals, is_active, last_activity_time
Device-local: elapsed_since_last_activity, last_saved_time (display/tracking only)
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("breaktimer.sync")

SYNC_FILENAME = "state.sync.json"
SYNC_INTERVAL_SECONDS = 30  # how often to check/push sync file


@dataclass
class SyncState:
    """The cross-device sync state — shared fields only."""
    remaining_time: float
    daily_work_totals: dict  # {date_str: seconds}
    is_active: bool
    last_activity_time: float  # monotonic
    last_saved_time: float    # wall-clock, for conflict resolution

    def to_dict(self) -> dict:
        return {
            "remaining_time": self.remaining_time,
            "daily_work_totals": self.daily_work_totals,
            "is_active": self.is_active,
            "last_activity_time": self.last_activity_time,
            "last_saved_time": self.last_saved_time,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SyncState | None":
        if not data:
            return None
        return cls(
            remaining_time=data.get("remaining_time", 0.0),
            daily_work_totals=data.get("daily_work_totals", {}),
            is_active=data.get("is_active", False),
            last_activity_time=data.get("last_activity_time", 0.0),
            last_saved_time=data.get("last_saved_time", 0.0),
        )


def sync_folder() -> Optional[str]:
    """Get the sync folder path from environment or config.

    Set BREAKTIMER_SYNC_FOLDER env var or ~/.config/breaktimer/sync-path
    """
    path = os.environ.get("BREAKTIMER_SYNC_FOLDER")
    if path:
        return path
    config_dir = os.path.expanduser("~/.config/breaktimer")
    config_file = os.path.join(config_dir, "sync-path")
    if os.path.isfile(config_file):
        with open(config_file) as f:
            return f.read().strip()
    return None


def ensure_sync_dir() -> Optional[str]:
    """Ensure the sync directory exists. Returns path or None."""
    folder = sync_folder()
    if not folder:
        log.warning("BREAKTIMER_SYNC_FOLDER not set — cross-device sync disabled")
        return None
    folder = os.path.expanduser(folder)
    os.makedirs(folder, exist_ok=True)
    return folder


def sync_file_path() -> Optional[str]:
    """Get the full path to the sync file."""
    folder = ensure_sync_dir()
    if not folder:
        return None
    return os.path.join(folder, SYNC_FILENAME)


def load_synced_state() -> Optional[SyncState]:
    """Load state from the sync folder if it exists and is newer."""
    path = sync_file_path()
    if not path or not os.path.isfile(path):
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return SyncState.from_dict(data)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("failed to load sync state: %s", e)
        return None


def save_synced_state(state: SyncState) -> bool:
    """Save state to the sync folder. Returns success."""
    path = sync_file_path()
    if not path:
        return False

    try:
        # Atomic write: write to temp file then rename
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state.to_dict(), f)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.warning("failed to save sync state: %s", e)
        return False


def should_sync(sync_time: float, last_sync: float) -> bool:
    """Check if enough time has passed to warrant a sync check."""
    return sync_time - last_sync >= SYNC_INTERVAL_SECONDS


def merge_states(local: SyncState, remote: SyncState) -> SyncState:
    """Merge local and remote states using last-write-wins.

    The device with the newer last_saved_time wins.
    """
    if remote.last_saved_time > local.last_saved_time:
        log.info("sync: using remote state (last_saved_time=%.1f)", remote.last_saved_time)
        return remote
    else:
        log.info("sync: using local state (last_saved_time=%.1f)", local.last_saved_time)
        return local
