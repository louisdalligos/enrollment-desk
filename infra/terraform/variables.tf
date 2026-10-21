variable "region" {
  description = "Deployment region. This AWS account permits Sydney only."
  type        = string
  default     = "ap-southeast-2"
  validation {
    condition     = var.region == "ap-southeast-2"
    error_message = "This project must use Sydney (ap-southeast-2), the account's permitted region."
  }
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
  description = "Allow terraform destroy to delete the bucket with its contents. Convenient for development environments; set false for anything long-lived."
  type        = bool
  default     = true
}
