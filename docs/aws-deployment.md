# AWS deployment preparation

## Current boundary

The browser interface (`python -m enrollment_desk.web`) runs only on loopback and uses local files, an in-process insurer, and a fixed demo clock. It is not a Lambda HTTP handler and must not be exposed as a production web server.

Existing Terraform provisions S3, workflow Lambda functions, the IAM-protected insurer simulator, and a Step Functions state machine. Applying it deploys the workflow backend only; it does not host the browser interface. No EC2 instance or Bedrock model is required for this phase.

## Prepare and validate from Windows

Run from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[aws,dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m enrollment_desk.cli evaluate data/generated
.\.venv\Scripts\python.exe scripts/build_lambda.py
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform validate
```

The builder reads dependencies from pyproject.toml and installs Linux x86_64 wheels for Python 3.12, including the AWS SDK. It creates `build/lambda.zip`, a deployment artifact ignored by Git, from a fresh staging directory. It neither deploys nor calls AWS APIs. Native Linux modules cannot be import-tested on Windows; CI builds the package on Linux, and an AWS smoke test is still needed.

## Workflow rollout, when approved

1. Select the AWS account/profile and region (Terraform defaults to ap-southeast-1), and confirm the caller with `aws sts get-caller-identity`.
2. Configure the intended Terraform state location; existing configuration uses local state. Keep state private and arrange shared state/locking before team deployment.
3. Build the package, initialize Terraform, and run `terraform -chdir=infra/terraform plan -out=../../build/workflow.tfplan`. Review the account, region, resource changes, IAM changes, and bucket deletion setting. Set `force_destroy_bucket=false` for persistent environments.
4. Apply only the reviewed plan after deployment approval. No apply is part of local preparation or CI.
5. Upload a synthetic sample to the case bucket under `incoming/` and start the state machine with `document_key` and `company_id`, as shown in README.md.
6. Confirm clean input reaches active only after acknowledgment. Confirm messy input reaches needs_review without submission. Check case snapshots and events in S3 and logs for execution failures. Verify an unsigned simulator URL request returns 403, and the workflow can invoke it using its scoped IAM role.

## Connect the web app in the next implementation phase

Recommended layout: static frontend on Amplify or private S3 behind CloudFront; Cognito sign-in; API Gateway with JWT authorization; Lambda API handlers; S3 case storage; the existing Step Functions workflow. No AWS credentials belong in browser code.

Implement these before calling the browser app AWS-ready:

- Add authenticated endpoints to create uploads, start an execution, list cases, and retrieve case details/events. Keep company access scoped to the signed-in user; the local custom header is not authentication.
- Use short-lived, company-scoped S3 upload URLs with size and file restrictions. Validate uploaded content on the server before processing.
- Start the existing asynchronous Step Functions workflow after upload. Return an execution identifier and poll for status; do not run the demo's synchronous fake-clock runner in a request handler.
- Map execution identifiers to cases so the UI can show queued/processing states before intake has issued a reference number. Persist storage in AWS, not Lambda local disk.
- Replace fixed demo labels/timestamps with actual execution state. Keep a separate explicit demo mode for synthetic local tests.
- Scope API permissions to the intended bucket prefixes, state machine, and case access. Configure frontend origins, HTTPS, logs without roster contents, and retention controls.
- Add Terraform for frontend hosting, identity, API routing, and API permissions. Test unauthenticated and cross-company access denial, upload failure, queued processing, insurer rejection, and refresh/poll behavior before rollout.

Bedrock document extraction is a separate phase. Deterministic validation and insurer acknowledgment continue to control case activation.

## References

- [AWS Python Lambda packaging](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html): bundle compatible dependencies and SDK versions.
- [Start Step Functions through an API](https://docs.aws.amazon.com/step-functions/latest/dg/tutorial-api-gateway.html): executions start asynchronously; retrieve status separately.
