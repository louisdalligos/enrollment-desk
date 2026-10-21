variable "region" {
  description = "AWS region. Singapore is the closest to the Philippines."
  type        = string
  default     = "ap-southeast-1"
}

variable "project" {
  description = "Prefix for resource names."
  type        = string
  default     = "enrollment-desk"
}

variable "lambda_zip" {
  description = "Deployment package built by scripts/build_lambda.sh."
  type        = string
  default     = "../../build/lambda.zip"
}

variable "existing_members_file" {
  description = "Synthetic list of members already enrolled, used by the already_enrolled rule."
  type        = string
  default     = "../../data/generated/existing_members.json"
}

variable "force_destroy_bucket" {
  description = "Allow terraform destroy to delete the bucket with its contents. Demo convenience only."
  type        = bool
  default     = true
}
