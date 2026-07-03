"""AmbientLog — full capture + msg_id dedup + retention.

Provides Discord message logging with:
- Full message capture to {root}/discord/{guild_id}/{channel_id}/{date}.jsonl
- msg_id dedup per channel/date (restart-safe by lazy loading seen IDs from file)
- Retention-based pruning of old date files

Surface-not-blank / no-fallback convention (CLAUDE.md): this is finetune-corpus
data. Errors are never silently swallowed. A single torn tail line in a log file
is skip-and-continue but logged loudly; a file-level OSError is logged AND raised
(never return a partial/empty seen-set, which would make a seen msg_id look unseen
and re-write a duplicate into the corpus with no trace).
"""
import json
import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


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
        event_date = event["date"]
        key = (guild_id, channel_id, event_date)

        # Lazy load seen IDs from file if not in cache (restart-safe dedup)
        if key not in self._seen_ids:
            self._seen_ids[key] = self._load_seen_ids(guild_id, channel_id, event_date)

        # Check if already seen
        if msg_id in self._seen_ids[key]:
            return False

        # Append to file
        log_path = self.root / "discord" / guild_id / channel_id / f"{event_date}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")

        # Track in memory
        self._seen_ids[key].add(msg_id)

        return True

    def _load_seen_ids(self, guild_id: str, channel_id: str, event_date: str) -> set[str]:
        """Load already-logged msg_ids from file for this channel/date.

        Args:
            guild_id: Discord guild ID
            channel_id: Discord channel ID
            event_date: ISO date string (YYYY-MM-DD)

        Returns:
            Set of msg_ids already in the log file (empty if file doesn't exist)

        Raises:
            OSError: on a file-level read failure. Surface-not-blank: a transient
                OSError must NOT be swallowed into an empty seen-set, or a seen
                msg_id would look unseen and get re-written as a duplicate into
                the finetune corpus. Log loudly and propagate.
        """
        log_path = self.root / "discord" / guild_id / channel_id / f"{event_date}.jsonl"
        seen: set[str] = set()

        if not log_path.exists():
            return seen

        try:
            raw = log_path.read_text()
        except OSError:
            # File-level failure: log + raise (do NOT return an empty seen-set,
            # which would corrupt the corpus with silent duplicates).
            logger.exception("failed to read ambient log %s for dedup", log_path)
            raise

        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # A torn tail line (partial write before a crash) is tolerable to
                # skip for THAT line — but log it loudly, never swallow silently.
                logger.warning(
                    "skipping corrupt line in ambient log %s: %r", log_path, line
                )
                continue
            if "msg_id" in obj:
                seen.add(obj["msg_id"])

        return seen

    def prune(self, today: str) -> None:
        """Delete log files older than retention_days.

        Args:
            today: ISO date string (YYYY-MM-DD), INJECTED — the module never calls
                date.today() itself so pruning stays hermetically testable
                (brief Step 3: "do NOT call date.today() inside").
        """
        today_date = date.fromisoformat(today)
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
                    if not log_file.is_file() or log_file.suffix != ".jsonl":
                        continue
                    # Extract date from filename (e.g., "2026-07-03.jsonl")
                    try:
                        file_date = date.fromisoformat(log_file.stem)
                    except ValueError:
                        # Non-date filename under a channel dir is unexpected;
                        # log it rather than silently ignore, then skip.
                        logger.warning(
                            "skipping non-date ambient log file %s during prune",
                            log_file,
                        )
                        continue
                    if file_date < cutoff_date:
                        log_file.unlink()
