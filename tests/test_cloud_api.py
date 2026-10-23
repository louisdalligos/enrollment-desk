import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from enrollment_desk import cloud_api
from enrollment_desk.cloud_api import ApiError, CloudApi, MAX_UPLOAD, validate_roster
from enrollment_desk.insurer_client import InProcessInsurer
from enrollment_desk.insurer_sim.core import InsurerSimulator, SimulatorConfig
from enrollment_desk.models import Case, Event
from enrollment_desk.store import LocalStore
from enrollment_desk.cli import START
from enrollment_desk.workflow import local_runner, steps

ROOT = Path(__file__).resolve().parents[1]
USER = '11111111-1111-1111-1111-111111111111'
OTHER = '22222222-2222-2222-2222-222222222222'
MACHINE = 'arn:aws:states:ap-southeast-1:123456789012:stateMachine:enrollment'
CLEAN = (ROOT / 'data/samples/clean_roster.csv').read_bytes()


def error(code):
    return ClientError({'Error': {'Code': code}}, 'test')


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.versions = {}
        self.read_versions = []
        self.presign = Mock(return_value={'url': 'https://bucket.s3.ap-southeast-1.amazonaws.com', 'fields': {'key': 'test'}})
        self.generate_presigned_post = self.presign

    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get('IfNoneMatch') == '*' and Key in self.objects:
            raise error('PreconditionFailed')
        data = Body if isinstance(Body, bytes) else Body.encode()
        version = str(len(self.versions) + 1)
        self.versions[Key, version] = data
        self.objects[Key] = (version, data)
        return {'VersionId': version}

    def get_object(self, Bucket, Key, VersionId=None):
        if Key not in self.objects:
            raise error('NoSuchKey')
        if VersionId:
            self.read_versions.append(VersionId)
            data = self.versions[Key, VersionId]
        else:
            data = self.objects[Key][1]
        return {'Body': io.BytesIO(data)}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error('404')
        version, data = self.objects[Key]
        return {'VersionId': version, 'ContentLength': len(data)}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=20, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        offset = int(ContinuationToken or 0)
        page = {'Contents': [{'Key': k} for k in keys[offset:offset + MaxKeys]]}
        if len(keys) > offset + MaxKeys:
            page['NextContinuationToken'] = str(offset + MaxKeys)
        return page


class MemorySfn:
    def __init__(self):
        self.executions = {}
        self.starts = []

    def start_execution(self, stateMachineArn, name, input):
        self.starts.append((name, input))
        arn = stateMachineArn.replace(':stateMachine:', ':execution:') + ':' + name
        if arn in self.executions:
            raise error('ExecutionAlreadyExists')
        self.executions[arn] = {'status': 'RUNNING'}
        return {'executionArn': arn}

    def describe_execution(self, executionArn):
        if executionArn not in self.executions:
            raise error('ExecutionDoesNotExist')
        return self.executions[executionArn]


class CaseStore:
    def __init__(self, s3):
        self.s3 = s3

    def get_case(self, case_id):
        return Case.model_validate_json(self.s3.get_object(Bucket='bucket', Key=f'cases/{case_id}.json')['Body'].read())

    def events(self, case_id):
        return [Event.model_validate_json(v[1]) for k, v in self.s3.objects.items() if k.startswith(f'events/{case_id}/')]


@pytest.fixture
def api():
    s3, sfn = MemoryS3(), MemorySfn()
    app = CloudApi(s3, sfn, 'bucket', MACHINE, CaseStore(s3))
    app.write(f'web-access/{USER}.json', {'company_ids': ['CMP-DALISAY']})
    return app


def event(method='GET', path='/api/cases', body=None, user=USER):
    return {'requestContext': {'http': {'method': method}, 'authorizer': {'jwt': {'claims': {
        'sub': user, 'token_use': 'access', 'scope': 'openid enrollment/access'}}}},
        'rawPath': path, 'body': json.dumps(body) if body is not None else None}


def upload(api, user=USER, filename='clean_roster.csv', data=CLEAN, review=False):
    _, grant = api.dispatch(event('POST', '/api/uploads', {'company_id': 'CMP-DALISAY', 'filename': filename, 'size': len(data), 'review_mapping': review}, user))
    job = api.job(user, grant['job_id'], {'CMP-DALISAY'})
    api.s3.put_object(Bucket='bucket', Key=job['staging_key'], Body=data)
    return job


def test_access_token_and_scope_required_before_storage(api):
    for claims in [{}, {'sub': USER, 'token_use': 'id'}, {'sub': '../bad', 'token_use': 'access'}]:
        request = event()
        request['requestContext']['authorizer']['jwt']['claims'] = claims
        with pytest.raises(ApiError) as exc:
            api.dispatch(request)
        assert exc.value.status == 401
    request = event()
    request['requestContext']['authorizer']['jwt']['claims']['scope'] = 'openid'
    with pytest.raises(ApiError) as exc:
        api.dispatch(request)
    assert exc.value.status == 403


def test_unassigned_user_has_no_companies_or_upload_permission(api):
    _, response = api.dispatch(event(user=OTHER))
    assert response['companies'] == response['cases'] == []
    with pytest.raises(ApiError) as exc:
        api.dispatch(event('POST', '/api/uploads', {'company_id': 'CMP-DALISAY'}, OTHER))
    assert exc.value.status == 403


def test_presigned_post_is_exact_size_key_and_short_lived(api):
    job = upload(api)
    args = api.s3.presign.call_args.kwargs
    assert args['Key'].startswith(f'uploads/web/{USER}/{job["job_id"]}/')
    assert ['content-length-range', len(CLEAN), len(CLEAN)] in args['Conditions']
    assert args['ExpiresIn'] == 300
    assert api.sfn.executions == {}


@pytest.mark.parametrize('body', [
    {'company_id': 'CMP-TALA', 'filename': 'a.csv', 'size': 1},
    {'company_id': 'CMP-DALISAY', 'filename': 'a.exe', 'size': 1},
    {'company_id': 'CMP-DALISAY', 'filename': 'a.csv', 'size': MAX_UPLOAD + 1},
    {'company_id': 'CMP-DALISAY', 'filename': 'a.csv', 'size': True},
])
def test_invalid_grants_denied(api, body):
    with pytest.raises(ApiError):
        api.dispatch(event('POST', '/api/uploads', body))
    assert not api.s3.presign.called


def test_cross_user_and_revoked_company_access_denied(api):
    job = upload(api)
    api.write(f'web-access/{OTHER}.json', {'company_ids': ['CMP-DALISAY']})
    for method, path, body in [('GET', '/api/cases/' + job['job_id'], None), ('POST', '/api/cases', {'job_id': job['job_id']})]:
        with pytest.raises(ApiError) as exc:
            api.dispatch(event(method, path, body, OTHER))
        assert exc.value.status == 404
    api.write(f'web-access/{USER}.json', {'company_ids': []})
    with pytest.raises(ApiError) as exc:
        api.dispatch(event('GET', '/api/cases/' + job['job_id']))
    assert exc.value.status == 404
    assert api.dispatch(event())[1]['cases'] == []


def test_start_is_async_and_retry_cannot_replace_final_input(api):
    job = upload(api)
    _, result = api.dispatch(event('POST', '/api/cases', {'job_id': job['job_id']}))
    assert result['status'] == 'queued'
    assert api.s3.read_versions
    original = api.s3.objects[job['document_key']][1]
    api.s3.put_object(Bucket='bucket', Key=job['staging_key'], Body=b'replaced')
    api.dispatch(event('POST', '/api/cases', {'job_id': job['job_id']}))
    assert len(api.sfn.executions) == 1
    assert api.sfn.starts[0] == api.sfn.starts[1]
    assert api.s3.objects[job['document_key']][1] == original == CLEAN
    detail = api.dispatch(event('GET', '/api/cases/' + job['job_id']))[1]
    assert detail['job']['status'] == 'processing' and detail['case'] is None


def test_upload_failure_and_invalid_content_never_start_execution(api):
    job = upload(api, data=b'not a roster')
    with pytest.raises(ApiError):
        api.start(job)
    assert not api.sfn.executions
    del api.s3.objects[job['staging_key']]
    with pytest.raises(ApiError) as exc:
        api.start(job)
    assert exc.value.status == 409


def test_overwritten_staging_read_uses_inspected_version(api):
    job = upload(api)
    original_head = api.s3.head_object
    def race(**kwargs):
        result = original_head(**kwargs)
        if kwargs['Key'] == job['staging_key']:
            api.s3.put_object(Bucket='bucket', Key=job['staging_key'], Body=b'malicious replacement')
        return result
    api.s3.head_object = race
    api.start(job)
    assert api.s3.objects[job['document_key']][1] == CLEAN


@pytest.mark.parametrize('filename,status,reject_rate', [('clean_roster.csv', 'active', 0), ('messy_roster.csv', 'needs_review', 0), ('clean_roster.csv', 'needs_review', 1)])
def test_workflow_reference_mapping_and_case_details(api, tmp_path, filename, status, reject_rate):
    data = (ROOT / 'data/samples' / filename).read_bytes()
    job = upload(api, filename=filename, data=data)
    api.start(job)
    payload = json.loads(api.sfn.starts[0][1])
    store = LocalStore(tmp_path)
    store.read_document = lambda key: api.s3.objects[key][1]
    clock = local_runner.FakeClock(START)
    sim = InsurerSimulator(SimulatorConfig(reject_rate=reject_rate))
    rt = steps.Runtime(store, InProcessInsurer(sim, clock), clock, lambda cid: {}, clock.advance)
    # Drive the actual workflow steps with the cloud-provided stable reference.
    state = steps.intake(payload, rt)
    assert state['case_id'] == job['case_id']
    state = steps.validate_case(state, rt)
    if state['all_clear']:
        state = steps.submit(state, rt)
        clock.advance(300)
        state = steps.check_ack(state, rt)
        if state['ack_status'] == 'rejected':
            steps.mark_for_review(state, rt)
        else:
            steps.mark_active(state, rt)
    else:
        steps.mark_for_review(state, rt)
    case = store.get_case(job['case_id'])
    api.s3.put_object(Bucket='bucket', Key=f'cases/{case.case_id}.json', Body=case.model_dump_json().encode())
    for index, item in enumerate(store.events(case.case_id)):
        api.s3.put_object(Bucket='bucket', Key=f'events/{case.case_id}/{index}.json', Body=item.model_dump_json().encode())
    execution = next(iter(api.sfn.executions.values()))
    execution['status'] = 'SUCCEEDED'
    detail = api.dispatch(event('GET', '/api/cases/' + job['job_id']))[1]
    assert detail['case']['status'] == detail['job']['status'] == status
    assert detail['events']
    if status == 'active':
        assert detail['case']['ack_id'] and detail['readiness']
    elif filename == 'messy_roster.csv':
        assert not detail['case']['submission_id']
    else:
        assert detail['case']['submission_id'] and not detail['case']['ack_id']
    execution['status'] = 'FAILED'
    assert api.snapshot(job)['status'] == 'failed'


def test_failed_execution_and_pagination(api):
    for _ in range(21):
        upload(api)
    _, first = api.dispatch(event())
    assert len(first['cases']) == 20 and first['next_cursor']
    request = event()
    request['queryStringParameters'] = {'cursor': first['next_cursor']}
    assert len(api.dispatch(request)[1]['cases']) == 1
    job = upload(api)
    api.start(job)
    next(iter(api.sfn.executions.values()))['status'] = 'TIMED_OUT'
    assert api.snapshot(job)['status'] == 'failed'


def test_bad_excel_and_expansion_limit():
    with pytest.raises(ApiError):
        validate_roster('bad.xlsx', b'not a workbook')
    import zipfile
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('huge.xml', b'0' * (21 * 1024 * 1024))
    with pytest.raises(ApiError, match='expands'):
        validate_roster('bad.xlsx', data.getvalue())


def test_errors_do_not_leak_internal_details(api, monkeypatch):
    monkeypatch.setattr(cloud_api, 'runtime', lambda: api)
    api.dispatch = Mock(side_effect=RuntimeError('sensitive body / token'))
    result = cloud_api.lambda_handler({}, SimpleNamespace(aws_request_id='safe-id'))
    assert result['statusCode'] == 503
    assert 'sensitive' not in result['body']


def test_roster_shape_limits_before_execution():
    from enrollment_desk.roster_parser import read_rows
    with pytest.raises(ValueError, match='exceeds'):
        read_rows('rows.csv', b'a,b,c\n' * 1016)
    with pytest.raises(ValueError, match='exceeds'):
        read_rows('columns.csv', (','.join(['column'] * 101)).encode())
    header = b'employee id,first name,last name,birth date\n'
    with pytest.raises(ApiError, match='smaller files'):
        validate_roster('large.csv', header + (b'1,' + b'a' * 70000 + b',Smith,1990-01-01\n'))


def test_real_sdk_presigned_post_matches_regional_origin_and_size_policy():
    import base64
    import boto3
    from botocore.config import Config
    from botocore.stub import Stubber, ANY
    client = boto3.client('s3', region_name='ap-southeast-1',
        endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
        aws_access_key_id='fake-key', aws_secret_access_key='fake-secret', aws_session_token='fake-session',
        config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
    assert 'IfNoneMatch' in client.meta.service_model.operation_model('PutObject').input_shape.members
    app = CloudApi(client, MemorySfn(), 'test-bucket', MACHINE, CaseStore(MemoryS3()))
    with Stubber(client) as stub:
        stub.add_response('put_object', {}, {'Bucket': 'test-bucket', 'Key': ANY, 'Body': ANY, 'ContentType': 'application/json'})
        grant = app.create_upload(USER, {'CMP-DALISAY'}, {'company_id': 'CMP-DALISAY', 'filename': 'test.csv', 'size': 100})
        stub.assert_no_pending_responses()
    post = grant['upload']
    assert post['url'] == 'https://test-bucket.s3.ap-southeast-1.amazonaws.com/'
    policy = json.loads(base64.b64decode(post['fields']['policy']))
    assert ['content-length-range', 100, 100] in policy['conditions']
    assert post['fields']['x-amz-security-token'] == 'fake-session'


def test_reviewed_mapping_requires_confirmation_and_is_immutable(api):
    data = b'Payroll reference,Given appellation,Family appellation\nSYN-1,Ana,Santos\n'
    job = upload(api, data=data, review=True)
    body = {'job_id': job['job_id'], 'header_row': 0,
            'mapping': {'0': 'employee_id', '1': 'first_name', '2': 'last_name'}}
    status, selected = api.dispatch(event('POST', '/api/mapping-preview', body))
    assert status == 200 and selected['headers'][0] == 'Payroll reference'
    assert not api.sfn.starts
    with pytest.raises(ApiError):
        api.dispatch(event('POST', '/api/mapping-approve', body))
    body['confirm'] = True
    status, result = api.dispatch(event('POST', '/api/mapping-approve', body))
    assert status == 202
    payload = json.loads(api.sfn.starts[-1][1])
    assert payload['mapping_key'] == job['document_key'] + '.mapping.json'
    approval = api.read(payload['mapping_key'])
    assert approval['approved_by'] == USER
    body['mapping']['0'] = None
    with pytest.raises(ApiError) as exc:
        api.dispatch(event('POST', '/api/mapping-approve', body))
    assert exc.value.status == 409


def test_mapping_routes_enforce_owner_and_manual_fallback(api):
    job = upload(api, review=True)
    with pytest.raises(ApiError) as exc:
        api.dispatch(event('POST', '/api/mapping-preview', {'job_id': job['job_id']}, OTHER))
    assert exc.value.status == 404
    with pytest.raises(ApiError) as exc:
        api.dispatch(event('POST', '/api/mapping-suggestions', {'job_id': job['job_id'], 'header_row': 0}))
    assert exc.value.status == 503
    assert not api.sfn.starts


def test_review_upload_cannot_bypass_approval(api):
    job = upload(api, review=True)
    with pytest.raises(ApiError) as exc:
        api.start(job)
    assert exc.value.status == 409
    assert not api.sfn.starts


def test_model_suggestion_is_cached_and_never_starts_workflow(api):
    job = upload(api, data=b'Payroll reference,Given appellation,Family appellation\nSYN-1,Ana,Santos\n', review=True)
    api.mapper = Mock()
    api.mapper.suggest.return_value = {'mapping': {'0': 'employee_id', '1': 'first_name', '2': 'last_name'},
                                     'model_id': 'amazon.nova-lite-v1:0', 'prompt_version': 'test'}
    body = {'job_id': job['job_id'], 'header_row': 0}
    assert api.dispatch(event('POST', '/api/mapping-suggestions', body))[0] == 200
    assert api.dispatch(event('POST', '/api/mapping-suggestions', body))[0] == 200
    api.mapper.suggest.assert_called_once()
    assert not api.sfn.starts
    body.update(mapping=api.mapper.suggest.return_value['mapping'], confirm=True)
    api.dispatch(event('POST', '/api/mapping-approve', body))
    approval = api.read(job['document_key'] + '.mapping.json')
    assert approval['model_id'] == 'amazon.nova-lite-v1:0'
    assert approval['suggested_mapping'] == body['mapping']
