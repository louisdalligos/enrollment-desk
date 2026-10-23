"""Loopback-only web interface for the existing local enrollment workflow."""
from __future__ import annotations

import hashlib
import argparse
import json
import re
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .column_mapping import preview, approval_record, BedrockColumnMapper, MappingUnavailable
from .roster_validation import ApiError, validate_container, validate_roster
from .cli import START
from .insurer_client import InProcessInsurer
from .insurer_sim.core import InsurerSimulator
from .reference import COMPANIES
from .store import LocalStore
from .workflow import local_runner, steps

ROOT = Path(__file__).resolve().parents[2]
MAX_UPLOAD = 5 * 1024 * 1024


class WebApp:
    def __init__(self, store='.local/store', existing='data/generated/existing_members.json', mapper=None):
        self.store = LocalStore(store)
        self.existing = Path(existing)
        self.mapper = mapper

    def detail(self, case_id):
        if not re.fullmatch(r'end-\d{8}-[a-f0-9]{6}', case_id):
            raise ValueError('Invalid case reference')
        case = self.store.get_case(case_id)
        return {'case': case.model_dump(mode='json'),
                'events': [e.model_dump(mode='json') for e in self.store.events(case_id)],
                'readiness': steps.readiness(case)}

    def submit(self, filename, company, data, review=False):
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
        validate_container(filename, data)
        if review:
            selected = preview(filename, data)
            job_id = uploads.name
            meta = {'document': str(document), 'company': company}
            (uploads / 'job.json').write_text(json.dumps(meta))
            return dict(selected, job_id=job_id, bedrock_enabled=self.mapper is not None)
        validate_roster(filename, data)
        return self.process(document, company)

    def process(self, document, company, mapping_key=None):
        clock = local_runner.FakeClock(START)
        existing = json.loads(self.existing.read_text()) if self.existing.exists() else {}
        rt = steps.Runtime(self.store, InProcessInsurer(InsurerSimulator(), clock), clock,
                           lambda cid: existing.get(cid, {}), clock.advance)
        result = local_runner.run(str(document), company, rt, clock, mapping_key=mapping_key)
        return self.detail(result['final']['case_id'])

    def mapping(self, action, body):
        job_id = body.get('job_id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise ValueError('Invalid mapping job')
        folder = self.store.root / 'uploads' / job_id
        meta = json.loads((folder / 'job.json').read_text())
        document = Path(meta['document'])
        data = document.read_bytes()
        selected = preview(document.name, data, body.get('header_row'))
        if action == 'preview':
            return dict(selected, job_id=job_id, bedrock_enabled=self.mapper is not None)
        if action == 'suggestions':
            if self.mapper is None:
                raise ValueError('Bedrock is disabled. Map manually, or restart with --bedrock.')
            suggestion_path = folder / f"suggestion-{selected['header_row']}.json"
            if suggestion_path.exists():
                return json.loads(suggestion_path.read_text())
            suggestion = self.mapper.suggest(selected['headers'])
            suggestion_path.write_text(json.dumps(suggestion))
            return suggestion
        if body.get('confirm') is not True:
            raise ValueError('Confirm your reviewed mapping first')
        suggestion_path = folder / f"suggestion-{selected['header_row']}.json"
        suggestion = json.loads(suggestion_path.read_text()) if suggestion_path.exists() else None
        record = approval_record(selected['headers'], selected['header_row'], body.get('mapping'),
                                 hashlib.sha256(data).hexdigest(), 'local-operator', suggestion)
        validate_roster(document.name, data, header_row=record['header_row'], column_map=record['column_map'])
        key = str(document) + '.mapping.json'
        try:
            with open(key, 'x') as target:
                json.dump(record, target)
        except FileExistsError:
            previous = json.loads(Path(key).read_text())
            if any(previous[k] != record[k] for k in ('header_row', 'column_map', 'source_sha256')):
                raise ValueError('Mapping already approved. Upload a new case to change it.')
        result_path = folder / 'result.json'
        if result_path.exists():
            return self.detail(json.loads(result_path.read_text())['case_id'])
        result = self.process(document, meta['company'], key)
        result_path.write_text(json.dumps({'case_id': result['case']['case_id']}))
        return result


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
                if path == '/config.json':
                    return self.respond(200, {'mode': 'local'})
                if path in ('/web.js', '/auth.js'):
                    return self.respond(200, Path(__file__).with_name(path[1:]).read_bytes(), 'text/javascript; charset=utf-8')
                if path == '/':
                    return self.respond(200, Path(__file__).with_name('web.html').read_bytes(), 'text/html; charset=utf-8')
                if path == '/api/cases':
                    return self.respond(200, {'cases': [c.model_dump(mode='json') for c in app.store.list_cases()],
                        'companies': [asdict(c) for c in COMPANIES.values()], 'demo_date': START.date().isoformat()})
                if path.startswith('/api/cases/'):
                    return self.respond(200, app.detail(path.rsplit('/', 1)[-1]))
                if path in ('/samples/clean_roster.csv', '/samples/messy_roster.csv', '/samples/unfamiliar_headers.csv'):
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
            path = urlsplit(self.path).path
            if path not in ('/api/cases', '/api/mapping-preview', '/api/mapping-suggestions', '/api/mapping-approve'):
                return self.respond(404, {'error': 'Not found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_UPLOAD:
                    return self.respond(413, {'error': 'Choose a nonempty file under 5 MB'})
                query = parse_qs(urlsplit(self.path).query)
                data = self.rfile.read(length)
                if path.startswith('/api/mapping-') and self.headers.get('Content-Type', '').startswith('application/json'):
                    if length > 16384:
                        raise ValueError('Mapping request too large')
                    body = json.loads(data)
                    if not isinstance(body, dict):
                        raise ValueError('Expected a JSON object')
                    result = app.mapping(path.rsplit('-', 1)[-1], body)
                else:
                    if path not in ('/api/cases', '/api/mapping-preview'):
                        raise ValueError('Use JSON for mapping review actions')
                    result = app.submit(query.get('filename', [''])[0], query.get('company', [''])[0], data,
                                        review=path == '/api/mapping-preview')
                self.respond(201, result)
            except FileNotFoundError:
                self.respond(404, {'error': 'Mapping review not found. Upload the roster again.'})
            except ApiError as exc:
                self.respond(exc.status, {'error': exc.message})
            except (ValueError, KeyError, MappingUnavailable) as exc:
                self.respond(400, {'error': str(exc)})
            except Exception:
                self.log_error('Roster processing failed')
                self.respond(500, {'error': 'Roster processing failed. Check the file and try again.'})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--store', default='.local/store')
    parser.add_argument('--bedrock', action='store_true', help='Enable header-only Bedrock suggestions in Sydney')
    args = parser.parse_args()
    server = HTTPServer(('127.0.0.1', args.port), make_handler(WebApp(args.store, mapper=BedrockColumnMapper() if args.bedrock else None)))
    print(f'Enrollment Desk: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
