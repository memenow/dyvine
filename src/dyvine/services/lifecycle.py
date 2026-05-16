"""R2 storage lifecycle helper.

`LifecycleManager` reads retention and transition rules from
``core/storage_lifecycle.JSON`` and applies them to objects stored in
R2 by walking each content-type prefix and either deleting expired
objects or recording a transition entry (R2 itself does not yet
support storage-class transitions, so the entry only documents the
intended action).

Audit log writes go through the optional ``audit_executor`` that
``ServiceContainer`` provisions for this helper, with rotation that
keeps logs under ``logs/r2_lifecycle_audit.YYYYMMDD.log`` and prunes
files older than the configured retention window.

`LifecycleManager` is exercised by
``tests/services/test_lifecycle_service.py`` but is not yet wired into
``ServiceContainer.initialize``; runtime deployments do not invoke it.
Treat the class as a tested utility that can be scheduled (e.g. via a
Kubernetes ``CronJob`` or an explicit FastAPI background task) once
the operational decision to enforce retention is made.
"""

import asyncio
import functools
import json
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.logging import ContextLogger
from .storage import ContentType, R2StorageService

# Initialize logger
logger = ContextLogger(__name__)


class LifecycleError(Exception):
    """Base exception for lifecycle management operations."""

    pass


class LifecycleManager:
    """Apply R2 retention and transition rules from `storage_lifecycle.JSON`.

    Reads the JSON ruleset at construction time and exposes
    ``apply_lifecycle_rules`` which walks each content-type prefix in
    R2, dispatches deletions for objects past `retention_days`, and
    records a ``transition`` audit entry for objects that should change
    storage class (R2 itself does not yet support storage-class
    transitions, so the entry only documents the intended action).

    Attributes:
        storage: `R2StorageService` instance the manager will operate
            against. The instance must already be configured (a
            disabled storage service produces an empty result).
        rules: Loaded lifecycle rules keyed by content type.
        audit_config: Audit logging configuration extracted from the
            same JSON file.

    """

    def __init__(
        self,
        storage: R2StorageService,
        *,
        executor: Executor | None = None,
    ) -> None:
        """Initialize the lifecycle manager.

        Args:
            storage: Configured R2StorageService instance.
            executor: Optional dedicated ``concurrent.futures.Executor`` used
                to run the synchronous audit-log writer off the event loop.
                When ``None`` the manager falls back to the default asyncio
                executor, keeping unit tests that build the manager directly
                working without a container.
        """
        self.storage = storage
        self._executor: Executor | None = executor
        self.rules: dict[str, Any] = {}
        self.audit_config: dict[str, Any] = {}
        self._load_config()

        logger.info(
            "LifecycleManager initialized", extra={"rules_count": len(self.rules)}
        )

    def set_executor(self, executor: Executor | None) -> None:
        """Attach a dedicated executor after construction."""
        self._executor = executor

    def _load_config(self) -> None:
        """Load lifecycle configuration from JSON file."""
        try:
            config_path = (
                Path(__file__).parent.parent / "core" / "storage_lifecycle.json"
            )
            with open(config_path) as f:
                config = json.load(f)

            # Validate and store rules
            self.rules = {rule["content_type"]: rule for rule in config["rules"]}
            self.audit_config = config["audit"]

            logger.info(
                "Loaded lifecycle configuration",
                extra={"version": config["version"], "rules": list(self.rules.keys())},
            )

        except Exception as e:
            logger.exception(
                "Failed to load lifecycle configuration", extra={"error": str(e)}
            )
            raise LifecycleError(
                f"Failed to load lifecycle configuration: {str(e)}"
            ) from e

    async def apply_lifecycle_rules(self) -> dict[str, Any]:
        """Apply lifecycle rules to all content in storage.

        This method:
        1. Scans all content in storage
        2. Applies retention rules
        3. Handles storage class transitions
        4. Performs deletions
        5. Generates audit logs

        Returns:
            Dict[str, Any]: Summary of actions taken
        """
        summary: dict[str, Any] = {
            "transitioned": 0,
            "deleted": 0,
            "errors": 0,
            "details": [],
        }

        try:
            # Process each content type
            for content_type in ContentType:
                rule = self.rules.get(content_type.value)
                if not rule:
                    continue

                # List objects for this content type
                objects = await self.storage.list_objects(
                    prefix=content_type.value + "/"
                )

                for obj in objects:
                    try:
                        action = await self._apply_rule_to_object(obj, rule)
                        if action:
                            summary["details"].append(action)
                            if action["action"] == "transition":
                                summary["transitioned"] += 1
                            elif action["action"] == "delete":
                                summary["deleted"] += 1

                    except Exception as e:
                        logger.exception(
                            "Error applying rule to object",
                            extra={"object_key": obj.get("Key"), "error": str(e)},
                        )
                        summary["errors"] += 1

            # Generate audit log
            if self.audit_config["enabled"]:
                await self._write_audit_log(summary)

            return summary

        except Exception as e:
            logger.exception("Error applying lifecycle rules", extra={"error": str(e)})
            raise LifecycleError(f"Lifecycle rule application failed: {str(e)}") from e

    async def _apply_rule_to_object(
        self, obj: dict[str, Any], rule: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply a lifecycle rule to a specific object.

        Args:
            obj: Object information from storage
            rule: Lifecycle rule to apply

        Returns:
            Optional[Dict[str, Any]]: Action taken, if any
        """
        now = datetime.now(UTC)
        raw_ts = obj.get("LastModified", now)
        # ``boto3`` already attaches UTC tzinfo for S3-compatible APIs,
        # but a tz-aware payload that uses a non-UTC offset must be
        # converted (not stamped over). ``replace(tzinfo=UTC)`` would
        # silently corrupt the age calculation in that case.
        if raw_ts.tzinfo is None:
            last_modified = raw_ts.replace(tzinfo=UTC)
        else:
            last_modified = raw_ts.astimezone(UTC)
        age_days = (now - last_modified).days

        # Check for deletion
        if "retention_days" in rule:
            if age_days >= rule["retention_days"]:
                await self.storage.delete_object(obj.get("Key", ""))
                return {
                    "action": "delete",
                    "object_key": obj.get("Key", ""),
                    "reason": (
                        f"Age {age_days} days exceeded retention "
                        f"{rule['retention_days']} days"
                    ),
                }

        # Check for storage class transition
        if "transition" in rule:
            transition = rule["transition"]
            if (
                age_days >= transition["days"]
                and obj.get("StorageClass") != transition["storage_class"]
            ):
                # Note: Actual transition would be implemented here
                # R2 currently doesn't support storage class transitions
                return {
                    "action": "transition",
                    "object_key": obj.get("Key", ""),
                    "from_class": obj.get("StorageClass", "STANDARD"),
                    "to_class": transition["storage_class"],
                    "reason": (
                        f"Age {age_days} days exceeded transition threshold "
                        f"{transition['days']} days"
                    ),
                }

        return None

    async def _write_audit_log(self, summary: dict[str, Any]) -> None:
        """Write lifecycle actions to audit log.

        Args:
            summary: Summary of actions taken
        """
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._executor,
                functools.partial(self._write_audit_log_sync, summary),
            )
        except Exception as e:
            logger.exception("Failed to write audit log", extra={"error": str(e)})

    def _write_audit_log_sync(self, summary: dict[str, Any]) -> None:
        """Perform the blocking file write and log rotation off the loop.

        The audit log filename embeds the UTC date so each day starts a
        fresh file (``r2_lifecycle_audit.YYYYMMDD.log``). The active file
        therefore matches the rotation glob; without the date stamp the
        legacy ``r2_lifecycle_audit.log`` was never rotated and grew
        unbounded on long-running deployments.
        """
        now = datetime.now(UTC)
        log_path = Path("logs") / f"r2_lifecycle_audit.{now:%Y%m%d}.log"
        log_path.parent.mkdir(exist_ok=True)

        with open(log_path, "a") as f:
            for action in summary["details"]:
                log_entry = self.audit_config["log_format"].format(
                    timestamp=now.isoformat(),
                    user="system",
                    action=action["action"],
                    object_key=action["object_key"],
                    metadata_size="0B",
                    status="success",
                )
                f.write(log_entry + "\n")

        # Rotate old logs within the same worker thread.
        self._rotate_audit_logs()

    def _rotate_audit_logs(self) -> None:
        """Delete dated audit logs older than the configured retention."""
        try:
            retention_days = self.audit_config["log_retention_days"]
            log_dir = Path("logs")
            if not log_dir.exists():
                return

            cutoff = datetime.now(UTC) - timedelta(days=retention_days)

            for log_file in log_dir.glob("r2_lifecycle_audit.*.log"):
                try:
                    timestamp_str = log_file.stem.split(".")[-1]
                    timestamp = datetime.strptime(timestamp_str, "%Y%m%d").replace(
                        tzinfo=UTC
                    )

                    if timestamp < cutoff:
                        log_file.unlink()
                        logger.info(
                            "Rotated audit log",
                            extra={"file": str(log_file)},
                        )

                except (ValueError, OSError) as e:
                    logger.error(
                        "Error rotating log file",
                        extra={"file": str(log_file), "error": str(e)},
                    )

            # Migrate the legacy un-dated ``r2_lifecycle_audit.log`` if
            # present so existing deployments do not strand a file the
            # rotation glob no longer matches.
            legacy = log_dir / "r2_lifecycle_audit.log"
            if legacy.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%d")
                target = log_dir / f"r2_lifecycle_audit.{stamp}.legacy.log"
                try:
                    legacy.rename(target)
                    logger.info(
                        "Migrated legacy audit log",
                        extra={"src": str(legacy), "dst": str(target)},
                    )
                except OSError as exc:
                    logger.warning(
                        "Failed to migrate legacy audit log",
                        extra={"file": str(legacy), "error": str(exc)},
                    )

        except Exception as e:
            logger.exception("Failed to rotate audit logs", extra={"error": str(e)})
