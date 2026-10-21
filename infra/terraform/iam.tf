data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

# Workflow tasks: read uploads and reference data, write case snapshots and events. Nothing else.
resource "aws_iam_role" "workflow_lambda" {
  name               = "${local.name}-workflow-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "workflow_lambda" {
  statement {
    sid       = "ReadInputs"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.cases.arn}/incoming/*", "${aws_s3_bucket.cases.arn}/reference/*", "${aws_s3_bucket.cases.arn}/cases/*", "${aws_s3_bucket.cases.arn}/events/*"]
  }
  statement {
    sid       = "WriteCaseState"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cases.arn}/cases/*", "${aws_s3_bucket.cases.arn}/events/*"]
  }
  statement {
    sid       = "ListCaseState"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.cases.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["cases/*", "events/*"]
    }
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name}-*:*"]
  }
}

resource "aws_iam_role_policy" "workflow_lambda" {
  role   = aws_iam_role.workflow_lambda.id
  policy = data.aws_iam_policy_document.workflow_lambda.json
}

resource "aws_iam_role" "insurer_sim" {
  name               = "${local.name}-insurer-sim"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "insurer_sim_logs" {
  role       = aws_iam_role.insurer_sim.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# State machine: invoke its own task functions and write its log group.
resource "aws_iam_role" "sfn" {
  name               = "${local.name}-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = flatten([for f in aws_lambda_function.fn : [f.arn, "${f.arn}:*"]])
  }
  statement {
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"] # these log-delivery actions don't support resource-level permissions
  }
}

resource "aws_iam_role_policy" "sfn" {
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}
