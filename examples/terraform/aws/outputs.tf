locals {
  handoff = {
    aws_region            = var.region
    namespace             = local.namespace
    workload_role_arn     = aws_iam_role.workload.arn
    kms_key_arn           = aws_kms_key.objects.arn
    database_subnet_cidrs = [for subnet in aws_subnet.database : subnet.cidr_block]
  }
}

output "region" {
  description = "AWS region for CLI and SDK configuration."
  value       = var.region
}

output "cluster_name" {
  description = "Private EKS cluster; use a routed operator host and the configured admin IAM role."
  value       = aws_eks_cluster.main.name
}

output "vpc_id" {
  description = "VPC to connect to the separately managed operator network."
  value       = aws_vpc.main.id
}

output "node_subnet_ids" {
  description = "Private subnet IDs for operator-managed internal ingress dependencies."
  value       = [for subnet in aws_subnet.private : subnet.id]
}

output "database_subnet_cidrs" {
  description = "Database destinations permitted by the generated application NetworkPolicy."
  value       = local.handoff.database_subnet_cidrs
}

output "database" {
  description = "Catalog coordinates and managed admin secret ARN only; no passwords. Create a separate application role outside Terraform."
  value = {
    host             = aws_db_instance.catalog.address
    port             = aws_db_instance.catalog.port
    name             = aws_db_instance.catalog.db_name
    admin_secret_arn = aws_db_instance.catalog.master_user_secret[0].secret_arn
  }
}

output "runtime_secret_arn" {
  description = "Empty runtime secret container. Populate/synchronize using an operator identity; workload role cannot read the admin secret."
  value       = aws_secretsmanager_secret.runtime.arn
}

output "object_storage" {
  description = "Bucket and encryption key identifiers; pass the bucket to CogniStore object operations."
  value = {
    bucket      = aws_s3_bucket.objects.id
    kms_key_arn = aws_kms_key.objects.arn
  }
}

output "workload_role_arn" {
  description = "Storage role restricted to system:serviceaccount:cognistore:cognistore."
  value       = aws_iam_role.workload.arn
}

output "helm_values" {
  description = "Production Helm starting values; replace operator placeholders and review allowed egress before use."
  value       = templatefile("${path.module}/helm/values.yaml.tftpl", local.handoff)
}

output "drivers_yaml" {
  description = "Native AWS S3 workload-identity driver configuration for the configuration Secret."
  value       = templatefile("${path.module}/helm/drivers.yaml.tftpl", local.handoff)
}

output "storage_class_yaml" {
  description = "Encrypted gp3 StorageClass with Retain reclaim policy; apply separately after EBS CSI is ready."
  value       = templatefile("${path.module}/helm/storage-class.yaml.tftpl", local.handoff)
}

output "migration_network_policy_yaml" {
  description = "Install before Helm so migration hooks can reach DNS and PostgreSQL on first install."
  value       = templatefile("${path.module}/helm/migration-network-policy.yaml.tftpl", local.handoff)
}

output "resolved_versions" {
  description = "Record the resolved image/add-on versions with qualification evidence and pin them in the next environment's inputs."
  value = {
    kubernetes   = aws_eks_cluster.main.version
    node_release = aws_eks_node_group.main.release_version
    postgres     = aws_db_instance.catalog.engine_version_actual
    addons = {
      vpc_cni    = aws_eks_addon.vpc_cni.addon_version
      coredns    = aws_eks_addon.coredns.addon_version
      kube_proxy = aws_eks_addon.kube_proxy.addon_version
      ebs_csi    = aws_eks_addon.ebs_csi.addon_version
    }
  }
}
