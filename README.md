# HMO Enrollment Desk

Turns employee rosters and endorsement requests from HR into validated, insurer-ready records. Rules decide what is clear, a person reviews what isn't, and coverage is marked active only when the insurer acknowledges it. Every step is logged.

It handles the enrollment and endorsement workflow an HMO brokerage runs for its client companies in the Philippines. **All data in this repository is synthetic.** Companies, people and plans are invented, and no real personal or health information is used.

## The problem it targets

Coverage starts on an employee's first day, but a new hire can't get a Letter of Authorization (LOA) until the HMO has loaded their record and issued a member number. Until then they are covered on paper and stuck in practice: the HMO app shows nothing, and they call the company's HR nurse to chase it.

So the project tracks one headline number: **days from hire date to first possible LOA**. Every case also gets a reference number at intake, on day one, that the member and the HR nurse can quote.

## What phase 1 does

```
Roster uploaded -> Rules validate -> All clear? --no--> Needs review
                                         | yes
                                  Send to insurer (retries) -> Acknowledged? --no--> Needs review
                                                                    | yes
                                                             Coverage active
```

- **Roster parsing** (`roster_parser.py`) reads CSV and Excel files that look like real HR exports: title rows, odd headers ("Emp #", "Bday", "Name (Last, First)"), mixed date formats, all-caps names and extra columns. Headers it can't map are set aside for the AI column-mapping step in phase 2. Rows are always parsed in code.
- **Validation rules** (`rules.py`) cover missing fields, unreadable or implausible dates, duplicates, unknown plans or plans outside the client's contract, dependents over the age limit or not allowed on the plan, dependents whose principal doesn't exist, people already enrolled, and late endorsements.
- **Workflow** (`workflow/`) is the same steps as an AWS Step Functions state machine, with a local runner that mirrors it.
- **Insurer simulator** (`insurer_sim/`) is a stateless stand-in for an HMO's intake. Some calls fail, acknowledgments arrive late, some submissions are rejected, and member numbers are issued a few business days after submission.
- **Synthetic data with labels** (`synth.py`): 100 requests, about half with real problems, each labeled with the correct values and the exact flags a reviewer should see. These labels are the start of the evaluation suite.
- **Event log**: every case records what happened, when, and with which rules version.

No AI is in the loop yet; that's phase 2. The baseline it has to beat (or match on clean files) is below.

## Run it locally

Needs Python 3.12+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                                          # unit and workflow tests
enrollment-desk evaluate data/generated            # parser + rules vs the 100 labeled requests

enrollment-desk run data/samples/clean_roster.csv --company CMP-DALISAY
enrollment-desk run data/samples/messy_roster.csv --company CMP-DALISAY
enrollment-desk run-all data/generated             # all 100 through the workflow
enrollment-desk pending                            # hired members who can't get an LOA yet
```

A clean roster:

```
END-20261015-B425EA  CMP-DALISAY  clean_roster.csv
  path:   Intake -> Validate -> SubmitToInsurer -> WaitForAck -> CheckAck -> WaitForAck -> CheckAck -> MarkActive
  status: active
  DLS-30001: hired 2026-10-01, member number on 2026-10-21 -> 20 day(s) to first possible LOA
```

A messy one stops before the insurer:

```
  path:   Intake -> Validate -> NeedsReview
  [block] row 5: unparseable_date - Could not read birth date '1990-13-01'
  [info] row 1: late_endorsement - Hired 58 days ago: covered since day one but not yet usable at clinics
  [block] row 2: dependent_over_age - Child is 25; Gold limit is 23
  [block] row 4: duplicate_in_file - This person appears earlier in the same file
```

Phase 1 baseline on the 100 labeled requests: field accuracy 100%, flag recall 100%, flag precision 100%, routing accuracy 100%. That is expected, because these files only use patterns the parser was written for. Phase 2 adds scanned PDFs, emails and unseen header names, where the deterministic parser can't cope and AI extraction is measured against the same labels. Across all 100 cases, median hire-to-LOA readiness is 12 days (p90: 31) under the simulator's settings.

To run the simulator as a real HTTP service and point the workflow at it:

```bash
enrollment-desk simulator --port 8081 &
enrollment-desk --insurer-url http://localhost:8081 run data/samples/clean_roster.csv --company CMP-DALISAY
```

With a real HTTP simulator the acknowledgment delay is real (90 seconds by default; set `SIM_ACK_DELAY_SECONDS=0` for quick runs).

## Deploy to AWS

```bash
./scripts/build_lambda.sh                          # build/lambda.zip
cd infra/terraform
terraform init && terraform apply                  # S3, Lambda, Step Functions, IAM in ap-southeast-1

BUCKET=$(terraform output -raw case_bucket)
SFN=$(terraform output -raw state_machine_arn)
aws s3 cp ../../data/samples/clean_roster.csv s3://$BUCKET/incoming/clean_roster.csv
aws stepfunctions start-execution --state-machine-arn $SFN \
  --input '{"document_key": "incoming/clean_roster.csv", "company_id": "CMP-DALISAY"}'
```

Everything is pay-per-use (Lambda, Step Functions, S3), so an idle deployment costs close to nothing. `terraform destroy` removes it all, bucket included.

The simulator Function URL uses `AWS_IAM`. The HTTP client signs Lambda URL requests with SigV4 using the AWS credential chain (including temporary session tokens) and the region in the URL. Local HTTP simulator calls remain unsigned. For local calls to an AWS URL, install `pip install -e '.[aws]'` and configure AWS credentials. The deployment builder bundles boto3/botocore with their dependencies. The workflow role receives both `lambda:InvokeFunctionUrl` (AWS_IAM only) and `lambda:InvokeFunction` (Function URL only), scoped to this simulator ARN. Both resources share an account, so no additional resource-based invocation policy is needed. A 403 should be investigated as a credentials or IAM configuration issue; do not make the URL public.

## Layout

```
src/enrollment_desk/
  models.py            cases, members, flags, events
  reference.py         synthetic plans and client companies
  roster_parser.py     CSV/Excel parsing and header mapping
  rules.py             validation rules (versioned)
  store.py             local and S3 storage for case snapshots and the event log
  insurer_client.py    in-process and HTTP clients
  insurer_sim/         simulator core, Lambda handler, local server
  workflow/            steps, Lambda handlers, local runner
  synth.py             synthetic requests and labels
  evaluate.py          scoring against labels
infra/terraform/       AWS resources and the state machine definition
data/generated/        100 labeled requests + existing members
data/samples/          a clean and a messy example roster
```

## Roadmap

1. **Synthetic data and workflow skeleton** (this phase)
2. AI extraction on Amazon Bedrock for PDFs, emails and unknown headers, with source quotes, Guardrails, Prompt Management and a CI evaluation gate
3. Operations console (React, TanStack Query, shadcn/ui): case list, document beside extracted fields, approve or fix, task-token handoff back to the workflow; Aurora PostgreSQL for case state
4. Review assistant (RAG over plan documents, LangGraph with read-only MCP tools) and full decision traces with replay
5. Quality dashboard and model comparison

## Notes

- Plans, dependent age limits and companies are invented for development and testing and are not any insurer's actual terms.
- The AI will never change a case's status. Only the workflow can, and coverage goes active only on an insurer acknowledgment.

## Local web interface (Windows PowerShell)

From the project folder, run:

```powershell
.\.venv\Scripts\python.exe -m enrollment_desk.web
```

Open http://127.0.0.1:8080 in your browser. Select a company, upload a CSV or XLSX roster (up to 5 MB), and inspect validation findings, member numbers, and the event timeline. Downloadable sample rosters are available on the page; choose Dalisay Software Labs for those samples. Search and status filters help navigate saved cases.

This is a local simulation using the same workflow rules as the CLI, with a fixed demo date and instantly advanced waits. It uses the in-process insurer simulator, requires no AWS credentials, and stores cases, events, and uploads in `.local/store`. Stop with Ctrl+C. Use `--port 8082` or `--store .local/web-demo` for an alternate port or isolated records. The server binds only to 127.0.0.1 and is intended for local development with synthetic data.

See [AWS deployment preparation](docs/aws-deployment.md) for the Windows packaging commands, workflow validation steps, and remaining web hosting work.
