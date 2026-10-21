# AWS web deployment

The repository now includes the cloud integration: private S3 frontend behind CloudFront, Cognito authorization-code sign-in with PKCE, JWT-protected API Gateway routes, Lambda API handlers, private versioned uploads, and asynchronous enrollment through the existing Step Functions workflow. No EC2 or Bedrock resource is required. The insurer remains a simulator.

The web infrastructure is opt-in (`enable_web=false` by default). Creating these files does not deploy resources. The local interface still runs independently without AWS credentials:

```powershell
.\.venv\Scripts\python.exe -m enrollment_desk.web
```

## Validate and package locally

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[aws,dev]"
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/auth.test.mjs tests/web.test.mjs
.\.venv\Scripts\python.exe -m enrollment_desk.cli evaluate data/generated
.\.venv\Scripts\python.exe scripts/build_lambda.py
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform test
```

Terraform 1.7+ is required. The Terraform tests use a mocked AWS provider to check workflow-only and web-enabled plans without AWS calls. Node 22+ is needed only for JavaScript tests. The package builder installs Linux x86_64 / Python 3.12 dependencies, including the SDK, in a fresh staging directory and creates the ignored deployment artifact `build/lambda.zip`. Windows cannot import-test Linux native modules; a deployed AWS smoke test is still required.

## Plan and deploy when authorized

1. Use the existing `copilot` profile for account `791956963770` and Sydney (`ap-southeast-2`). Singapore is blocked by this account's region policy. Set `$env:AWS_PROFILE = "copilot"` in the same PowerShell window, then confirm with `aws sts get-caller-identity --profile copilot`. Terraform validates the region and rejects any value other than Sydney.
2. Arrange an appropriate Terraform state location and backup. Existing configuration uses local state; use shared state with locking before team operations.
3. Copy `infra/terraform/web.tfvars.example` to `infra/terraform/web.auto.tfvars`. This enables hosting and sets `force_destroy_bucket=false`. The local tfvars file is ignored by Git. Keep it for every subsequent plan: disabling web later would remove the web stack.
4. Build the deployment package, initialize Terraform, then generate a reviewable plan:

```powershell
terraform "-chdir=infra/terraform" plan "-out=../../build/web.tfplan"
```

Review resource creation, IAM, bucket retention, account, region, and costs. This step requires AWS access. Apply only the reviewed plan after approval:

```powershell
terraform -chdir=infra/terraform apply ../../build/web.tfplan
terraform -chdir=infra/terraform output web_url
```

CloudFront and Cognito domain availability may take time. S3 versioning can also take time to become usable on a newly configured bucket; the API fails safely and asks for a retry if versioning is not yet ready. The frontend uses no-store assets, so normal code updates do not need a separate invalidation. Never upload roster documents to the frontend bucket.

## Invite an operator and assign companies

Self-registration is disabled. An administrator must invite each operator and separately grant companies. The API reads grants from private S3 on every request and fails closed for missing grants. The browser and API role cannot modify grants.

After deployment, an authorized administrator can run these examples with a real operator email:

```powershell
$poolId = terraform -chdir=infra/terraform output -raw web_user_pool_id
$caseBucket = terraform -chdir=infra/terraform output -raw case_bucket
$operatorEmail = 'operator@example.com' # replace before running
aws cognito-idp admin-create-user --user-pool-id $poolId --username $operatorEmail --user-attributes Name=email,Value=$operatorEmail Name=email_verified,Value=true
$operatorSub = aws cognito-idp admin-get-user --user-pool-id $poolId --username $operatorEmail --query "UserAttributes[?Name=='sub'].Value | [0]" --output text
@{ company_ids = @('CMP-DALISAY') } | ConvertTo-Json | Set-Content -Encoding utf8 build/web-access.json
aws s3 cp build/web-access.json "s3://$caseBucket/web-access/$operatorSub.json"
```

The operator completes the invitation/password flow directly in Cognito. Do not commit passwords or user grants. To revoke company access, replace the grant with a reduced or empty `company_ids` list. Existing access tokens expire within one hour; company revocation is evaluated on each API request. Records already displayed in a browser cannot be recalled. User A cannot read user B's cases even when both are assigned the same company; shared company work queues are not implemented.

## API contract

All routes require a Cognito access token with `enrollment/access` scope. API Gateway verifies the JWT signature, issuer, audience, lifetime, and scope; Lambda checks the trusted authorizer context and per-user/company permissions. There is no public Lambda Function URL for the API.

| Route | Behavior |
| --- | --- |
| `POST /api/uploads` | Accepts `company_id`, `filename`, and `size`; returns `job_id` and a five-minute, exact-key, exact-size S3 POST grant. |
| `POST /api/cases` | Accepts `job_id`; validates the uploaded version, preserves immutable workflow input, starts one execution, and returns 202. Repeating with the same id is safe. |
| `GET /api/cases?cursor=…` | Returns up to 20 own cases, assigned companies, and `next_cursor`. Counts apply to loaded pages, not the entire bucket. |
| `GET /api/cases/{job_id}` | Returns execution state, case snapshot when available, validation findings, member numbers, and events. |

Uploads allow CSV/XLSX up to 5 MB, with at most 500 members and a 128 KB parsed representation to keep the stateless simulator within workflow payload limits. Excel expansion is capped at 20 MB. Parsing also caps rows/columns before materializing a large worksheet. Input validation happens before execution; deterministic business-rule findings still go through the workflow to needs_review. Staged versions expire after one day; final inputs and case records remain governed by the case bucket's retention policy.

The frontend polls pending cases every ten seconds, provides refresh and pagination, and labels failed executions separately from validation review. Real timestamps and real acknowledgment waits replace the local fake clock. The reference is assigned when creating the cloud upload and carried through intake so an early refresh can find the same case. If a network retry occurs after a successful upload, submitting the same selected file reuses that job id; a page reload starts a new upload if the previous upload was never submitted.

Browser authentication uses state validation and S256 PKCE. Only the short-lived access token is stored in sessionStorage; ID/refresh tokens are discarded. Users sign in again on expiry. AWS credentials are never placed in the browser. Static S3 objects stay private and are readable through the specific CloudFront distribution; API responses and frontend assets are not cached.

## AWS smoke test after rollout

- Open `web_url`. Unsigned API requests must return 401. Sign in with the invited operator; verify only assigned companies appear.
- Upload clean and messy Dalisay samples. Clean input must become active only after insurer acknowledgment; messy input must reach needs_review without insurer submission. Refresh while the workflow is running and verify status survives page reload.
- Test another user, including one assigned the same company: a copied job URL must return 404. A forbidden company upload must return 403. Revoke the company grant and confirm subsequent reads are denied.
- Try missing, oversized, malformed, or incomplete uploads; verify no execution starts. Retry submission for an existing job and verify there is only one execution and the finalized input is unchanged.
- Confirm workflow failures appear as failed, events and member numbers load, pagination works, and unsigned insurer simulator calls return 403 while the workflow can invoke it.
- Inspect logs for request IDs/status only; do not enable full request-body logging. Confirm private case/frontend buckets cannot be read directly without authorization.

This remains a synthetic-data development environment. Before real HR use, define retention/deletion, production identity/MFA requirements, monitoring/alerting, and company collaboration semantics. Bedrock extraction remains a separate phase; rules and insurer acknowledgment continue to control activation.

## References

- [Cognito authorization and PKCE](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
- [API Gateway JWT authorization](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html)
- [S3 POST policy limits](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sigv4-HTTPPOSTConstructPolicy.html)
- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [Python Lambda packaging](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)
