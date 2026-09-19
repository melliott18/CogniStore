resource "aws_iam_role" "cluster" {
  name = "${var.name}-cluster"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "eks.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 90
}

resource "aws_eks_cluster" "main" {
  name                          = var.name
  role_arn                      = aws_iam_role.cluster.arn
  version                       = var.kubernetes_version
  bootstrap_self_managed_addons = false
  enabled_cluster_log_types     = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  vpc_config {
    subnet_ids              = [for subnet in aws_subnet.private : subnet.id]
    endpoint_private_access = true
    endpoint_public_access  = false
  }

  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}

resource "aws_vpc_security_group_ingress_rule" "operator" {
  for_each          = var.operator_cidrs
  security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  description       = "Private routed operator access"
  cidr_ipv4         = each.key
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_eks_access_entry" "operator" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.cluster_admin_role_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "operator" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.operator.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}

resource "aws_iam_role" "nodes" {
  name = "${var.name}-nodes"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "nodes" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly"])
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

resource "aws_launch_template" "nodes" {
  name_prefix = "${var.name}-"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      encrypted             = true
      volume_type           = "gp3"
      volume_size           = 80
      delete_on_termination = true
    }
  }
}

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.name}-general"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = [for subnet in aws_subnet.private : subnet.id]
  instance_types  = [var.node_instance_type]
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  version         = var.kubernetes_version
  release_version = var.node_release_version

  launch_template {
    id      = aws_launch_template.nodes.id
    version = tostring(aws_launch_template.nodes.latest_version)
  }
  scaling_config {
    min_size     = var.node_capacity.min
    desired_size = var.node_capacity.desired
    max_size     = var.node_capacity.max
  }
  update_config {
    max_unavailable = 1
  }
  # The network plug-in is installed before nodes join; DNS/CSI wait for nodes.
  depends_on = [aws_iam_role_policy_attachment.nodes, aws_eks_addon.vpc_cni, aws_route.nat, aws_route_table_association.private]
}

# IAM retrieves the CA thumbprint for this AWS-hosted issuer. No TLS provider
# or workstation connection to the cluster's private API is needed.
resource "aws_iam_openid_connect_provider" "cluster" {
  url            = aws_eks_cluster.main.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  oidc_host = replace(aws_iam_openid_connect_provider.cluster.url, "https://", "")
  addon_service_accounts = {
    cni = "system:serviceaccount:kube-system:aws-node"
    ebs = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
  }
}

resource "aws_iam_role" "addon" {
  for_each = local.addon_service_accounts
  name     = "${var.name}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.cluster.arn }
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = each.value
      } }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "addon" {
  for_each   = { cni = "AmazonEKS_CNI_Policy", ebs = "service-role/AmazonEBSCSIDriverPolicy" }
  role       = aws_iam_role.addon[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "vpc-cni"
  addon_version            = var.addon_versions.vpc_cni
  service_account_role_arn = aws_iam_role.addon["cni"].arn
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
  })
  depends_on = [aws_iam_role_policy_attachment.addon]
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name  = aws_eks_cluster.main.name
  addon_name    = "kube-proxy"
  addon_version = var.addon_versions.kube_proxy
}

resource "aws_eks_addon" "coredns" {
  cluster_name  = aws_eks_cluster.main.name
  addon_name    = "coredns"
  addon_version = var.addon_versions.coredns
  depends_on    = [aws_eks_node_group.main]
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "aws-ebs-csi-driver"
  addon_version            = var.addon_versions.ebs_csi
  service_account_role_arn = aws_iam_role.addon["ebs"].arn
  depends_on               = [aws_eks_node_group.main, aws_iam_role_policy_attachment.addon]
}
