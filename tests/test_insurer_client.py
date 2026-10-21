import hashlib
import io
import json
import urllib.error
from unittest.mock import Mock

import pytest

from enrollment_desk.insurer_client import HttpInsurer
from enrollment_desk.insurer_sim.core import InsurerUnavailable


@pytest.fixture
def captured(monkeypatch):
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr('urllib.request.urlopen', open_request)
    return requests


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_sigv4_signs_exact_request_with_session_credentials(monkeypatch, captured, method):
    botocore = pytest.importorskip('botocore.session')
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    credentials = Credentials('test-key', 'test-secret', 'test-token')
    monkeypatch.setattr(botocore, 'get_session', lambda: Mock(get_credentials=lambda: credentials))
    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    client = HttpInsurer('https://example.lambda-url.ap-southeast-1.on.aws/')
    if method == 'POST':
        client.submit({'name': 'Sample', 'count': 2}, 3)
    else:
        client.status('submission-123')
    request = captured[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert '/ap-southeast-1/lambda/aws4_request' in headers['authorization']
    assert headers['x-amz-security-token'] == 'test-token'
    assert headers['x-amz-content-sha256'] == hashlib.sha256(request.data or b'').hexdigest()
    if method == 'POST':
        assert request.full_url.endswith('/submissions?attempt=3')
        assert json.loads(request.data)['name'] == 'Sample'
    unsigned_headers = {k: v for k, v in headers.items() if k != 'authorization'}
    signed = AWSRequest(method=method, url=request.full_url, data=request.data, headers=unsigned_headers)
    signer = SigV4Auth(credentials.get_frozen_credentials(), 'lambda', 'ap-southeast-1')
    signed.context['timestamp'] = headers['x-amz-date']
    signature = signer.signature(signer.string_to_sign(signed, signer.canonical_request(signed)), signed)
    assert headers['authorization'].endswith('Signature=' + signature)


def test_local_simulator_remains_unsigned(captured):
    HttpInsurer('http://localhost:8081').submit({'test': True}, 1)
    assert 'authorization' not in dict((k.lower(), v) for k, v in captured[0].header_items())


def test_missing_credentials_fails_before_network(monkeypatch, captured):
    botocore = pytest.importorskip('botocore.session')
    monkeypatch.setattr(botocore, 'get_session', lambda: Mock(get_credentials=lambda: None))
    with pytest.raises(RuntimeError, match='AWS credentials required'):
        HttpInsurer('https://example.lambda-url.ap-southeast-1.on.aws').status('id')
    assert not captured


def test_lambda_url_requires_https(captured):
    with pytest.raises(ValueError, match='HTTPS'):
        HttpInsurer('http://example.lambda-url.ap-southeast-1.on.aws').status('id')
    assert not captured


@pytest.mark.parametrize('code', [403, 503])
def test_http_error_behavior(monkeypatch, code):
    error = urllib.error.HTTPError('http://localhost', code, 'error', {}, None)
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr('urllib.request.urlopen', fail)
    with pytest.raises(InsurerUnavailable if code == 503 else urllib.error.HTTPError):
        HttpInsurer('http://localhost:8081').status('id')
