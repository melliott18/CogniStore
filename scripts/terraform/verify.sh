#!/usr/bin/env bash
# Validate in an isolated directory, with a mocked provider and no AWS credentials.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for dependency in terraform helm python3; do
  command -v "$dependency" >/dev/null || { echo "Missing prerequisite: $dependency" >&2; exit 1; }
done
output="${COGNISTORE_TERRAFORM_OUTPUT:-$root/test-results/terraform}"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/cognistore-terraform.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT

# Exclude local state, backend configuration, auto-loaded variable files and
# credentials. The example's checked-in templates and tests are the only inputs.
python3 - "$root/examples/terraform/aws" "$scratch" <<'PY'
import shutil
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
for path in source.rglob("*"):
    relative = path.relative_to(source)
    if ".terraform" in relative.parts or not path.is_file():
        continue
    if path.name == "override.tf" or path.name.endswith("_override.tf"):
        continue
    if path.name != ".terraform.lock.hcl" and not path.name.endswith(
        (".tf", ".tftpl", ".tftest.hcl", ".tfmock.hcl")
    ):
        continue
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
PY

export TF_IN_AUTOMATION=1 TF_INPUT=0 CHECKPOINT_DISABLE=1
export AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE
unset AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI
unset AWS_CONTAINER_AUTHORIZATION_TOKEN AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE
# Caller CLI arguments must not redirect this isolated verification to a backend.
unset TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_validate TF_CLI_ARGS_test TF_DATA_DIR

terraform -chdir="$scratch" fmt -check -recursive
terraform fmt -check - < "$root/examples/terraform/aws/terraform.tfvars.example"
terraform fmt -check - < "$root/examples/terraform/aws/backend.hcl.example"
terraform -chdir="$scratch" init -backend=false -input=false -lockfile=readonly -no-color \
  | tee "$output/init.log"
terraform -chdir="$scratch" validate -no-color | tee "$output/validate.log"
test_status=0
terraform -chdir="$scratch" test -filter=tests/production.tftest.hcl -json -verbose \
  -junit-xml="$output/terraform.xml" > "$output/terraform-test.json" || test_status=$?

python3 - "$output" "$test_status" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
events = [json.loads(line) for line in (output / "terraform-test.json").read_text().splitlines()]
passed = {
    event["test_run"]["run"]
    for event in events
    if event["type"] == "test_run" and event["test_run"].get("status") == "pass"
}
for event in events:
    # Expected variable-validation failures are evidence for successful negative tests.
    if event["type"] == "diagnostic" and event.get("@testrun") in passed:
        continue
    if event["type"] in {"diagnostic", "test_summary"}:
        print(event["@message"])
        location = event.get("diagnostic", {}).get("range")
        if location:
            print(f"  {location['filename']}:{location['start']['line']}")
        detail = event.get("diagnostic", {}).get("detail")
        if detail:
            print(detail)
if int(sys.argv[2]):
    raise SystemExit(int(sys.argv[2]))
plans = [
    event["test_plan"]
    for event in events
    if event["type"] == "test_plan" and event.get("@testrun") == "secure_production_topology"
]
if len(plans) != 1:
    raise SystemExit("Expected one mocked production topology plan")
changes = plans[0]["output_changes"]
for name, filename in (
    ("helm_values", "helm-values.yaml"),
    ("drivers_yaml", "drivers.yaml"),
    ("storage_class_yaml", "storage-class.yaml"),
    ("migration_network_policy_yaml", "migration-network-policy.yaml"),
):
    change = changes[name]
    if change.get("after_unknown") or not isinstance(change.get("after"), str):
        raise SystemExit(f"The mocked plan did not resolve {name}")
    (output / filename).write_text(change["after"])
PY

# The handoff deliberately leaves the operator's tested image digest unset.
# Supply an explicitly synthetic image only for rendering; no image is pulled.
image_fixture=(--set-string image.repository=example.invalid/cognistore \
  --set-string image.digest=sha256:0000000000000000000000000000000000000000000000000000000000000000)
helm lint "$root/helm/cognistore" --strict --values "$output/helm-values.yaml" "${image_fixture[@]}" \
  | tee "$output/helm-lint.log"
helm template cognistore "$root/helm/cognistore" --namespace cognistore \
  --values "$output/helm-values.yaml" "${image_fixture[@]}" > "$output/helm-rendered.yaml"
echo "Terraform and production Helm verification passed. Evidence: $output"
