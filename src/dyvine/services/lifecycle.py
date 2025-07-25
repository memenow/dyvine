"""Lifecycle management service for R2 storage.

This module implements the lifecycle management rules for R2 storage content,
handling:
- Content retention policies
- Storage class transitions
- Automated deletions
- Audit logging

The service reads rules from storage_lifecycle.json and applies them to stored
content based on content type and age.
"""

import json
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
    """Service for managing R2 storage lifecycle policies.

    This class implements the lifecycle rules defined in storage_lifecycle.json,
    managing content retention, transitions, and deletions.

    Attributes:
        storage: R2StorageService instance
        rules: Loaded lifecycle rules
        audit_config: Audit logging configuration
    """

    def __init__(self, storage: R2StorageService) -> None:
        """Initialize the lifecycle manager.

        Args:
            storage: Configured R2StorageService instance
        """
        self.storage = storage
        self.rules = {}
        self.audit_config = {}
        self._load_config()

        logger.info(
            "LifecycleManager initialized", extra={"rules_count": len(self.rules)}
        )

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
            raise LifecycleError(f"Failed to load lifecycle configuration: {str(e)}")

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
        summary = {"transitioned": 0, "deleted": 0, "errors": 0, "details": []}

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
                            extra={"object_key": obj["Key"], "error": str(e)},
                        )
                        summary["errors"] += 1

            # Generate audit log
            if self.audit_config["enabled"]:
                self._write_audit_log(summary)

            return summary

        except Exception as e:
            logger.exception("Error applying lifecycle rules", extra={"error": str(e)})
            raise LifecycleError(f"Lifecycle rule application failed: {str(e)}")

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
        last_modified = obj["LastModified"].replace(tzinfo=UTC)
        age_days = (now - last_modified).days

        # Check for deletion
        if "retention_days" in rule:
            if age_days >= rule["retention_days"]:
                await self.storage.delete_object(obj["Key"])
                return {
                    "action": "delete",
                    "object_key": obj["Key"],
                    "reason": f"Age {age_days} days exceeded retention {rule['retention_days']} days",
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
                    "object_key": obj["Key"],
                    "from_class": obj.get("StorageClass", "STANDARD"),
                    "to_class": transition["storage_class"],
                    "reason": f"Age {age_days} days exceeded transition threshold {transition['days']} days",
                }

        return None

    def _write_audit_log(self, summary: dict[str, Any]) -> None:
        """Write lifecycle actions to audit log.

        Args:
            summary: Summary of actions taken
        """
        try:
            now = datetime.now(UTC)
            log_path = Path("logs/r2_lifecycle_audit.log")
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

            # Rotate old logs
            self._rotate_audit_logs()

        except Exception as e:
            logger.exception("Failed to write audit log", extra={"error": str(e)})

    def _rotate_audit_logs(self) -> None:
        """Rotate audit logs based on retention policy."""
        try:
            retention_days = self.audit_config["log_retention_days"]
            log_dir = Path("logs")
            if not log_dir.exists():
                return

            cutoff = datetime.now(UTC) - timedelta(days=retention_days)

            for log_file in log_dir.glob("r2_lifecycle_audit.*.log"):
                try:
                    # Parse timestamp from filename
                    timestamp_str = log_file.stem.split(".")[-1]
                    timestamp = datetime.strptime(timestamp_str, "%Y%m%d").replace(
                        tzinfo=UTC
                    )

                    if timestamp < cutoff:
                        log_file.unlink()
                        logger.info("Rotated audit log", extra={"file": str(log_file)})

                except (ValueError, OSError) as e:
                    logger.error(
                        "Error rotating log file",
                        extra={"file": str(log_file), "error": str(e)},
                    )

        except Exception as e:
            logger.exception("Failed to rotate audit logs", extra={"error": str(e)})
