output "case_bucket" {
  value = aws_s3_bucket.cases.id
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.enrollment.arn
}

output "insurer_sim_url" {
  value = aws_lambda_function_url.insurer_sim.function_url
}
