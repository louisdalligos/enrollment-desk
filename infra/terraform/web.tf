# Optional web stack. Existing workflow-only deployments remain unchanged until enabled.
variable "enable_web" {
  description = "Provision Cognito, the authenticated HTTP API, and CloudFront frontend."
  type        = bool
  default     = false
}

locals {
  web_enabled = var.enable_web ? 1 : 0
  web_origin  = var.enable_web ? "https://${aws_cloudfront_distribution.web[0].domain_name}" : ""
}

resource "aws_s3_bucket" "web" {
  count         = local.web_enabled
  bucket        = "${local.name}-web-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "web" {
  count                   = local.web_enabled
  bucket                  = aws_s3_bucket.web[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  count  = local.web_enabled
  bucket = aws_s3_bucket.web[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "web" {
  count                             = local.web_enabled
  name                              = "${local.name}-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "disabled" {
  count = local.web_enabled
  name  = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "api" {
  count = local.web_enabled
  name  = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_response_headers_policy" "web" {
  count = local.web_enabled
  name  = "${local.name}-web-security"
  security_headers_config {
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_security_policy {
      # Inline styles are retained for the existing UI; all scripts are local files.
      content_security_policy = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' https://${aws_cognito_user_pool_domain.web[0].domain}.auth.${var.region}.amazoncognito.com https://${aws_s3_bucket.cases.bucket_regional_domain_name}; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
      override                = true
    }
  }
}

resource "aws_cloudfront_distribution" "web" {
  comment = "Enrollment Desk frontend and authenticated API"
  tags = {
    Name = "${local.name}-web"
  }
  count               = local.web_enabled
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_200"
  origin {
    origin_id                = "static"
    domain_name              = aws_s3_bucket.web[0].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web[0].id
  }
  origin {
    origin_id   = "api"
    domain_name = replace(aws_apigatewayv2_api.web[0].api_endpoint, "https://", "")
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }
  default_cache_behavior {
    target_origin_id           = "static"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled[0].id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.web[0].id
    compress                   = true
  }
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "api"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled[0].id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.api[0].id
  }
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

data "aws_iam_policy_document" "web_bucket" {
  count = local.web_enabled
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.web[0].arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.web[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "web" {
  count  = local.web_enabled
  bucket = aws_s3_bucket.web[0].id
  policy = data.aws_iam_policy_document.web_bucket[0].json
}

resource "aws_cognito_user_pool" "web" {
  count                    = local.web_enabled
  name                     = "${local.name}-operators"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }
}

resource "aws_cognito_user_pool_domain" "web" {
  count        = local.web_enabled
  domain       = "${local.name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.web[0].id
}

resource "aws_cognito_resource_server" "web" {
  count        = local.web_enabled
  identifier   = "enrollment"
  name         = "Enrollment API"
  user_pool_id = aws_cognito_user_pool.web[0].id
  scope {
    scope_name        = "access"
    scope_description = "Access assigned enrollment companies and own cases"
  }
}

resource "aws_cognito_user_pool_client" "web" {
  count                                = local.web_enabled
  name                                 = "${local.name}-browser"
  user_pool_id                         = aws_cognito_user_pool.web[0].id
  generate_secret                      = false
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "${aws_cognito_resource_server.web[0].identifier}/access"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.web_origin}/"]
  logout_urls                          = ["${local.web_origin}/"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_apigatewayv2_api" "web" {
  count         = local.web_enabled
  name          = "${local.name}-web"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_authorizer" "web" {
  count            = local.web_enabled
  api_id           = aws_apigatewayv2_api.web[0].id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web[0].id]
    issuer   = "https://${aws_cognito_user_pool.web[0].endpoint}"
  }
}

resource "aws_apigatewayv2_integration" "web" {
  count                  = local.web_enabled
  api_id                 = aws_apigatewayv2_api.web[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.web_api[0].invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

resource "aws_apigatewayv2_route" "web" {
  for_each             = var.enable_web ? toset(["GET /api/cases", "GET /api/cases/{job_id}", "POST /api/uploads", "POST /api/cases"]) : toset([])
  api_id               = aws_apigatewayv2_api.web[0].id
  route_key            = each.key
  target               = "integrations/${aws_apigatewayv2_integration.web[0].id}"
  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.web[0].id
  authorization_scopes = ["enrollment/access"]
}

resource "aws_cloudwatch_log_group" "web_api_access" {
  count             = local.web_enabled
  name              = "/aws/apigateway/${local.name}-web"
  retention_in_days = 14
}

resource "aws_apigatewayv2_stage" "web" {
  count       = local.web_enabled
  api_id      = aws_apigatewayv2_api.web[0].id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.web_api_access[0].arn
    format = jsonencode({
      requestId = "$context.requestId", status = "$context.status", route = "$context.routeKey"
    })
  }
}

resource "aws_iam_role" "web_api" {
  count              = local.web_enabled
  name               = "${local.name}-web-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "web_api" {
  count = local.web_enabled
  statement {
    sid       = "ReadCasesAndAssignments"
    actions   = ["s3:GetObject"]
    resources = [for prefix in ["web-access", "web/jobs", "incoming/web", "cases", "events"] : "${aws_s3_bucket.cases.arn}/${prefix}/*"]
  }
  statement {
    sid       = "ReadUploadVersions"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.cases.arn}/uploads/web/*"]
  }
  statement {
    sid       = "WriteUploadsAndJobs"
    actions   = ["s3:PutObject"]
    resources = [for prefix in ["uploads/web", "incoming/web", "web/jobs"] : "${aws_s3_bucket.cases.arn}/${prefix}/*"]
  }
  statement {
    sid       = "ListJobsAndEvents"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.cases.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["web/jobs/*", "events/*", "cases/*", "web-access/*", "incoming/web/*", "uploads/web/*"]
    }
  }
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.enrollment.arn]
  }
  statement {
    actions   = ["states:DescribeExecution"]
    resources = ["${replace(aws_sfn_state_machine.enrollment.arn, ":stateMachine:", ":execution:")}:*"]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.web_api[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "web_api" {
  count  = local.web_enabled
  role   = aws_iam_role.web_api[0].id
  policy = data.aws_iam_policy_document.web_api[0].json
}

resource "aws_cloudwatch_log_group" "web_api" {
  count             = local.web_enabled
  name              = "/aws/lambda/${local.name}-web-api"
  retention_in_days = 14
}

resource "aws_lambda_function" "web_api" {
  count            = local.web_enabled
  function_name    = "${local.name}-web-api"
  role             = aws_iam_role.web_api[0].arn
  runtime          = "python3.12"
  handler          = "enrollment_desk.cloud_api.lambda_handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 29
  memory_size      = 1024
  environment {
    variables = {
      CASE_BUCKET       = aws_s3_bucket.cases.id
      STATE_MACHINE_ARN = aws_sfn_state_machine.enrollment.arn
    }
  }
  depends_on = [aws_iam_role_policy.web_api, aws_s3_bucket_versioning.cases]
}

resource "aws_lambda_permission" "web_api" {
  count         = local.web_enabled
  statement_id  = "AllowWebApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.web_api[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.web[0].execution_arn}/*/*/api/*"
}

resource "aws_s3_bucket_cors_configuration" "web_uploads" {
  count  = local.web_enabled
  bucket = aws_s3_bucket.cases.id
  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["POST"]
    allowed_origins = [local.web_origin]
    max_age_seconds = 300
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "web_uploads" {
  count  = local.web_enabled
  bucket = aws_s3_bucket.cases.id
  rule {
    id     = "ExpireStagedWebUploads"
    status = "Enabled"
    filter {
      prefix = "uploads/web/"
    }
    expiration {
      days = 1
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_object" "web_assets" {
  for_each = var.enable_web ? {
    "index.html"               = { path = "web.html", type = "text/html; charset=utf-8" }
    "web.js"                   = { path = "web.js", type = "text/javascript; charset=utf-8" }
    "auth.js"                  = { path = "auth.js", type = "text/javascript; charset=utf-8" }
    "samples/clean_roster.csv" = { path = "../../data/samples/clean_roster.csv", type = "text/csv" }
    "samples/messy_roster.csv" = { path = "../../data/samples/messy_roster.csv", type = "text/csv" }
  } : {}
  bucket        = aws_s3_bucket.web[0].id
  key           = each.key
  source        = startswith(each.key, "samples/") ? "${path.module}/${each.value.path}" : "${path.module}/../../src/enrollment_desk/${each.value.path}"
  source_hash   = filemd5(startswith(each.key, "samples/") ? "${path.module}/${each.value.path}" : "${path.module}/../../src/enrollment_desk/${each.value.path}")
  content_type  = each.value.type
  cache_control = "no-store"
}

resource "aws_s3_object" "web_config" {
  count         = local.web_enabled
  bucket        = aws_s3_bucket.web[0].id
  key           = "config.json"
  content_type  = "application/json"
  cache_control = "no-store"
  content = jsonencode({
    mode     = "aws"
    domain   = "https://${aws_cognito_user_pool_domain.web[0].domain}.auth.${var.region}.amazoncognito.com"
    clientId = aws_cognito_user_pool_client.web[0].id
  })
}

output "web_url" {
  value = var.enable_web ? local.web_origin : null
}

output "web_user_pool_id" {
  value = var.enable_web ? aws_cognito_user_pool.web[0].id : null
}
