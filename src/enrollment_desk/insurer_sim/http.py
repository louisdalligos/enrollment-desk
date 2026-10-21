"""HTTP faces of the insurer simulator: a Lambda function URL handler and a local server.

Routes:
  POST /submissions?attempt=N   body: {"company_id": ..., "members": [...]}
  GET  /submissions/{id}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import InsurerSimulator, InsurerUnavailable, SimulatorConfig, UnknownSubmission


def _config_from_env() -> SimulatorConfig:
    return SimulatorConfig(
        seed=int(os.environ.get("SIM_SEED", 7)),
        ack_delay_seconds=int(os.environ.get("SIM_ACK_DELAY_SECONDS", 90)),
        reject_rate=float(os.environ.get("SIM_REJECT_RATE", 0.08)),
    )


def route(sim: InsurerSimulator, method: str, path: str, query: dict, body: bytes, now: datetime) -> tuple[int, dict]:
    parts = [p for p in path.split("/") if p]
    try:
        if method == "POST" and parts == ["submissions"]:
            attempt = int((query.get("attempt") or ["1"])[0])
            return 202, sim.submit(json.loads(body or b"{}"), now, attempt)
        if method == "GET" and len(parts) == 2 and parts[0] == "submissions":
            return 200, sim.status(parts[1], now)
        return 404, {"error": "not found"}
    except InsurerUnavailable as exc:
        return 503, {"error": str(exc)}
    except UnknownSubmission:
        return 404, {"error": "unknown submission"}
    except (ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}


def lambda_handler(event, context):  # Lambda function URL (payload format 2.0)
    import base64

    sim = InsurerSimulator(_config_from_env())
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    status, payload = route(
        sim,
        event["requestContext"]["http"]["method"],
        event.get("rawPath", "/"),
        parse_qs(event.get("rawQueryString", "")),
        raw,
        datetime.now(timezone.utc),
    )
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(payload)}


def serve(port: int = 8081) -> None:
    sim = InsurerSimulator(_config_from_env())

    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method: str):
            url = urlparse(self.path)
            length = int(self.headers.get("content-length") or 0)
            status, payload = route(sim, method, url.path, parse_qs(url.query), self.rfile.read(length),
                                    datetime.now(timezone.utc))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    print(f"insurer simulator on http://localhost:{port}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", 8081)))
