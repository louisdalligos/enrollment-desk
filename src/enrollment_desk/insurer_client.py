"""Clients the workflow uses to talk to the insurer: in-process (tests, local runs) or HTTP (deployed)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .insurer_sim.core import InsurerSimulator, InsurerUnavailable


class InsurerClient(Protocol):
    def submit(self, payload: dict, attempt: int) -> dict: ...
    def status(self, submission_id: str) -> dict: ...


class InProcessInsurer:
    def __init__(self, sim: InsurerSimulator, clock: Callable[[], datetime]):
        self.sim = sim
        self.clock = clock

    def submit(self, payload: dict, attempt: int) -> dict:
        return self.sim.submit(payload, self.clock(), attempt)

    def status(self, submission_id: str) -> dict:
        return self.sim.status(submission_id, self.clock())


class HttpInsurer:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body, default=str).encode() if body is not None else None
        url = self.base_url + path
        headers = {"content-type": "application/json"}
        # AWS Lambda Function URLs use IAM authentication. Keep local HTTP
        # simulator calls unsigned for developer workflows and unit tests.
        endpoint = urlsplit(url)
        function_url = re.fullmatch(
            r"[a-z0-9-]+\.lambda-url\.([a-z0-9-]+)\.on\.aws", endpoint.hostname or ""
        )
        if function_url:
            if endpoint.scheme != "https":
                raise ValueError("Lambda Function URLs require HTTPS")
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.session import get_session

            session = get_session()
            credentials = session.get_credentials()
            if credentials is None:
                raise RuntimeError("AWS credentials required for IAM-protected simulator")
            region = function_url.group(1)
            from hashlib import sha256

            headers["x-amz-content-sha256"] = sha256(data or b"").hexdigest()
            signed = AWSRequest(method=method, url=url, data=data, headers=headers)
            SigV4Auth(credentials.get_frozen_credentials(), "lambda", region).add_auth(signed)
            headers = dict(signed.headers.items())
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (502, 503, 504):
                raise InsurerUnavailable(f"HTTP {exc.code}") from exc
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InsurerUnavailable(str(exc)) from exc

    def submit(self, payload: dict, attempt: int) -> dict:
        return self._call("POST", f"/submissions?attempt={attempt}", payload)

    def status(self, submission_id: str) -> dict:
        return self._call("GET", f"/submissions/{submission_id}")
