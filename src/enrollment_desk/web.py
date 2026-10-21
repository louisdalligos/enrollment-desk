"""Loopback-only web interface for the existing local enrollment workflow."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .cli import START
from .insurer_client import InProcessInsurer
from .insurer_sim.core import InsurerSimulator
from .reference import COMPANIES
from .store import LocalStore
from .workflow import local_runner, steps

ROOT = Path(__file__).resolve().parents[2]
MAX_UPLOAD = 5 * 1024 * 1024


class WebApp:
    def __init__(self, store='.local/store', existing='data/generated/existing_members.json'):
        self.store = LocalStore(store)
        self.existing = Path(existing)

    def detail(self, case_id):
        if not re.fullmatch(r'end-\d{8}-[a-f0-9]{6}', case_id):
            raise ValueError('Invalid case reference')
        case = self.store.get_case(case_id)
        return {'case': case.model_dump(mode='json'),
                'events': [e.model_dump(mode='json') for e in self.store.events(case_id)],
                'readiness': steps.readiness(case)}

    def submit(self, filename, company, data):
        if company not in COMPANIES:
            raise ValueError('Choose a valid company')
        # Never use a user-supplied path for filesystem writes.
        filename = filename.replace('\\', '/').split('/')[-1]
        if Path(filename).suffix.lower() not in ('.csv', '.xlsx'):
            raise ValueError('Upload a CSV or XLSX roster')
        if not data or len(data) > MAX_UPLOAD:
            raise ValueError('Choose a nonempty file under 5 MB')
        uploads = self.store.root / 'uploads' / uuid4().hex
        uploads.mkdir(parents=True)
        document = uploads / filename
        document.write_bytes(data)
        clock = local_runner.FakeClock(START)
        existing = json.loads(self.existing.read_text()) if self.existing.exists() else {}
        rt = steps.Runtime(self.store, InProcessInsurer(InsurerSimulator(), clock), clock,
                           lambda cid: existing.get(cid, {}), clock.advance)
        result = local_runner.run(str(document), company, rt, clock)
        return self.detail(result['final']['case_id'])


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, payload, content_type='application/json'):
            data = json.dumps(payload, default=str).encode() if content_type == 'application/json' else payload
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == '/':
                    return self.respond(200, Path(__file__).with_name('web.html').read_bytes(), 'text/html; charset=utf-8')
                if path == '/api/cases':
                    return self.respond(200, {'cases': [c.model_dump(mode='json') for c in app.store.list_cases()],
                        'companies': [asdict(c) for c in COMPANIES.values()], 'demo_date': START.date().isoformat()})
                if path.startswith('/api/cases/'):
                    return self.respond(200, app.detail(path.rsplit('/', 1)[-1]))
                if path in ('/samples/clean_roster.csv', '/samples/messy_roster.csv'):
                    return self.respond(200, (ROOT / 'data' / path.lstrip('/')).read_bytes(), 'text/csv')
                self.respond(404, {'error': 'Not found'})
            except FileNotFoundError:
                self.respond(404, {'error': 'Case or sample not found'})
            except ValueError as exc:
                self.respond(400, {'error': str(exc)})

        def do_POST(self):
            # Require a custom header and same-origin browser requests.
            origin = self.headers.get('Origin')
            if self.headers.get('X-Enrollment-Desk') != 'local' or (origin and origin != 'http://' + self.headers.get('Host', '')):
                return self.respond(403, {'error': 'Use the local enrollment page to upload'})
            if urlsplit(self.path).path != '/api/cases':
                return self.respond(404, {'error': 'Not found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_UPLOAD:
                    return self.respond(413, {'error': 'Choose a nonempty file under 5 MB'})
                query = parse_qs(urlsplit(self.path).query)
                result = app.submit(query.get('filename', [''])[0], query.get('company', [''])[0], self.rfile.read(length))
                self.respond(201, result)
            except (ValueError, KeyError):
                self.respond(400, {'error': 'Could not process roster. Check the file format and company.'})
            except Exception:
                self.log_error('Roster processing failed')
                self.respond(500, {'error': 'Roster processing failed. Check the file and try again.'})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--store', default='.local/store')
    args = parser.parse_args()
    server = HTTPServer(('127.0.0.1', args.port), make_handler(WebApp(args.store)))
    print(f'Enrollment Desk: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
