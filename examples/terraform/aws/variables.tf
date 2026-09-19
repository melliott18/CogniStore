variable "account_id" {
  description = "Expected commercial AWS account ID; guards against applying to another account."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a twelve-digit AWS account ID."
  }
}

variable "region" {
  description = "Commercial AWS region containing all three availability zones."
  type        = string
}

variable "name" {
  description = "Unique environment prefix; use a distinct root/state key per environment."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}[a-z0-9]$", var.name))
    error_message = "name must be 4–26 lowercase letters, digits or hyphens, starting with a letter and ending with a letter/digit."
  }
}

variable "availability_zones" {
  description = "Three distinct standard availability zones in region, in stable order."
  type        = list(string)
  validation {
    condition     = length(var.availability_zones) == 3 && length(distinct(var.availability_zones)) == 3 && alltrue([for az in var.availability_zones : can(regex("^${var.region}[a-z]$", az))])
    error_message = "Supply three distinct standard availability zones in the selected region."
  }
}

variable "vpc_cidr" {
  description = "Nonoverlapping IPv4 /16; /20 node and /24 public/database subnets are derived deterministically."
  type        = string
  default     = "10.72.0.0/16"
  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && endswith(var.vpc_cidr, "/16")
    error_message = "vpc_cidr must be an IPv4 /16."
  }
}

variable "operator_cidrs" {
  description = "Private routed VPN/runner IPv4 CIDRs permitted to reach the private Kubernetes API on 443. Routes/VPN are managed separately."
  type        = set(string)
  validation {
    condition     = length(var.operator_cidrs) > 0 && alltrue([for cidr in var.operator_cidrs : can(cidrnetmask(cidr)) && try(tonumber(split("/", cidr)[1]) >= 16, false)])
    error_message = "Provide at least one IPv4 operator CIDR with prefix /16 or narrower."
  }
}

variable "cluster_admin_role_arn" {
  description = "Existing IAM role used by operators; granted EKS cluster admin. Use a dedicated, audited role."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/.+$", var.cluster_admin_role_arn))
    error_message = "cluster_admin_role_arn must be an IAM role in account_id (not an STS session ARN)."
  }
}

variable "kubernetes_version" {
  description = "EKS minor version; verify regional support and maintenance dates before applying."
  type        = string
  default     = "1.35"
}

variable "addon_versions" {
  description = "Optional exact compatible EKS add-on builds. Null selects the EKS default at creation; record/pin resolved builds for release reproduction."
  type = object({
    vpc_cni    = optional(string)
    coredns    = optional(string)
    kube_proxy = optional(string)
    ebs_csi    = optional(string)
  })
  default = {}
}

variable "node_instance_type" {
  description = "x86_64 instance type supported by the AL2023 node image; size against pod limits and NATS capacity."
  type        = string
  default     = "m7i.large"
}

variable "node_capacity" {
  description = "Managed node group capacity; this example does not install a node autoscaler."
  type        = object({ min = number, desired = number, max = number })
  default     = { min = 3, desired = 3, max = 6 }
  validation {
    condition     = var.node_capacity.min >= 3 && var.node_capacity.min <= var.node_capacity.desired && var.node_capacity.desired <= var.node_capacity.max && alltrue([for n in values(var.node_capacity) : n == floor(n)])
    error_message = "Integer node capacities must satisfy 3 <= min <= desired <= max."
  }
}

variable "node_release_version" {
  description = "Optional EKS AL2023 AMI release pin; null selects the current release at creation. Record the resolved release before qualification."
  type        = string
  default     = null
}

variable "postgres_version" {
  description = "RDS PostgreSQL 17 minor version with pgvector >=0.8.0; verify regional availability."
  type        = string
  default     = "17.11"
  validation {
    condition     = can(regex("^17\\.[0-9]+", var.postgres_version))
    error_message = "This reference uses PostgreSQL 17 and its postgres17 parameter family."
  }
}

variable "database_instance_class" {
  description = "RDS PostgreSQL instance class; tune against catalog size and connection demand."
  type        = string
  default     = "db.m7g.large"
}

variable "database_storage_gib" {
  description = "Initial encrypted gp3 catalog capacity. Shrinking requires restore/migration."
  type        = number
  default     = 100
  validation {
    condition     = var.database_storage_gib >= 20 && var.database_storage_gib <= 900 && var.database_storage_gib == floor(var.database_storage_gib)
    error_message = "database_storage_gib must be an integer from 20 through 900, leaving at least ten percent headroom below the 1000 GiB autoscaling ceiling."
  }
}

variable "tags" {
  description = "Additional nonsecret ownership/cost tags; core application/environment tags take precedence."
  type        = map(string)
  default     = {}
}
