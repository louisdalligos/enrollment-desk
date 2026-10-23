import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from enrollment_desk.web import MAX_UPLOAD, ROOT, WebApp, make_handler


@pytest.fixture
def server(tmp_path):
    app = WebApp(tmp_path / 'store', ROOT / 'data/generated/existing_members.json')
    http = HTTPServer(('127.0.0.1', 0), make_handler(app))
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}', app
    http.shutdown()
    http.server_close()
    thread.join()


def test_web_upload_validation_persistence_and_details(server):
    url, app = server
    with urllib.request.urlopen(url) as response:
        assert b'Enrollment Desk' in response.read()
    for filename, expected in [('clean_roster.csv', 'active'), ('messy_roster.csv', 'needs_review')]:
        request = urllib.request.Request(
            url + '/api/cases?company=CMP-DALISAY&filename=' + filename,
            data=(ROOT / 'data/samples' / filename).read_bytes(),
            headers={'X-Enrollment-Desk': 'local', 'Origin': url}, method='POST')
        with urllib.request.urlopen(request) as response:
            assert response.status == 201
            detail = json.load(response)
        assert detail['case']['status'] == expected
        assert detail['events']
        case_id = detail['case']['case_id']
        with urllib.request.urlopen(url + '/api/cases/' + case_id) as response:
            assert json.load(response)['case']['case_id'] == case_id
        if expected == 'active':
            assert detail['case']['ack_id']
            assert detail['case']['member_numbers']
        else:
            assert detail['case']['flags']
            assert not detail['case']['submission_id']
    with urllib.request.urlopen(url + '/api/cases') as response:
        assert len(json.load(response)['cases']) == 2
    assert len(WebApp(app.store.root).store.list_cases()) == 2


@pytest.mark.parametrize('headers,expected', [({}, 403), ({'X-Enrollment-Desk': 'local', 'Origin': 'https://elsewhere.example'}, 403), ({'X-Enrollment-Desk': 'local', 'Content-Length': '0'}, 413)])
def test_web_rejects_untrusted_or_empty_upload(server, headers, expected):
    url, _ = server
    request = urllib.request.Request(url + '/api/cases', data=b'', headers=headers, method='POST')
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == expected


@pytest.mark.parametrize('filename,company,data', [('bad.exe', 'CMP-DALISAY', b'data'), ('roster.csv', 'UNKNOWN', b'data'), ('roster.csv', 'CMP-DALISAY', b''), ('roster.csv', 'CMP-DALISAY', b'x' * (MAX_UPLOAD + 1))], ids=['extension', 'company', 'empty', 'oversize'])
def test_invalid_upload_does_not_create_case(tmp_path, filename, company, data):
    app = WebApp(tmp_path)
    with pytest.raises(ValueError):
        app.submit(filename, company, data)
    assert not app.store.list_cases()


def test_case_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        WebApp(tmp_path).detail('../../outside')


def test_local_mapping_review_preserves_rules_and_approval_log(server):
    url, app = server
    data = (ROOT / 'data/samples/clean_roster.csv').read_bytes()
    selected = app.submit('test.csv', 'CMP-DALISAY', data, review=True)
    assert not app.store.list_cases()
    body = {'job_id': selected['job_id'], 'header_row': selected['header_row'],
            'mapping': selected['mapping'], 'confirm': True}
    result = app.mapping('approve', body)
    assert result['case']['status'] == 'active'
    assert result['case']['ack_id']
    assert any(e['type'] == 'columns.approved' for e in result['events'])
    again = app.mapping('approve', body)
    assert again['case']['case_id'] == result['case']['case_id']
    assert len(app.store.list_cases()) == 1


def test_mapping_http_requires_explicit_confirmation(server):
    url, app = server
    data = b'Payroll reference,Given appellation,Family appellation\nSYN-1,Ana,Santos\n'
    request = urllib.request.Request(url + '/api/mapping-preview?company=CMP-DALISAY&filename=test.csv',
        data=data, headers={'X-Enrollment-Desk': 'local'}, method='POST')
    with urllib.request.urlopen(request) as response:
        selected = json.load(response)
    request = urllib.request.Request(url + '/api/mapping-approve',
        data=json.dumps({'job_id': selected['job_id'], 'header_row': 0, 'mapping': {}}).encode(),
        headers={'X-Enrollment-Desk': 'local', 'Content-Type': 'application/json'}, method='POST')
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request)
    assert exc.value.code == 400
    assert not app.store.list_cases()


@pytest.mark.parametrize('source,expected', [('clean_roster.csv', 'active'), ('messy_roster.csv', 'needs_review')])
@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_manual_unfamiliar_header_http_workflow(server, source, expected, extension, monkeypatch):
    import csv
    import io
    from unittest.mock import Mock
    from openpyxl import Workbook
    from enrollment_desk.column_mapping import preview, BedrockColumnMapper
    from enrollment_desk.roster_parser import read_rows

    url, app = server
    # Preserve labeled synthetic values while replacing every header with an unknown label.
    original = (ROOT / 'data/samples' / source).read_bytes()
    known = preview(source, original)
    rows = read_rows(source, original)
    rows[known['header_row']] = [f'Unfamiliar source column {i + 1}' for i in range(len(known['headers']))]
    if extension == 'csv':
        output = io.StringIO()
        csv.writer(output).writerows(rows)
        data = output.getvalue().encode()
    else:
        book = Workbook()
        for row in rows:
            book.active.append(row)
        output = io.BytesIO()
        book.save(output)
        book.close()
        data = output.getvalue()
    model_call = Mock(side_effect=AssertionError('Manual mode must not invoke Bedrock'))
    monkeypatch.setattr(BedrockColumnMapper, 'suggest', model_call)

    def post(path, body, raw=False):
        request = urllib.request.Request(url + path, data=body if raw else json.dumps(body).encode(),
            headers={'X-Enrollment-Desk': 'local', 'Origin': url,
                     'Content-Type': 'application/octet-stream' if raw else 'application/json'}, method='POST')
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    selected = post(f'/api/mapping-preview?company=CMP-DALISAY&filename=unknown.{extension}', data, raw=True)
    assert selected['bedrock_enabled'] is False
    assert all(field is None for field in selected['mapping'].values())
    assert not app.store.list_cases()
    body = {'job_id': selected['job_id'], 'header_row': known['header_row']}
    selected = post('/api/mapping-preview', body)
    assert selected['header_row'] == known['header_row']
    body['mapping'] = known['mapping']
    with pytest.raises(urllib.error.HTTPError) as error:
        post('/api/mapping-approve', body)
    assert error.value.code == 400
    assert not app.store.list_cases()
    body['confirm'] = True
    result = post('/api/mapping-approve', body)
    assert result['case']['status'] == expected
    event = next(e for e in result['events'] if e['type'] == 'columns.approved')
    assert event['data']['column_map'] == known['mapping']
    assert event['data']['approved_by'] == 'local-operator'
    assert event['data']['model_id'] is None
    if expected == 'active':
        assert result['case']['ack_id'] and result['case']['member_numbers']
    else:
        assert result['case']['flags'] and not result['case']['submission_id']
    repeated = post('/api/mapping-approve', body)
    assert repeated['case']['case_id'] == result['case']['case_id']
    assert len(app.store.list_cases()) == 1
    model_call.assert_not_called()


def test_mapping_endpoints_reject_invalid_json_shapes_and_raw_approval(server):
    url, app = server
    for content, content_type in [(b'[]', 'application/json'), (b'null', 'application/json'),
                                  (b'Employee ID,First Name,Last Name\nX,A,B', 'application/octet-stream')]:
        request = urllib.request.Request(url + '/api/mapping-approve?company=CMP-DALISAY&filename=test.csv',
            data=content, headers={'X-Enrollment-Desk': 'local', 'Content-Type': content_type}, method='POST')
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 400
    assert not app.store.list_cases()
