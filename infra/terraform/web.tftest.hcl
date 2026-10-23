# No AWS calls: both runs use Terraform's built-in mocked provider.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn                         = "arn:aws:s3:::mock-bucket"
      id                          = "mock-bucket"
      bucket_regional_domain_name = "mock-bucket.s3.ap-southeast-2.amazonaws.com"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-southeast-2:123456789012:log-group:mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = {
      arn        = "arn:aws:lambda:ap-southeast-2:123456789012:function:mock"
      invoke_arn = "arn:aws:apigateway:ap-southeast-2:lambda:path/2015-03-31/functions/arn:aws:lambda:ap-southeast-2:123456789012:function:mock/invocations"
    }
  }
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:ap-southeast-2:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_apigatewayv2_api" {
    defaults = {
      api_endpoint  = "https://example.execute-api.ap-southeast-2.amazonaws.com"
      execution_arn = "arn:aws:execute-api:ap-southeast-2:123456789012:example"
    }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = {
      id       = "ap-southeast-2_Mock123"
      endpoint = "cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_Mock123"
    }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = {
      arn         = "arn:aws:cloudfront::123456789012:distribution/MOCK"
      domain_name = "mock.cloudfront.net"
    }
  }
}

run "workflow_only_by_default" {
  command = plan
  variables {
    enable_web = false
  }
  assert {
    condition     = length(aws_lambda_function.web_api) == 0 && length(aws_cloudfront_distribution.web) == 0
    error_message = "Web resources must be opt-in."
  }
  assert {
    condition     = aws_lambda_function_url.insurer_sim.authorization_type == "AWS_IAM"
    error_message = "The insurer URL must remain IAM-protected."
  }
}

run "authenticated_web_stack" {
  command = plan
  variables {
    enable_web             = true
    enable_bedrock_mapping = false
  }
  assert {
    condition     = aws_lambda_function.web_api[0].environment[0].variables.BEDROCK_MAPPING_MODEL == ""
    error_message = "Manual-only deployment must not configure a Bedrock model."
  }
  assert {
    condition     = length(aws_apigatewayv2_route.web) == 7 && alltrue([for route in aws_apigatewayv2_route.web : route.authorization_type == "JWT" && contains(route.authorization_scopes, "enrollment/access")])
    error_message = "Every API route must require JWT enrollment scope."
  }
  assert {
    condition     = aws_cognito_user_pool_client.web[0].generate_secret == false && aws_cognito_user_pool_client.web[0].allowed_oauth_flows == toset(["code"])
    error_message = "The browser client must use authorization code flow without a secret."
  }
  assert {
    condition     = aws_cognito_user_pool.web[0].admin_create_user_config[0].allow_admin_create_user_only
    error_message = "Self-registration must be disabled."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.web[0].block_public_policy && aws_s3_bucket_public_access_block.web[0].restrict_public_buckets
    error_message = "Frontend storage must remain private."
  }
  assert {
    condition     = aws_lambda_function.web_api[0].handler == "enrollment_desk.cloud_api.lambda_handler" && aws_cloudfront_distribution.web[0].ordered_cache_behavior[0].path_pattern == "/api/*"
    error_message = "CloudFront API requests must reach the cloud Lambda handler."
  }
}

run "bedrock_mapping_opt_in" {
  command = plan
  variables {
    enable_web             = true
    enable_bedrock_mapping = true
  }
  assert {
    condition     = aws_lambda_function.web_api[0].environment[0].variables.BEDROCK_MAPPING_MODEL == "amazon.nova-lite-v1:0"
    error_message = "Bedrock must use the direct model configured for Sydney."
  }
}
