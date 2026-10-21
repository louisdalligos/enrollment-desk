"""Case snapshots and the append-only event log.

LocalStore writes to a folder for development and tests. S3Store writes the same
layout to a bucket for the deployed workflow (Aurora replaces case snapshots in phase 3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .models import Case, Event


class Store(Protocol):
    def put_case(self, case: Case) -> None: ...
    def get_case(self, case_id: str) -> Case: ...
    def list_cases(self) -> list[Case]: ...
    def append_event(self, event: Event) -> None: ...
    def events(self, case_id: str) -> list[Event]: ...
    def read_document(self, key: str) -> bytes: ...


class LocalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "cases").mkdir(parents=True, exist_ok=True)
        (self.root / "events").mkdir(parents=True, exist_ok=True)

    def put_case(self, case: Case) -> None:
        (self.root / "cases" / f"{case.case_id}.json").write_text(case.model_dump_json(indent=2))

    def get_case(self, case_id: str) -> Case:
        return Case.model_validate_json((self.root / "cases" / f"{case_id}.json").read_text())

    def list_cases(self) -> list[Case]:
        return [Case.model_validate_json(p.read_text()) for p in sorted((self.root / "cases").glob("*.json"))]

    def append_event(self, event: Event) -> None:
        with (self.root / "events" / f"{event.case_id}.jsonl").open("a") as f:
            f.write(event.model_dump_json() + "\n")

    def events(self, case_id: str) -> list[Event]:
        p = self.root / "events" / f"{case_id}.jsonl"
        if not p.exists():
            return []
        return [Event.model_validate_json(line) for line in p.read_text().splitlines() if line.strip()]

    def read_document(self, key: str) -> bytes:
        return Path(key).read_bytes()


class S3Store:
    """Same layout in S3. Events are one object per event so concurrent writers never overwrite each other."""

    def __init__(self, bucket: str, prefix: str = ""):
        import boto3  # available in the Lambda runtime

        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix else ""

    def _key(self, *parts: str) -> str:
        return self.prefix + "/".join(parts)

    def put_case(self, case: Case) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key("cases", f"{case.case_id}.json"),
                           Body=case.model_dump_json(indent=2).encode(), ContentType="application/json")

    def get_case(self, case_id: str) -> Case:
        obj = self.s3.get_object(Bucket=self.bucket, Key=self._key("cases", f"{case_id}.json"))
        return Case.model_validate_json(obj["Body"].read())

    def list_cases(self) -> list[Case]:
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key("cases", "")):
            for item in page.get("Contents", []):
                out.append(Case.model_validate_json(self.s3.get_object(Bucket=self.bucket, Key=item["Key"])["Body"].read()))
        return out

    def append_event(self, event: Event) -> None:
        stamp = event.at.strftime("%Y%m%dT%H%M%S%f")
        key = self._key("events", event.case_id, f"{stamp}-{event.type}.json")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=event.model_dump_json().encode(),
                           ContentType="application/json")

    def events(self, case_id: str) -> list[Event]:
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key("events", case_id, "")):
            for item in page.get("Contents", []):
                out.append(Event.model_validate_json(self.s3.get_object(Bucket=self.bucket, Key=item["Key"])["Body"].read()))
        return sorted(out, key=lambda e: e.at)

    def read_document(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()


def dump_events(events: list[Event]) -> str:
    return "\n".join(json.dumps({"at": e.at.isoformat(), "type": e.type, **e.data}, default=str) for e in events)
