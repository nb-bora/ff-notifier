from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from config import settings
from logger import logger


class DedupStore:
    async def seen_or_mark(self, *, event_id: str) -> bool:
        """
        Returns True if already seen (duplicate).
        Returns False if marked as seen for first time.
        """


@dataclass
class MemoryDedupStore(DedupStore):
    ttl_seconds: int

    def __post_init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def seen_or_mark(self, *, event_id: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            exp = self._seen.get(event_id)
            if exp is not None and exp > now:
                return True
            # Prune opportunistically (small, bounded)
            if len(self._seen) > 10_000:
                cutoff = now
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            self._seen[event_id] = now + max(1, int(self.ttl_seconds))
            return False


class DynamoDbDedupStore(DedupStore):
    def __init__(self, *, table_name: str):
        self._table_name = table_name
        if settings.aws_profile:
            session = boto3.Session(profile_name=settings.aws_profile)
            self._ddb = session.client("dynamodb", region_name=settings.aws_region)
        else:
            self._ddb = boto3.client("dynamodb", region_name=settings.aws_region)

    async def seen_or_mark(self, *, event_id: str) -> bool:
        ttl = max(60, int(getattr(settings, "notifications_dedup_ttl_seconds", 86400)))
        expires_at = int(time.time()) + ttl

        def _put() -> bool:
            try:
                self._ddb.put_item(
                    TableName=self._table_name,
                    Item={
                        "event_id": {"S": str(event_id)},
                        "expires_at": {"N": str(expires_at)},
                    },
                    ConditionExpression="attribute_not_exists(event_id)",
                )
                return False
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    return True
                logger.error("DynamoDB dedup put_item failed: %s", str(e), exc_info=True)
                # Fail-open: if DDB is down, do not block sending
                return False

        return await asyncio.to_thread(_put)


def build_dedup_store() -> DedupStore:
    mode = (getattr(settings, "notifications_dedup_mode", "memory") or "memory").lower()
    if mode == "dynamodb":
        table = getattr(settings, "notifications_dedup_dynamodb_table", "") or ""
        if not table:
            logger.warning(
                "NOTIFICATIONS_DEDUP_MODE=dynamodb but NOTIFICATIONS_DEDUP_DYNAMODB_TABLE empty; falling back to memory"
            )
        else:
            return DynamoDbDedupStore(table_name=table)
    return MemoryDedupStore(
        ttl_seconds=int(getattr(settings, "notifications_dedup_ttl_seconds", 86400))
    )

