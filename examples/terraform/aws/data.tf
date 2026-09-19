resource "aws_security_group" "catalog" {
  name_prefix = "${var.name}-catalog-"
  description = "Catalog reachable only from EKS nodes/pods"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "catalog" {
  security_group_id            = aws_security_group.catalog.id
  referenced_security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_subnet_group" "catalog" {
  name       = var.name
  subnet_ids = [for subnet in aws_subnet.database : subnet.id]
}

resource "aws_db_parameter_group" "catalog" {
  name_prefix = "${var.name}-"
  family      = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "catalog" {
  identifier                      = "${var.name}-catalog"
  engine                          = "postgres"
  engine_version                  = var.postgres_version
  instance_class                  = var.database_instance_class
  db_name                         = "cognistore"
  username                        = "cognistore_admin"
  manage_master_user_password     = true
  allocated_storage               = var.database_storage_gib
  max_allocated_storage           = 1000
  storage_type                    = "gp3"
  storage_encrypted               = true
  multi_az                        = true
  publicly_accessible             = false
  db_subnet_group_name            = aws_db_subnet_group.catalog.name
  vpc_security_group_ids          = [aws_security_group.catalog.id]
  parameter_group_name            = aws_db_parameter_group.catalog.name
  backup_retention_period         = 35
  backup_window                   = "03:00-04:00"
  maintenance_window              = "sun:05:00-sun:06:00"
  auto_minor_version_upgrade      = true
  allow_major_version_upgrade     = false
  apply_immediately               = false
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-catalog-final"
  delete_automated_backups        = false
  copy_tags_to_snapshot           = true
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_key" "objects" {
  description             = "${var.name} object storage; retain while any live/backup object needs decryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "objects" {
  name          = "alias/${var.name}-objects"
  target_key_id = aws_kms_key.objects.key_id
}

resource "aws_s3_bucket" "objects" {
  bucket        = "${var.name}-${var.account_id}-${var.region}-objects"
  force_destroy = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "objects" {
  bucket                  = aws_s3_bucket.objects.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "objects" {
  bucket = aws_s3_bucket.objects.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "objects" {
  bucket = aws_s3_bucket.objects.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.objects.arn
    }
  }
}

resource "aws_s3_bucket_policy" "objects" {
  bucket = aws_s3_bucket.objects.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*"
        Resource  = [aws_s3_bucket.objects.arn, "${aws_s3_bucket.objects.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid      = "DenyWrongEncryption", Effect = "Deny", Principal = "*", Action = "s3:PutObject"
        Resource = "${aws_s3_bucket.objects.arn}/*"
        Condition = {
          Null            = { "s3:x-amz-server-side-encryption" = "false" }
          StringNotEquals = { "s3:x-amz-server-side-encryption" = "aws:kms" }
        }
      },
      {
        Sid      = "DenyWrongKey", Effect = "Deny", Principal = "*", Action = "s3:PutObject"
        Resource = "${aws_s3_bucket.objects.arn}/*"
        Condition = {
          Null            = { "s3:x-amz-server-side-encryption-aws-kms-key-id" = "false" }
          StringNotEquals = { "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.objects.arn }
        }
      }
    ]
  })
}

# No secret version, password, random_password, or secret-value data source.
# An operator publishes runtime credentials through the separate secret workflow.
resource "aws_secretsmanager_secret" "runtime" {
  name                    = "${var.name}/runtime"
  description             = "Externally populated application database/NATS credentials; no values in Terraform"
  recovery_window_in_days = 30
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role" "workload" {
  name = "${var.name}-workload"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.cluster.arn }
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:${local.namespace}:cognistore"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "workload" {
  role = aws_iam_role.workload.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
        Resource = aws_s3_bucket.objects.arn
      },
      {
        # Conditional reads pin VersionId; deletes deliberately retain versions.
        Effect   = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${aws_s3_bucket.objects.arn}/*"
      },
      {
        Effect   = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.objects.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" }
          StringLike   = { "kms:EncryptionContext:aws:s3:arn" = [aws_s3_bucket.objects.arn, "${aws_s3_bucket.objects.arn}/*"] }
        }
      }
    ]
  })
}
