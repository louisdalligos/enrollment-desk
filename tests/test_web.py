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
