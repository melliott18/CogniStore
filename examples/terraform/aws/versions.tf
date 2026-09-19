terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.28.0"
    }
  }

  # Bootstrap separately; see backend.hcl.example and docs/terraform.md.
  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = merge(var.tags, { Application = "CogniStore", Environment = var.name, ManagedBy = "Terraform" })
  }
}
