"""AWS Lambda entry points, one per Step Functions task.

Environment:
  CASE_BUCKET        S3 bucket for documents, case snapshots and events
  INSURER_URL        base URL of the insurer simulator's function URL
  EXISTING_MEMBERS_KEY  optional S3 key of a JSON file: {company_id: {employee_id: {"plan_code": ...}}}
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from functools import lru_cache

from ..insurer_client import HttpInsurer
from ..store import S3Store
from . import steps


@lru_cache(maxsize=1)
def _runtime() -> steps.Runtime:
    store = S3Store(os.environ["CASE_BUCKET"])
    key = os.environ.get("EXISTING_MEMBERS_KEY")
    existing_all = json.loads(store.read_document(key)) if key else {}
    return steps.Runtime(
        store=store,
        insurer=HttpInsurer(os.environ["INSURER_URL"]),
        clock=lambda: datetime.now(timezone.utc),
        existing_members=lambda company_id: existing_all.get(company_id, {}),
        sleep=time.sleep,
    )


def intake(event, context):
    return steps.intake(event, _runtime())


def validate(event, context):
    return steps.validate_case(event, _runtime())


def submit(event, context):
    return steps.submit(event, _runtime())


def check_ack(event, context):
    return steps.check_ack(event, _runtime())


def mark_active(event, context):
    return steps.mark_active(event, _runtime())


def mark_for_review(event, context):
    return steps.mark_for_review(event, _runtime())
