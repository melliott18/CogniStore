#!/usr/bin/env bash
# Create and destroy a dedicated cluster; never operate on the user's context.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
for dependency in docker kind kubectl helm python3; do
  command -v "$dependency" >/dev/null || { echo "Missing prerequisite: $dependency" >&2; exit 1; }
done
cluster="${COGNISTORE_KUBE_CLUSTER:-cognistore-acceptance-$$}"
output="${COGNISTORE_KUBE_OUTPUT:-$root/test-results/kubernetes}"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
# A private kubeconfig ensures every subprocess uses this fresh cluster only.
export KUBECONFIG="$output/kubeconfig"
namespace=cognistore-acceptance
created=false
forward_pid=""
base_url=""
cleanup() {
  status=$?
  trap - EXIT
  if [[ -n "$forward_pid" ]]; then kill "$forward_pid" 2>/dev/null || true; fi
  if [[ "$created" == true ]]; then
    kubectl -n "$namespace" get pods,deployments,hpa,scaledobjects,pvc -o wide >"$output/resources.txt" 2>&1 || true
    kubectl -n "$namespace" get events --sort-by=.lastTimestamp >"$output/events.txt" 2>&1 || true
    kubectl -n "$namespace" describe hpa >"$output/hpa.txt" 2>&1 || true
    kubectl -n "$namespace" logs -l app.kubernetes.io/instance=acceptance --all-containers --tail=1000 --prefix >"$output/application.log" 2>&1 || true
    if [[ "${COGNISTORE_KUBE_KEEP_CLUSTER:-0}" != 1 ]]; then
      kind delete cluster --name "$cluster"
      rm -f "$KUBECONFIG"
    else
      echo "Retained cluster $cluster; kubeconfig: $KUBECONFIG"
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
if kind get clusters | python3 -c 'import sys; raise SystemExit(sys.argv[1] not in sys.stdin.read().splitlines())' "$cluster"; then
  echo "Refusing to reuse existing cluster $cluster; choose a new COGNISTORE_KUBE_CLUSTER" >&2
  exit 1
fi
kind create cluster --name "$cluster" --image kindest/node:v1.34.0 --wait 120s
created=true
if [[ "${COGNISTORE_KUBE_SKIP_BUILD:-0}" != 1 ]]; then
  docker build --target runtime --tag cognistore:helm-acceptance .
fi
kind load docker-image --name "$cluster" cognistore:helm-acceptance
docker build --target minio --tag cognistore-minio:qualification .
kind load docker-image --name "$cluster" cognistore-minio:qualification
kubectl create namespace "$namespace"
python3 scripts/kubernetes/fixtures.py | kubectl -n "$namespace" apply -f -
kubectl -n "$namespace" apply -f scripts/kubernetes/prometheus.yaml
for dependency in postgres nats minio acceptance-prometheus; do
  kubectl -n "$namespace" rollout status "deployment/$dependency" --timeout=300s
done
helm repo add kedacore https://kedacore.github.io/charts --force-update
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo update kedacore prometheus-community
helm install keda kedacore/keda --version 2.18.3 --namespace keda --create-namespace --wait --timeout 5m
helm install acceptance-adapter prometheus-community/prometheus-adapter --version 5.3.0 \
  --namespace "$namespace" --values scripts/kubernetes/adapter-values.yaml --wait --timeout 5m
helm lint helm/cognistore --strict --values scripts/kubernetes/values.yaml
helm install acceptance helm/cognistore --namespace "$namespace" \
  --values scripts/kubernetes/values.yaml --wait --timeout 5m
helm test acceptance --namespace "$namespace" --logs --timeout 3m

forward() {
  if [[ -n "$forward_pid" ]]; then
    kill "$forward_pid" 2>/dev/null || true
    wait "$forward_pid" 2>/dev/null || true
  fi
  base_url=""
  # Service proxy follows ready endpoints through HPA scale-down. A normal
  # port-forward pins one pod and disconnects if that pod is selected for removal.
  kubectl proxy --address=127.0.0.1 --port=0 >"$output/service-proxy.log" 2>&1 &
  forward_pid=$!
  base_url="$(python3 - "$forward_pid" "$output/service-proxy.log" "$namespace" <<'PY'
import os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

pid = int(sys.argv[1])
log = Path(sys.argv[2])
for _ in range(60):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise SystemExit('kubectl service proxy exited: ' + log.read_text()) from None
    match = re.search(r'^Starting to serve on 127\.0\.0\.1:(\d+)$', log.read_text(), re.M)
    if match is None:
        time.sleep(1)
        continue
    base = ('http://127.0.0.1:' + match.group(1) + '/api/v1/namespaces/'
            + sys.argv[3] + '/services/http:acceptance-cognistore-api:8080/proxy')
    try:
        with urllib.request.urlopen(base + '/readyz', timeout=2) as response:
            assert response.status == 200
        os.kill(pid, 0)
        print(base)
        break
    except (OSError, urllib.error.URLError):
        time.sleep(1)
else:
    raise SystemExit('API did not become ready through service proxy: ' + log.read_text())
PY
  )"
  kill -0 "$forward_pid"
}
exercise=(python3 scripts/kubernetes/exercise.py)
forward
"${exercise[@]}" security --base-url "$base_url"
"${exercise[@]}" seed --base-url "$base_url" --output "$output/invariants.json"

# Roll a new configuration revision, running the idempotent migration hook.
helm upgrade acceptance helm/cognistore --namespace "$namespace" \
  --values scripts/kubernetes/values.yaml --set-string config.revision=acceptance-upgrade \
  --wait --timeout 5m
forward
"${exercise[@]}" verify --base-url "$base_url" --output "$output/invariants.json"
helm rollback acceptance 1 --namespace "$namespace" --wait --timeout 5m
forward
"${exercise[@]}" verify --base-url "$base_url" --output "$output/invariants.json"

# Restart persistent dependencies independently of the chart to verify that the
# catalog, JetStream state, and object bytes are on their PVCs.
for dependency in postgres nats minio; do
  kubectl -n "$namespace" rollout restart "deployment/$dependency"
  kubectl -n "$namespace" rollout status "deployment/$dependency" --timeout=180s
done
forward
"${exercise[@]}" verify --base-url "$base_url" --output "$output/invariants.json" --recover-backends
"${exercise[@]}" autoscale --base-url "$base_url" --output "$output/autoscaling.json" --invariants "$output/invariants.json"
"${exercise[@]}" security --base-url "$base_url"
helm history acceptance --namespace "$namespace" -o json >"$output/helm-history.json"
echo "Kubernetes acceptance passed. Evidence: $output"
