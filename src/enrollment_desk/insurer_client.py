"""Clients the workflow uses to talk to the insurer: in-process (tests, local runs) or HTTP (deployed)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Protocol

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
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"content-type": "application/json"})
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
