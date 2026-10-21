terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name = var.project
  functions = {
    intake          = "enrollment_desk.workflow.handlers.intake"
    validate        = "enrollment_desk.workflow.handlers.validate"
    submit          = "enrollment_desk.workflow.handlers.submit"
    check_ack       = "enrollment_desk.workflow.handlers.check_ack"
    mark_active     = "enrollment_desk.workflow.handlers.mark_active"
    mark_for_review = "enrollment_desk.workflow.handlers.mark_for_review"
  }
}

# ---------- storage: documents, case snapshots, event log ----------

resource "aws_s3_bucket" "cases" {
  bucket        = "${local.name}-cases-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.force_destroy_bucket
}

resource "aws_s3_bucket_versioning" "cases" {
  bucket = aws_s3_bucket.cases.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cases" {
  bucket = aws_s3_bucket.cases.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # phase 3 moves to a customer-managed KMS key
    }
  }
}

resource "aws_s3_bucket_public_access_block" "cases" {
  bucket                  = aws_s3_bucket.cases.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "existing_members" {
  bucket       = aws_s3_bucket.cases.id
  key          = "reference/existing_members.json"
  source       = var.existing_members_file
  etag         = filemd5(var.existing_members_file)
  content_type = "application/json"
}

# ---------- insurer simulator (stateless Lambda behind a function URL) ----------

resource "aws_lambda_function" "insurer_sim" {
  function_name    = "${local.name}-insurer-sim"
  role             = aws_iam_role.insurer_sim.arn
  runtime          = "python3.12"
  handler          = "enrollment_desk.insurer_sim.http.lambda_handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 10
  memory_size      = 256
  environment {
    variables = {
      SIM_SEED              = "7"
      SIM_ACK_DELAY_SECONDS = "90"
      SIM_REJECT_RATE       = "0.08"
    }
  }
}

# IAM-protected URL: the workflow role receives scoped invocation permissions.
resource "aws_lambda_function_url" "insurer_sim" {
  function_name      = aws_lambda_function.insurer_sim.function_name
  authorization_type = "AWS_IAM"
}

# The workflow and simulator share an account, so the scoped identity policy in
# iam.tf is sufficient. Avoid a resource policy that grants direct invocation.

# ---------- workflow task functions ----------

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.name}-${replace(each.key, "_", "-")}"
  retention_in_days = 14
}

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${local.name}-${replace(each.key, "_", "-")}"
  role             = aws_iam_role.workflow_lambda.arn
  runtime          = "python3.12"
  handler          = each.value
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 30
  memory_size      = 512
  environment {
    variables = {
      CASE_BUCKET          = aws_s3_bucket.cases.id
      INSURER_URL          = aws_lambda_function_url.insurer_sim.function_url
      EXISTING_MEMBERS_KEY = aws_s3_object.existing_members.key
    }
  }
  depends_on = [aws_cloudwatch_log_group.fn]
}

# ---------- state machine ----------

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${local.name}-enrollment"
  retention_in_days = 14
}

resource "aws_sfn_state_machine" "enrollment" {
  name     = "${local.name}-enrollment"
  role_arn = aws_iam_role.sfn.arn
  definition = templatefile("${path.module}/state_machine.asl.json", {
    intake_arn          = aws_lambda_function.fn["intake"].arn
    validate_arn        = aws_lambda_function.fn["validate"].arn
    submit_arn          = aws_lambda_function.fn["submit"].arn
    check_ack_arn       = aws_lambda_function.fn["check_ack"].arn
    mark_active_arn     = aws_lambda_function.fn["mark_active"].arn
    mark_for_review_arn = aws_lambda_function.fn["mark_for_review"].arn
  })
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = false # keeps member data out of logs
    level                  = "ERROR"
  }
}
