"""In-memory fake PocketBase for tests.

Implements the subset of the PB record API the repositories use, including
unique-constraint enforcement (job_leases atomic claim) and a pragmatic filter
language (`=`/`!=`/`<`/`<=`, strings/numbers/bools, `&&`, `||`, parens).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

from pocketbase.errors import ClientResponseError
from pocketbase.models.file_upload import FileUpload


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


class FakeListResult:
    def __init__(self, items: list[dict], total: int) -> None:
        self.items = items
        self.total_items = total


class FakeRecordService:
    def __init__(self, storage: FakeStorage, name: str) -> None:
        self._storage = storage
        self.name = name

    # -- filtering -----------------------------------------------------------------
    def _match(self, record: dict, filter: str) -> bool:
        if not filter or not filter.strip():
            return True
        tokens = self._tokenize(filter)
        return self._parse_or(tokens, record)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        # field="value" (no spaces), quoted strings, operators, parens, && ||
        return re.findall(r'\(|\)|&&|\|\||!=|<=|>=|=|<|>|"[^"]*"|[^\s()&|=<>!]+', text)

    def _parse_or(self, tokens: list[str], record: dict) -> bool:
        left = self._parse_and(tokens, record)
        while tokens and tokens[0] == "||":
            tokens.pop(0)
            right = self._parse_and(tokens, record)
            left = left or right
        return left

    def _parse_and(self, tokens: list[str], record: dict) -> bool:
        left = self._parse_atom(tokens, record)
        while tokens and tokens[0] == "&&":
            tokens.pop(0)
            right = self._parse_atom(tokens, record)
            left = left and right
        return left

    def _parse_atom(self, tokens: list[str], record: dict) -> bool:
        token = tokens.pop(0) if tokens else ""
        if token == "(":
            value = self._parse_or(tokens, record)
            if tokens and tokens[0] == ")":
                tokens.pop(0)
            return value
        # field OP value
        op_token = tokens.pop(0) if tokens else ""
        value_token = tokens.pop(0) if tokens else ""
        return self._eval_atom(record, token, op_token, value_token)

    def _eval_atom(self, record: dict, field: str, op: str, raw_value: str) -> bool:
        actual = self._resolve_field(record, field)
        if raw_value.startswith('"') and raw_value.endswith('"'):
            expected: Any = raw_value[1:-1]
        elif raw_value == "true":
            expected = True
        elif raw_value == "false":
            expected = False
        elif raw_value == "null" or raw_value == "None":
            expected = None
        else:
            expected = float(raw_value) if "." in raw_value else int(raw_value)

        if op == "=":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op in ("<", "<="):
            if actual is None:
                return False
            return (actual < expected) if op == "<" else (actual <= expected)
        if op in (">", ">="):
            if actual is None:
                return False
            return (actual > expected) if op == ">" else (actual >= expected)
        if op == "~":
            # PocketBase `~` is a REGEX match (".*" matches everything)
            if actual is None:
                return False
            try:
                return re.search(expected, str(actual)) is not None
            except re.error:
                return expected in str(actual)
        raise AssertionError(f"unsupported filter op {op!r}")

    def _resolve_field(self, record: dict[str, Any], field: str) -> Any:
        """Resolve `relation.field` dotted paths (PocketBase relation traversal).

        A dotted path means the first segment is a relation id on this record;
        the referenced record is looked up across collections so filters behave
        like real PB (which rejects filters on non-existent fields with a 400).
        """
        parts = field.split(".")
        current: Any = record
        for part in parts:
            while isinstance(current, str):
                # Relation id → resolve to the referenced record first.
                found = None
                for name in self._storage.collection_names():
                    found = self._storage.get(name, current)
                    if found is not None:
                        break
                if found is None:
                    return None
                current = found
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    # -- sort ----------------------------------------------------------------------
    @staticmethod
    def _sorted(records: list[dict], sort: str) -> list[dict]:
        if not sort:
            return list(records)
        keys = [k.strip() for k in sort.split(",")]
        for key in reversed(keys):
            desc = key.startswith("-")
            field = key.lstrip("-")
            records = sorted(records, key=lambda r: str(r.get(field, "") or ""), reverse=desc)
        return records

    # -- crud ----------------------------------------------------------------------
    def get_list(self, page: int, per_page: int, query_params: dict[str, Any] | None = None):
        params = query_params or {}
        records = self._storage.records(self.name)
        records = [r for r in records if self._match(r, params.get("filter", ""))]
        records = self._sorted(records, params.get("sort", ""))
        total = len(records)
        start = (max(1, page) - 1) * per_page
        return FakeListResult(records[start : start + per_page], total)

    def get_full_list(self, query_params: dict[str, Any] | None = None):
        params = query_params or {}
        records = [
            r for r in self._storage.records(self.name) if self._match(r, params.get("filter", ""))
        ]
        return self._sorted(records, params.get("sort", ""))

    def get_one(self, record_id: str):
        record = self._storage.get(self.name, record_id)
        if record is None:
            raise ClientResponseError("not found", status=404)
        return record

    def get_first_list_item(self, filter: str, query_params: dict[str, Any] | None = None):
        params = dict(query_params or {})
        params["filter"] = filter
        result = self.get_list(1, 1, params)
        if not result.items:
            raise ClientResponseError("not found", status=404)
        return result.items[0]

    def create(
        self, body_params: dict[str, Any] | None = None, query_params: dict[str, Any] | None = None
    ):
        data = dict(body_params or {})
        record = self._storage.create(self.name, data)
        return record

    def update(
        self,
        record_id: str,
        body_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
    ):
        return self._storage.update(self.name, record_id, body_params or {})

    def delete(self, record_id: str, query_params: dict[str, Any] | None = None):
        self._storage.delete(self.name, record_id)


class FakeStorage:
    """Shared record storage with unique constraints per (collection, field)."""

    def __init__(self, unique_fields: dict[str, list[str]] | None = None) -> None:
        self._records: dict[str, dict[str, dict]] = {}
        self.unique_fields: dict[str, list[str]] = unique_fields or {}
        self.files: dict[tuple[str, str, str], bytes] = {}  # (collection, id, field) → bytes
        self.query_count: int = 0  # reads (list/get/first)
        self.write_count: int = 0  # creates/updates/deletes

    @staticmethod
    def _split_files(data: dict) -> tuple[dict, list[tuple[str, object]]]:
        """Separate FileUpload values from plain fields (mirrors PB multipart)."""
        plain: dict = {}
        uploads: list[tuple[str, object]] = []
        for key, value in data.items():
            if isinstance(value, FileUpload):
                for item in value.files:
                    uploads.append((key, item))
            else:
                plain[key] = value
        return plain, uploads

    @staticmethod
    def _upload_parts(item: object) -> tuple[str, bytes]:
        # FileUpload entries are (filename, bytes|fileobj) tuples
        filename, content = item  # type: ignore[misc]
        if hasattr(content, "read"):
            content = content.read()
        return str(filename), bytes(content)

    def _bump_read(self) -> None:
        self.query_count += 1

    def _bump_write(self) -> None:
        self.write_count += 1

    def records(self, name: str) -> list[dict]:
        self._bump_read()
        return list(self._records.setdefault(name, {}).values())

    def collection_names(self) -> list[str]:
        return list(self._records.keys())

    def get(self, name: str, record_id: str) -> dict | None:
        self._bump_read()
        return self._records.get(name, {}).get(record_id)

    def create(self, name: str, data: dict) -> dict:
        self._bump_write()
        plain, uploads = self._split_files(data)
        for field in self.unique_fields.get(name, []):
            value = plain.get(field)
            if value is None or value == "":
                continue
            for other in self._records.get(name, {}).values():
                if other.get(field) == value and other.get(field) != "":
                    raise ClientResponseError("unique constraint violated", status=400)
        record = {"id": uuid.uuid4().hex, "created": _now(), "updated": _now(), **plain}
        self._records.setdefault(name, {})[record["id"]] = record
        for key, item in uploads:
            filename, content = self._upload_parts(item)
            record[key] = filename
            self.files[(name, record["id"], key)] = content
        return dict(record)

    def update(self, name: str, record_id: str, data: dict) -> dict:
        self._bump_write()
        record = self._records.get(name, {}).get(record_id)
        if record is None:
            raise ClientResponseError("not found", status=404)
        plain, uploads = self._split_files(data)
        for field in self.unique_fields.get(name, []):
            if field in plain and plain[field]:
                for other in self._records[name].values():
                    if other["id"] != record_id and other.get(field) == plain[field]:
                        raise ClientResponseError("unique constraint violated", status=400)
        record.update(plain)
        record["updated"] = _now()
        for key, item in uploads:
            filename, content = self._upload_parts(item)
            record[key] = filename
            self.files[(name, record_id, key)] = content
        return dict(record)

    def file_bytes(self, name: str, record_id: str, field: str) -> bytes | None:
        return self.files.get((name, record_id, field))

    def delete(self, name: str, record_id: str) -> None:
        self._bump_write()
        if record_id in self._records.get(name, {}):
            del self._records[name][record_id]

    def seed(self, name: str, data: dict) -> dict:
        return self.create(name, data)


class FakePocketBase:
    """Duck-typed PocketBase replacement: `.collection(name)` → FakeRecordService."""

    def __init__(self, unique_fields: dict[str, list[str]] | None = None) -> None:
        self.storage = FakeStorage(unique_fields)
        self.base_url = "http://fake.local"

    def collection(self, name: str) -> FakeRecordService:
        return FakeRecordService(self.storage, name)


def default_unique_fields() -> dict[str, list[str]]:
    return {
        "job_leases": ["job"],
        "jobs": ["idempotencyKey"],
        "documents": ["sourceId"],
        "projects": ["slug"],
    }
