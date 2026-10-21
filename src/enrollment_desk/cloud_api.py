"""Authenticated HTTP API for the AWS enrollment workflow (API Gateway v2)."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from botocore.exceptions import ClientError

from .reference import COMPANIES
from .roster_parser import parse_roster
from .store import S3Store
from .workflow.steps import readiness

MAX_UPLOAD = 5 * 1024 * 1024
MAX_MEMBERS = 500
ID = re.compile(r'[a-f0-9]{32}')
SUB = re.compile(r'[a-f0-9-]{36}')
MISSING = {'NoSuchKey', '404', 'NotFound'}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def validate_roster(filename, data):
    """Reject oversized/invalid containers before the workflow can consume them."""
    if not 0 < len(data) <= MAX_UPLOAD:
        raise ApiError(400, 'Choose a nonempty roster up to 5 MB.')
    if filename.endswith('.xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(i.file_size for i in entries) > 20 * 1024 * 1024:
                    raise ApiError(400, 'Excel file expands beyond the supported limit.')
        except zipfile.BadZipFile:
            raise ApiError(400, 'Invalid Excel file.') from None
    try:
        roster = parse_roster(filename, data)
    except Exception:
        raise ApiError(400, 'Cannot read this roster. Upload a valid CSV or XLSX file.') from None
    if not 0 < len(roster.members) <= MAX_MEMBERS:
        raise ApiError(400, f'Upload between 1 and {MAX_MEMBERS} members per roster.')
    if len(roster.model_dump_json().encode()) > 128 * 1024:
        raise ApiError(400, 'Parsed roster is too large for the workflow. Split it into smaller files.')


class CloudApi:
    def __init__(self, s3, sfn, bucket, state_machine, store=None):
        self.s3, self.sfn = s3, sfn
        self.bucket, self.state_machine = bucket, state_machine
        self.store = store or S3Store(bucket)

    def exists(self, key):
        result = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=key, MaxKeys=1)
        return any(obj['Key'] == key for obj in result.get('Contents', []))

    def read(self, key):
        return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)['Body'].read())

    def write(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(data).encode(), ContentType='application/json')

    def companies(self, sub):
        if not self.exists(f'web-access/{sub}.json'):
            return set()
        try:
            allowed = self.read(f'web-access/{sub}.json').get('company_ids', [])
        except ClientError as exc:
            if exc.response['Error']['Code'] not in MISSING:
                raise
            allowed = []
        return {cid for cid in allowed if cid in COMPANIES}

    def job(self, sub, job_id, allowed):
        if not ID.fullmatch(job_id):
            raise ApiError(404, 'Case not found.')
        if not self.exists(f'web/jobs/{sub}/{job_id}.json'):
            raise ApiError(404, 'Case not found.')
        try:
            job = self.read(f'web/jobs/{sub}/{job_id}.json')
        except ClientError as exc:
            if exc.response['Error']['Code'] in MISSING:
                raise ApiError(404, 'Case not found.') from None
            raise
        if job['company_id'] not in allowed:
            raise ApiError(404, 'Case not found.')
        return job

    def create_upload(self, sub, allowed, body):
        company = body.get('company_id')
        if company not in allowed:
            raise ApiError(403, 'This company is not assigned to your account.')
        filename = str(body.get('filename', '')).replace('\\', '/').split('/')[-1]
        if len(filename) > 150 or not re.fullmatch(r'[\w .()-]+\.(csv|xlsx)', filename, re.ASCII):
            raise ApiError(400, 'Use a CSV or XLSX filename with letters, numbers, spaces, dots, hyphens or parentheses.')
        size = body.get('size')
        if type(size) is not int or not 0 < size <= MAX_UPLOAD:
            raise ApiError(400, 'Choose a nonempty roster up to 5 MB.')
        job_id = uuid4().hex
        now = datetime.now(timezone.utc)
        reference = f'END-{now:%Y%m%d}-{job_id.upper()}'
        staging = f'uploads/web/{sub}/{job_id}/{filename}'
        document = f'incoming/web/{sub}/{job_id}/{filename}'
        job = dict(job_id=job_id, company_id=company, filename=filename, size=size,
                   created_at=now.isoformat(), reference_number=reference, case_id=reference.lower(),
                   staging_key=staging, document_key=document)
        self.write(f'web/jobs/{sub}/{job_id}.json', job)
        post = self.s3.generate_presigned_post(
            Bucket=self.bucket, Key=staging, Fields={'Content-Type': 'application/octet-stream'},
            Conditions=[['content-length-range', size, size], {'Content-Type': 'application/octet-stream'}],
            ExpiresIn=300)
        return {'job_id': job_id, 'upload': post}

    def start(self, job):
        # Final input is immutable: retries never replace a roster already queued.
        if not self.exists(job['document_key']):
            if not self.exists(job['staging_key']):
                raise ApiError(409, 'Upload the roster before starting this case.')
            try:
                head = self.s3.head_object(Bucket=self.bucket, Key=job['staging_key'])
            except ClientError as missing:
                if missing.response['Error']['Code'] in MISSING:
                    raise ApiError(409, 'Upload the roster before starting this case.') from None
                raise
            if head['ContentLength'] != job['size'] or head['ContentLength'] > MAX_UPLOAD:
                raise ApiError(400, 'Uploaded file size does not match.')
            # Versioned read prevents replacing the object between inspection and download.
            version = head.get('VersionId')
            if not version or version == 'null':
                raise ApiError(503, 'Upload storage versioning is not ready. Try again shortly.')
            response = self.s3.get_object(Bucket=self.bucket, Key=job['staging_key'], VersionId=version)
            stream = response['Body']
            try:
                data = stream.read(MAX_UPLOAD + 1)
            finally:
                stream.close()
            if len(data) != job['size']:
                raise ApiError(400, 'Uploaded file size does not match.')
            validate_roster(job['filename'], data)
            try:
                self.s3.put_object(Bucket=self.bucket, Key=job['document_key'], Body=data,
                                   ContentType='application/octet-stream', IfNoneMatch='*')
            except ClientError as race:
                if race.response['Error']['Code'] not in ('PreconditionFailed', '412'):
                    raise
        payload = {'document_key': job['document_key'], 'company_id': job['company_id'],
                   'reference_number': job['reference_number']}
        try:
            self.sfn.start_execution(stateMachineArn=self.state_machine, name=job['job_id'],
                                     input=json.dumps(payload, sort_keys=True))
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ExecutionAlreadyExists':
                raise
        return {'job_id': job['job_id'], 'status': 'queued'}

    def snapshot(self, job, include_events=False):
        result = dict(job_id=job['job_id'], case_id=job['case_id'], reference_number=job['reference_number'],
                      company_id=job['company_id'], source_document=job['filename'],
                      received_at=job['created_at'], status='awaiting_upload', member_count=0)
        execution = self.state_machine.replace(':stateMachine:', ':execution:') + ':' + job['job_id']
        try:
            description = self.sfn.describe_execution(executionArn=execution)
            state = description['status']
            result['execution_status'] = state
            result['status'] = 'processing' if state == 'RUNNING' else 'failed'
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ExecutionDoesNotExist':
                raise
            state = None
        case = None
        try:
            if self.exists(f"cases/{job['case_id']}.json"):
                case = self.store.get_case(job['case_id'])
        except ClientError as exc:
            if exc.response['Error']['Code'] not in MISSING:
                raise
        if case is not None:
            if case.source_document != job['document_key'] or case.company_id != job['company_id']:
                raise ApiError(404, 'Case not found.')
            result['member_count'] = len(case.members)
            if state == 'SUCCEEDED':
                result['status'] = case.status.value
            elif state == 'RUNNING':
                result['status'] = case.status.value if case.status.value not in ('active', 'needs_review') else 'processing'
        if not include_events:
            return result
        return {'job': result, 'case': case.model_dump(mode='json') if case else None,
                'events': [e.model_dump(mode='json') for e in self.store.events(case.case_id)] if case else [],
                'readiness': readiness(case) if case else []}

    def dispatch(self, event):
        claims = event.get('requestContext', {}).get('authorizer', {}).get('jwt', {}).get('claims', {})
        sub = claims.get('sub', '')
        if not SUB.fullmatch(sub) or claims.get('token_use') != 'access':
            raise ApiError(401, 'Sign in to continue.')
        if 'enrollment/access' not in str(claims.get('scope', '')).split():
            raise ApiError(403, 'Enrollment access is required.')
        allowed = self.companies(sub)
        method = event['requestContext']['http']['method']
        path = event.get('rawPath', '')
        body = {}
        if method == 'POST':
            raw = event.get('body') or '{}'
            if event.get('isBase64Encoded'):
                raw = base64.b64decode(raw, validate=True).decode()
            if len(raw) > 4096:
                raise ApiError(400, 'Request body is too large.')
            try:
                body = json.loads(raw)
            except (ValueError, TypeError):
                raise ApiError(400, 'Invalid JSON request.') from None
            if not isinstance(body, dict):
                raise ApiError(400, 'Expected a JSON object.')
        if method == 'POST' and path == '/api/uploads':
            return 201, self.create_upload(sub, allowed, body)
        if method == 'POST' and path == '/api/cases':
            job = self.job(sub, str(body.get('job_id', '')), allowed)
            return 202, self.start(job)
        if method == 'GET' and path == '/api/cases':
            query = event.get('queryStringParameters') or {}
            args = dict(Bucket=self.bucket, Prefix=f'web/jobs/{sub}/', MaxKeys=20)
            if query.get('cursor'):
                args['ContinuationToken'] = query['cursor']
            page = self.s3.list_objects_v2(**args)
            jobs = [self.read(obj['Key']) for obj in page.get('Contents', [])]
            return 200, {'cases': [self.snapshot(j) for j in jobs if j['company_id'] in allowed],
                         'companies': [asdict(COMPANIES[c]) for c in sorted(allowed)],
                         'next_cursor': page.get('NextContinuationToken'), 'mode': 'aws'}
        if method == 'GET' and path.startswith('/api/cases/'):
            job = self.job(sub, path.rsplit('/', 1)[-1], allowed)
            return 200, self.snapshot(job, include_events=True)
        raise ApiError(404, 'Not found.')


@lru_cache(maxsize=1)
def runtime():
    import boto3
    from botocore.config import Config
    region = os.environ['AWS_REGION']
    s3 = boto3.client('s3', endpoint_url=f'https://s3.{region}.amazonaws.com',
                      config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
    return CloudApi(s3, boto3.client('stepfunctions'), os.environ['CASE_BUCKET'], os.environ['STATE_MACHINE_ARN'])


def lambda_handler(event, context):
    try:
        status, data = runtime().dispatch(event)
    except ApiError as exc:
        status, data = exc.status, {'error': exc.message}
    except (ValueError, TypeError):
        status, data = 400, {'error': 'Invalid request.'}
    except Exception:
        # Never log the request, roster, token, or upstream exception body.
        logging.error('Enrollment API request failed: %s', getattr(context, 'aws_request_id', 'unknown'))
        status, data = 503, {'error': 'Enrollment service is temporarily unavailable. Try again.'}
    return {'statusCode': status, 'headers': {'content-type': 'application/json', 'cache-control': 'no-store',
                                             'x-content-type-options': 'nosniff'}, 'body': json.dumps(data)}
