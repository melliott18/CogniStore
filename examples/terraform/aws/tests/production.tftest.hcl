# Every run is a plan using Terraform's AWS mock. No credentials or AWS APIs.
mock_provider "aws" {
  override_during = plan

  mock_resource "aws_vpc" { override_during = plan }
  mock_resource "aws_subnet" { override_during = plan }
  mock_resource "aws_eip" { override_during = plan }
  mock_resource "aws_nat_gateway" { override_during = plan }
  mock_resource "aws_route_table" { override_during = plan }

  mock_resource "aws_eks_cluster" {
    defaults = {
      arn      = "arn:aws:eks:us-east-1:111122223333:cluster/cognistore-test"
      endpoint = "https://example.invalid"
      certificate_authority = [{
        data = "bW9jay1jZXJ0aWZpY2F0ZQ=="
      }]
      identity = [{
        oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE" }]
      }]
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::111122223333:role/cognistore-test"
    }
  }

  mock_resource "aws_iam_openid_connect_provider" {
    defaults = {
      arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000072"
      key_id = "00000000-0000-0000-0000-000000000072"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::cognistore-test-objects"
    }
  }

  mock_resource "aws_db_instance" {
    defaults = {
      address  = "catalog.example.invalid"
      endpoint = "catalog.example.invalid:5432"
      port     = 5432
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:111122223333:secret:mock-rds-secret"
        secret_status = "active"
        kms_key_id    = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000072"
      }]
    }
  }

  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:mock-runtime"
    }
  }

  mock_resource "aws_launch_template" {
    defaults = {
      id             = "lt-0123456789abcdef0"
      latest_version = 1
    }
  }

  # Distinct role ARNs ensure identity-wiring assertions detect the wrong role.
  override_resource {
    target = aws_iam_role.workload
    values = { arn = "arn:aws:iam::111122223333:role/cognistore-test-workload" }
  }
  override_resource {
    target = aws_iam_role.addon["ebs"]
    values = { arn = "arn:aws:iam::111122223333:role/cognistore-test-ebs" }
  }
  override_resource {
    target = aws_iam_role.addon["cni"]
    values = { arn = "arn:aws:iam::111122223333:role/cognistore-test-cni" }
  }
}

variables {
  account_id             = "111122223333"
  region                 = "us-east-1"
  name                   = "cognistore-test"
  availability_zones     = ["us-east-1a", "us-east-1b", "us-east-1c"]
  operator_cidrs         = ["10.200.0.0/24"]
  cluster_admin_role_arn = "arn:aws:iam::111122223333:role/cognistore-operators"
}

run "secure_production_topology" {
  command = plan

  assert {
    condition     = length(aws_subnet.public) == 3 && length(aws_subnet.private) == 3 && length(aws_subnet.database) == 3 && length(aws_nat_gateway.main) == 3
    error_message = "Production must span three AZs with separate public, workload and database subnets and a NAT gateway per AZ."
  }

  assert {
    condition     = alltrue([for subnet in concat(values(aws_subnet.public), values(aws_subnet.private), values(aws_subnet.database)) : !subnet.map_public_ip_on_launch])
    error_message = "No subnet may assign public IPs automatically."
  }

  assert {
    condition     = alltrue([for az in var.availability_zones : aws_route.nat[az].nat_gateway_id == aws_nat_gateway.main[az].id && aws_route_table_association.private[az].route_table_id == aws_route_table.private[az].id && aws_nat_gateway.main[az].subnet_id == aws_subnet.public[az].id])
    error_message = "Each workload subnet must use the NAT gateway in its own availability zone."
  }

  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].endpoint_private_access && !aws_eks_cluster.main.vpc_config[0].endpoint_public_access && toset(aws_eks_node_group.main.subnet_ids) == toset([for subnet in aws_subnet.private : subnet.id])
    error_message = "The Kubernetes API and every worker node must stay private."
  }

  assert {
    condition     = toset(aws_eks_cluster.main.enabled_cluster_log_types) == toset(["api", "audit", "authenticator", "controllerManager", "scheduler"])
    error_message = "EKS must enable the full control-plane audit log set."
  }

  assert {
    condition     = aws_eks_node_group.main.scaling_config[0].min_size >= 3 && aws_launch_template.nodes.metadata_options[0].http_tokens == "required" && aws_launch_template.nodes.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "Keep at least three nodes and prevent pods using the node role through IMDS."
  }

  assert {
    condition     = aws_db_instance.catalog.multi_az && !aws_db_instance.catalog.publicly_accessible && aws_db_instance.catalog.storage_encrypted && aws_db_instance.catalog.manage_master_user_password
    error_message = "The catalog must be private, Multi-AZ and encrypted with an RDS-managed administrator password."
  }

  assert {
    condition     = aws_db_instance.catalog.backup_retention_period >= 7 && aws_db_instance.catalog.deletion_protection && !aws_db_instance.catalog.skip_final_snapshot
    error_message = "Catalog backups, deletion protection and the final snapshot are required."
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.catalog.from_port == 5432 && aws_vpc_security_group_ingress_rule.catalog.to_port == 5432 && aws_vpc_security_group_ingress_rule.catalog.referenced_security_group_id == aws_eks_cluster.main.vpc_config[0].cluster_security_group_id && toset(aws_db_subnet_group.catalog.subnet_ids) == toset([for subnet in aws_subnet.database : subnet.id])
    error_message = "Catalog access must be limited to EKS nodes/pods on 5432 in isolated database subnets."
  }

  assert {
    condition     = anytrue([for parameter in aws_db_parameter_group.catalog.parameter : parameter.name == "rds.force_ssl" && parameter.value == "1"])
    error_message = "RDS must reject database connections without TLS."
  }

  assert {
    condition     = aws_s3_bucket_versioning.objects.versioning_configuration[0].status == "Enabled" && !aws_s3_bucket.objects.force_destroy && aws_s3_bucket_public_access_block.objects.block_public_acls && aws_s3_bucket_public_access_block.objects.block_public_policy && aws_s3_bucket_public_access_block.objects.ignore_public_acls && aws_s3_bucket_public_access_block.objects.restrict_public_buckets
    error_message = "Object storage must be versioned, block public access and refuse forced deletion of object data."
  }

  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.objects.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms" && one(aws_s3_bucket_server_side_encryption_configuration.objects.rule).apply_server_side_encryption_by_default[0].kms_master_key_id == aws_kms_key.objects.arn && aws_kms_key.objects.enable_key_rotation && aws_kms_key.objects.deletion_window_in_days == 30
    error_message = "The object bucket must default to its rotating KMS key with a 30-day deletion window."
  }

  assert {
    condition     = anytrue([for statement in jsondecode(aws_s3_bucket_policy.objects.policy).Statement : statement.Sid == "DenyInsecureTransport" && statement.Effect == "Deny" && try(statement.Condition.Bool["aws:SecureTransport"], "") == "false"])
    error_message = "The object bucket must reject unencrypted transport."
  }

  assert {
    condition     = jsondecode(aws_iam_role.workload.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub"] == "system:serviceaccount:cognistore:cognistore" && jsondecode(aws_iam_role.workload.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud"] == "sts.amazonaws.com"
    error_message = "Only the named CogniStore service account may assume the workload role."
  }

  assert {
    condition     = alltrue([for statement in jsondecode(aws_iam_role_policy.workload.policy).Statement : contains([aws_s3_bucket.objects.arn, "${aws_s3_bucket.objects.arn}/*", aws_kms_key.objects.arn], statement.Resource) && alltrue([for action in statement.Action : contains(["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads", "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "kms:Decrypt", "kms:GenerateDataKey"], action)])]) && contains(flatten([for statement in jsondecode(aws_iam_role_policy.workload.policy).Statement : statement.Action]), "s3:GetObjectVersion")
    error_message = "Workload permissions must remain limited to object operations on this bucket and key; no wildcard resources or secret-reading access."
  }

  assert {
    condition     = jsondecode(aws_eks_addon.vpc_cni.configuration_values).enableNetworkPolicy == "true" && aws_eks_addon.ebs_csi.service_account_role_arn == aws_iam_role.addon["ebs"].arn && aws_eks_addon.vpc_cni.service_account_role_arn == aws_iam_role.addon["cni"].arn
    error_message = "The cluster must enforce NetworkPolicy and give the EBS CSI driver its dedicated workload identity."
  }

  assert {
    condition     = yamldecode(output.helm_values).securityProfile == "production" && yamldecode(output.helm_values).networkPolicy.enabled && yamldecode(output.helm_values).api.autoscaling.enabled && yamldecode(output.helm_values).api.replicas >= 2 && yamldecode(output.helm_values).worker.replicas >= 2
    error_message = "Generated Helm values must preserve production security, NetworkPolicy, API autoscaling and multiple replicas."
  }

  assert {
    condition     = yamldecode(output.helm_values).serviceAccount.annotations["eks.amazonaws.com/role-arn"] == aws_iam_role.workload.arn && yamldecode(output.helm_values).extraVolumes[0].projected.sources[0].serviceAccountToken.audience == "sts.amazonaws.com"
    error_message = "The chart must receive the workload IAM role and a projected STS token, without static AWS keys."
  }

  assert {
    condition     = yamldecode(output.drivers_yaml).tiers.object.server_side_encryption == "aws:kms" && !yamldecode(output.drivers_yaml).tiers.object.auto_create_bucket && yamldecode(output.drivers_yaml).tiers.object.kms_key_id == aws_kms_key.objects.arn
    error_message = "The runtime driver must use the Terraform KMS key and cannot create arbitrary buckets."
  }

  assert {
    condition     = yamldecode(output.storage_class_yaml).provisioner == "ebs.csi.aws.com" && yamldecode(output.storage_class_yaml).parameters.encrypted == "true" && yamldecode(output.storage_class_yaml).volumeBindingMode == "WaitForFirstConsumer" && yamldecode(output.storage_class_yaml).reclaimPolicy == "Retain"
    error_message = "Persistent workloads need encrypted, zonally scheduled EBS volumes retained after claim deletion."
  }

  assert {
    condition     = yamldecode(output.migration_network_policy_yaml).metadata.namespace == "cognistore" && yamldecode(output.migration_network_policy_yaml).spec.podSelector.matchLabels["app.kubernetes.io/component"] == "migration" && yamldecode(output.migration_network_policy_yaml).spec.egress[0].ports[0].port == 5432 && toset([for destination in yamldecode(output.migration_network_policy_yaml).spec.egress[0].to : destination.ipBlock.cidr]) == toset([for subnet in aws_subnet.database : subnet.cidr_block])
    error_message = "The first-install migration policy must allow the migration job to reach every catalog subnet on 5432."
  }
}

run "reject_single_zone" {
  command = plan
  variables {
    availability_zones = ["us-east-1a"]
  }
  expect_failures = [var.availability_zones]
}

run "reject_public_operator_access" {
  command = plan
  variables {
    operator_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.operator_cidrs]
}

run "reject_undersized_node_pool" {
  command = plan
  variables {
    node_capacity = { min = 1, desired = 1, max = 3 }
  }
  expect_failures = [var.node_capacity]
}

run "reject_cross_account_admin" {
  command = plan
  variables {
    cluster_admin_role_arn = "arn:aws:iam::999900001111:role/operator"
  }
  expect_failures = [var.cluster_admin_role_arn]
}

run "reject_unsupported_postgres_family" {
  command = plan
  variables {
    postgres_version = "16.9"
  }
  expect_failures = [var.postgres_version]
}
