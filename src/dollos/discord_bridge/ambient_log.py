"""AmbientLog — full capture + msg_id dedup + retention.

Provides Discord message logging with:
- Full message capture to {root}/discord/{guild_id}/{channel_id}/{date}.jsonl
- msg_id dedup per channel/date (restart-safe by lazy loading seen IDs from file)
- Retention-based pruning of old date files
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class AmbientLog:
    """Discord message logger with dedup and retention."""

    def __init__(self, root: Path, retention_days: int):
        """Initialize AmbientLog.

        Args:
            root: Root directory for log files
            retention_days: Number of days to retain log files
        """
        self.root = Path(root)
        self.retention_days = retention_days
        # Per-(guild, channel, date) cache of seen msg_ids
        self._seen_ids: dict[tuple[str, str, str], set[str]] = {}

    def append(self, guild_id: str, channel_id: str, event: dict) -> bool:
        """Append message to log if not already seen.

        Args:
            guild_id: Discord guild ID
            channel_id: Discord channel ID
            event: Event dict containing "msg_id", "date", and other fields

        Returns:
            True if appended (new msg_id), False if already logged (dedup)
        """
        msg_id = event["msg_id"]
        date = event["date"]
        key = (guild_id, channel_id, date)

        # Lazy load seen IDs from file if not in cache
        if key not in self._seen_ids:
            self._seen_ids[key] = self._load_seen_ids(guild_id, channel_id, date)

        # Check if already seen
        if msg_id in self._seen_ids[key]:
            return False

        # Append to file
        log_path = self.root / "discord" / guild_id / channel_id / f"{date}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")

        # Track in memory
        self._seen_ids[key].add(msg_id)

        return True

    def _load_seen_ids(self, guild_id: str, channel_id: str, date: str) -> set[str]:
        """Load already-logged msg_ids from file for this channel/date.

        Args:
            guild_id: Discord guild ID
            channel_id: Discord channel ID
            date: ISO date string (YYYY-MM-DD)

        Returns:
            Set of msg_ids already in the log file (empty if file doesn't exist)
        """
        log_path = self.root / "discord" / guild_id / channel_id / f"{date}.jsonl"
        seen = set()

        if log_path.exists():
            try:
                for line in log_path.read_text().splitlines():
                    if line.strip():
                        try:
                            obj = json.loads(line)
                            if "msg_id" in obj:
                                seen.add(obj["msg_id"])
                        except json.JSONDecodeError:
                            pass
            except (OSError, ValueError):
                pass

        return seen

    def prune(self, today: str | None = None) -> None:
        """Delete log files older than retention_days.

        Args:
            today: ISO date string (YYYY-MM-DD). If None, uses datetime.date.today()
        """
        if today is None:
            today = datetime.today().date().isoformat()

        today_date = datetime.fromisoformat(today).date()
        cutoff_date = today_date - timedelta(days=self.retention_days)

        discord_root = self.root / "discord"
        if not discord_root.exists():
            return

        # Walk through all guild/channel/date.jsonl files
        for guild_dir in discord_root.iterdir():
            if not guild_dir.is_dir():
                continue
            for channel_dir in guild_dir.iterdir():
                if not channel_dir.is_dir():
                    continue
                for log_file in channel_dir.iterdir():
                    if not log_file.is_file() or not log_file.suffix == ".jsonl":
                        continue
                    # Extract date from filename (e.g., "2026-07-03.jsonl")
                    try:
                        date_str = log_file.stem
                        file_date = datetime.fromisoformat(date_str).date()
                        if file_date < cutoff_date:
                            log_file.unlink()
                    except (ValueError, OSError):
                        pass
