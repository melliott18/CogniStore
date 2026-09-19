# AWS production reference

This Terraform root provisions one three-AZ VPC, private EKS workers, a private
Multi-AZ PostgreSQL catalog, a versioned KMS-encrypted S3 bucket, workload IAM,
and secret containers. It emits the nonsecret Helm handoff; it does not install
CogniStore or publish credentials.

Follow the [complete provisioning and operations guide](../../../docs/terraform.md)
for prerequisites, inputs/outputs, state, NATS, certificates, production
configuration, cost, backup, recovery, and deliberate decommissioning.

From the repository root, validate without cloud credentials or resources:

```bash
./scripts/terraform/verify.sh
```

The pinned AWS provider is recorded in `.terraform.lock.hcl`. Terraform state,
backend files, environment inputs, plans, and generated handoff files belong
outside Git. Do not run `apply` with the illustrative identities in
`terraform.tfvars.example`.
