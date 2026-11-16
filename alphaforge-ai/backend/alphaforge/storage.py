"""Tiny key-value store: local JSON files for development, DynamoDB on AWS.

Logical tables (DynamoDB name = ALPHAFORGE_TABLE_PREFIX + table, partition key "pk" (string),
TTL attribute "expires_at" on the short-lived tables):
    profiles        one item per user
    sources         monitoring source settings (frequency, enabled)
    agent_traces    stored answers with evidence and trace ("how did it get here")
    agent_sessions  short-lived context keys that bind agent tool calls to a user (TTL)
    ask_jobs        asynchronous agent questions (TTL)
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from .config import get_settings

TABLES = ("profiles", "sources", "agent_traces", "agent_sessions", "ask_jobs")


class LocalJsonStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, table: str) -> Path:
        if table not in TABLES:
            raise KeyError(table)
        return self.root / f"{table}.json"

    def _read(self, table: str) -> dict:
        p = self._path(table)
        return json.loads(p.read_text()) if p.exists() else {}

    def _write(self, table: str, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path(table).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self._path(table))

    def get(self, table: str, key: str) -> dict | None:
        with self._lock:
            item = self._read(table).get(key)
        if item and item.get("_expires_at") and item["_expires_at"] < time.time():
            return None
        return item

    def put(self, table: str, key: str, item: dict, ttl_seconds: int | None = None) -> None:
        with self._lock:
            data = self._read(table)
            data[key] = {**item, **({"_expires_at": int(time.time()) + ttl_seconds} if ttl_seconds else {})}
            self._write(table, data)

    def append(self, table: str, key: str, field: str, value) -> None:
        with self._lock:
            data = self._read(table)
            item = data.setdefault(key, {})
            item.setdefault(field, []).append(value)
            self._write(table, data)

    def list(self, table: str) -> list[dict]:
        with self._lock:
            return list(self._read(table).values())


class DynamoStore:
    """Items are stored as {"pk": key, "doc": <item>} to keep the schema stable."""

    def __init__(self, prefix: str):
        import boto3

        self._ddb = boto3.resource("dynamodb")
        self._prefix = prefix

    def _table(self, table: str):
        if table not in TABLES:
            raise KeyError(table)
        return self._ddb.Table(self._prefix + table)

    @staticmethod
    def _to_ddb(item):
        return json.loads(json.dumps(item, default=str), parse_float=Decimal)

    @staticmethod
    def _from_ddb(item):
        if isinstance(item, Decimal):
            return int(item) if item == item.to_integral_value() else float(item)
        if isinstance(item, list):
            return [DynamoStore._from_ddb(x) for x in item]
        if isinstance(item, dict):
            return {k: DynamoStore._from_ddb(v) for k, v in item.items()}
        return item

    def get(self, table: str, key: str) -> dict | None:
        res = self._table(table).get_item(Key={"pk": key}, ConsistentRead=True)
        item = res.get("Item")
        if not item or (item.get("expires_at") and int(item["expires_at"]) < time.time()):
            return None  # DynamoDB TTL deletes lazily, so check expiry ourselves
        return self._from_ddb(item.get("doc", {}))

    def put(self, table: str, key: str, item: dict, ttl_seconds: int | None = None) -> None:
        record = {"pk": key, "doc": self._to_ddb(item)}
        if ttl_seconds:
            record["expires_at"] = int(time.time()) + ttl_seconds
        self._table(table).put_item(Item=record)

    def append(self, table: str, key: str, field: str, value) -> None:
        """Atomic list append inside doc.<field> (safe for concurrent tool calls)."""
        self._table(table).update_item(
            Key={"pk": key},
            UpdateExpression="SET #d.#f = list_append(if_not_exists(#d.#f, :empty), :v)",
            ExpressionAttributeNames={"#d": "doc", "#f": field},
            ExpressionAttributeValues={":v": [self._to_ddb(value)], ":empty": []},
        )

    def list(self, table: str) -> list[dict]:
        t = self._table(table)
        items, kwargs = [], {}
        while True:
            res = t.scan(**kwargs)
            items += [self._from_ddb(i["doc"]) for i in res.get("Items", [])]
            if "LastEvaluatedKey" not in res:
                return items
            kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


@lru_cache(maxsize=1)
def get_store():
    s = get_settings()
    if s.storage == "dynamodb":
        return DynamoStore(s.table_prefix)
    return LocalJsonStore(s.local_store_dir)
