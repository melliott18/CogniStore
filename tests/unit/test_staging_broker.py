"""Keep the isolated broker profile compatible with the real queue/client contract."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from nats.aio.msg import Msg
from nats.js.api import StreamConfig
from nats.js.client import JetStreamContext
from nats.js.manager import JetStreamManager

from cognistore.jobs.nats_queue import NatsJetStreamConfig, NatsJetStreamQueue

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "release/staging"


def _yaml(name):
    return yaml.safe_load((STAGING / name).read_text())


def _matches(pattern, subject):
    expected, actual = pattern.split("."), subject.split(".")
    for index, token in enumerate(expected):
        if token == ">":
            return index < len(actual)
        if index >= len(actual) or token not in ("*", actual[index]):
            return False
    return len(expected) == len(actual)


def _app_allows(direction, subject):
    permissions = _yaml("nats-values.yaml")["config"]["merge"]["authorization"]["users"][0]["permissions"]
    return any(_matches(pattern, subject) for pattern in permissions[direction]["allow"])


def _queue_config():
    selected = _yaml("values.yaml")["queue"]
    return NatsJetStreamConfig(
        stream=selected["stream"], subject=selected["subject"], consumer=selected["consumer"],
        max_ack_pending=64,
    )


def test_staging_streams_satisfy_application_configuration():
    main = json.loads((STAGING / "jetstream-jobs.json").read_text())
    dead = json.loads((STAGING / "jetstream-dlq.json").read_text())
    queue = NatsJetStreamQueue(_queue_config())
    assert queue._main_stream_config_compatible(StreamConfig.from_response(dict(main)))

    class RetainedStream:
        async def stream_info(self, name):
            assert name == dead["name"]
            return SimpleNamespace(config=StreamConfig.from_response(dict(dead)))

    queue._jetstream = RetainedStream()
    asyncio.run(queue._ensure_dead_letter_stream())
    assert queue._dead_letter_ready
    assert main["num_replicas"] == dead["num_replicas"] == 1
    assert main["max_msgs"] == 10_000 and main["max_bytes"] == 1024**3
    assert dead["subjects"] == [f"{queue.config.resolved_dead_letter_subject}.>"]
    # DLQ limits are a runtime compatibility contract; the server/PVC bound disk.
    assert dead["max_age"] == 30 * 24 * 60 * 60 * 10**9


class SubjectReached(Exception):
    pass


class PermissionCheckedConnection:
    """Stop at the transport boundary after checking the real client's subject."""

    connected_server_version = SimpleNamespace(major=2, minor=14)

    async def request(self, subject, *args, **kwargs):
        assert _app_allows("publish", subject), subject
        raise SubjectReached(subject)

    async def publish(self, subject, *args, **kwargs):
        assert _app_allows("publish", subject), subject
        raise SubjectReached(subject)


@pytest.mark.parametrize("operation", [
    "account", "main-info", "dlq-info", "dlq-read", "consumer-info",
    "consumer-create", "enqueue", "dead-letter", "ack", "nak", "heartbeat",
])
def test_permissions_cover_real_application_and_nats_client_requests(operation):
    config = _queue_config()
    connection = PermissionCheckedConnection()
    manager = JetStreamManager(connection)
    context = JetStreamContext(connection)
    queue = NatsJetStreamQueue(config)
    queue._connection = connection
    queue._jetstream = context
    message = Msg(connection, reply=f"$JS.ACK.{config.stream}.{config.consumer}.1.1.1.1.0")
    requests = {
        "account": lambda: manager.account_info(),
        "main-info": lambda: manager.stream_info(config.stream),
        "dlq-info": lambda: manager.stream_info(config.resolved_dead_letter_stream),
        "dlq-read": lambda: manager.get_last_msg(config.resolved_dead_letter_stream, "fixture"),
        "consumer-info": lambda: manager.consumer_info(config.stream, config.consumer),
        "consumer-create": queue._ensure_consumer,
        "enqueue": lambda: context.publish(config.subject, b"synthetic"),
        "dead-letter": lambda: context.publish(f"{config.resolved_dead_letter_subject}.entry.fixture", b"synthetic"),
        "ack": message.ack_sync,
        "nak": message.nak,
        "heartbeat": message.in_progress,
    }
    with pytest.raises(SubjectReached):
        asyncio.run(requests[operation]())


def test_permissions_cover_pull_delivery_and_request_inboxes():
    config = _queue_config()
    assert _app_allows("publish", f"$JS.API.CONSUMER.MSG.NEXT.{config.stream}.{config.consumer}")
    assert _app_allows("subscribe", "_INBOX.synthetic.random")
    assert not _app_allows("subscribe", config.subject)


@pytest.mark.parametrize("subject", [
    "$JS.API.STREAM.CREATE.COGNISTORE_STAGING_JOBS",
    "$JS.API.STREAM.UPDATE.COGNISTORE_STAGING_JOBS",
    "$JS.API.STREAM.DELETE.COGNISTORE_STAGING_JOBS",
    "$JS.API.STREAM.PURGE.COGNISTORE_STAGING_JOBS_DLQ",
    "$JS.API.STREAM.MSG.DELETE.COGNISTORE_STAGING_JOBS_DLQ",
    "$JS.API.STREAM.MSG.GET.COGNISTORE_STAGING_JOBS",
    "$JS.API.CONSUMER.DELETE.COGNISTORE_STAGING_JOBS.cognistore-staging-workers",
    "$JS.API.CONSUMER.CREATE.COGNISTORE_STAGING_JOBS.other.cognistore.staging.jobs",
    "$JS.API.CONSUMER.CREATE.COGNISTORE_STAGING_JOBS.cognistore-staging-workers.other",
    "$JS.API.STREAM.INFO.COGNISTORE_PILOT_JOBS",
    "$JS.ACK.COGNISTORE_PILOT_JOBS.cognistore-pilot-workers.1.1.1.1.0",
    "cognistore.pilot.jobs", "unrelated", "_INBOX.other",
])
def test_application_cannot_administer_streams_or_address_other_campaigns(subject):
    assert not _app_allows("publish", subject)


def test_broker_capacity_and_private_network_contract():
    values = _yaml("nats-values.yaml")
    assert values["config"]["cluster"]["enabled"] is False
    store = values["config"]["jetstream"]["fileStore"]
    assert store["pvc"]["size"] == "20Gi" and store["maxSize"] == "12Gi"
    resources = list(yaml.safe_load_all((STAGING / "resources.yaml").read_text()))
    storage = next(item for item in resources if item["kind"] == "StorageClass")
    assert store["pvc"]["storageClassName"] == storage["metadata"]["name"]
    assert storage["parameters"]["encrypted"] == "true"
    assert values["config"]["nats"]["tls"]["merge"]["handshake_first"] is True
    assert values["config"]["monitor"]["tls"]["enabled"] is True
    assert values["service"]["merge"]["spec"]["type"] == "ClusterIP"
    policy = values["extraResources"][0]["spec"]
    monitor = next(rule for rule in policy["ingress"] if rule["ports"][0]["port"] == 8222)
    assert len(monitor["from"]) == 1
    assert set(monitor["from"][0]) == {"namespaceSelector", "podSelector"}
    assert values["container"]["resources"]["requests"] == {"cpu": "2", "memory": "4Gi"}


def test_pinned_chart_render_preserves_tls_persistence_and_single_replica():
    helm = os.environ.get("HELM") or shutil.which("helm")
    chart = os.environ.get("COGNISTORE_NATS_CHART")
    if not helm or not chart:
        pytest.skip("set HELM and COGNISTORE_NATS_CHART to the local nats-2.14.6 chart archive")
    result = subprocess.run([
        helm, "template", "cognistore-staging-nats", chart,
        "--namespace", "cognistore-staging", "-f", str(STAGING / "nats-values.yaml"),
    ], check=True, capture_output=True, text=True)
    rendered = list(yaml.safe_load_all(result.stdout))
    stateful = next(item for item in rendered if item["kind"] == "StatefulSet")
    assert stateful["metadata"]["labels"]["helm.sh/chart"] == "nats-2.14.6"
    assert stateful["spec"]["replicas"] == 1
    pod = stateful["spec"]["template"]["spec"]
    container = pod["containers"][0]
    mounts = {mount["mountPath"] for mount in container["volumeMounts"]}
    assert {"/data", "/etc/nats-config", "/etc/nats-certs/nats", "/etc/nats-ca-cert", "/tmp"} <= mounts
    assert len(pod["containers"]) == 1
    assert pod["automountServiceAccountToken"] is False
    assert all(container[name]["httpGet"]["scheme"] == "HTTPS"
               for name in ("startupProbe", "readinessProbe", "livenessProbe"))
    claims = stateful["spec"]["volumeClaimTemplates"]
    assert len(claims) == 1 and claims[0]["spec"]["resources"]["requests"]["storage"] == "20Gi"
    config = next(item for item in rendered if item["kind"] == "ConfigMap")["data"]["nats.conf"]
    assert '"handshake_first": true' in config and '"max_file_store": 12Gi' in config
    assert '"https_port": 8222' in config and '"cluster"' not in config
