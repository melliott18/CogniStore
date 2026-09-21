#!/usr/bin/env python3
"""Reproduce #156/#157 using synthetic identities and temporary real POSIX data.

Run this script in a fresh interpreter for each source tree. It imports that
tree's authenticated REST fixture; it never contacts a deployed API or IdP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread


def run(source: Path, expectation: str) -> dict:
    sys.path.insert(0, str(source))
    os.environ["COGNISTORE_SECURITY_PROFILE"] = "development"
    from fastapi.testclient import TestClient

    from cognistore.api.app import create_app
    from cognistore.api.gateway import CogniStoreGateway
    from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
    from cognistore.auth.tenancy import TenantResolver
    from cognistore.core.catalog import Catalog
    from cognistore.db import SQLCatalog
    from cognistore.drivers.posix_driver import PosixDriver
    from tests.unit.test_api_authentication import ISSUER, identity_provider

    fixture = identity_provider.__wrapped__()
    auth, token, _ = next(fixture)
    alice = {"Authorization": "Bearer " + token("alice")}
    bob = {"Authorization": "Bearer " + token("bob")}
    results: dict = {
        "expectation": expectation,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "storage": "real temporary POSIX filesystem; no casefold emulation",
        },
    }
    try:
        with TemporaryDirectory(prefix="cognistore-162-known-") as folder:
            probe = Path(folder) / "case-probe"
            probe.write_bytes(b"synthetic probe")
            case_insensitive = probe.with_name(probe.name.upper()).exists()
            results["environment"]["case_insensitive"] = case_insensitive
            if not case_insensitive:
                raise RuntimeError("#156 baseline reproduction requires a real case-insensitive volume")
            raw = PosixDriver(str(Path(folder) / "tenant-hot"))
            app = create_app(
                CogniStoreGateway(Catalog(), {"hot": raw}),
                authentication=auth,
                authorization=RBACAuthorizer(RBACPolicy({
                    (ISSUER, "alice"): ["admin"], (ISSUER, "bob"): ["writer"],
                })),
                tenancy=TenantResolver({
                    (ISSUER, "alice"): "victim", (ISSUER, "bob"): "default",
                }),
            )
            target = "/v1/objects/hot/bucket/private.txt"
            alias = (
                "/v1/objects/hot/bucket/.COGNISTORE-TENANTS/"
                + hashlib.sha256(b"victim").hexdigest() + "/private.txt"
            )
            with TestClient(app) as client:
                original = client.put(target, headers=alice, content=b"original")
                held = client.post("/v1/legal-holds", headers=alice, json={
                    "bucket": "bucket", "key": "private.txt", "reason": "synthetic audit hold",
                })
                denied = client.put(target, headers=alice, content=b"denied")
                alias_put = client.put(alias, headers=bob, content=b"replaced")
                observed = client.get(target, headers=alice)
                alias_delete = client.delete(alias, headers=bob)
                after = client.get(target, headers=alice)
                results["156"] = {
                    "initial_put": original.status_code, "hold": held.status_code,
                    "held_overwrite": denied.status_code, "alias_put": alias_put.status_code,
                    "read_after_alias_put": observed.status_code,
                    "read_bytes_after_alias_put": observed.content.decode(),
                    "alias_delete": alias_delete.status_code, "final_get": after.status_code,
                }
                assert (original.status_code, held.status_code, denied.status_code) == (201, 201, 409)
                if expectation == "vulnerable":
                    assert (alias_put.status_code, observed.content, alias_delete.status_code,
                            after.status_code) == (201, b"replaced", 204, 404)
                else:
                    assert (alias_put.status_code, observed.content, alias_delete.status_code,
                            after.status_code, after.content) == (422, b"original", 404, 200, b"original")

            removed, release = Event(), Event()

            class PausingDelete(PosixDriver):
                def delete_object_if_generation(self, bucket, key, generation):
                    result = super().delete_object_if_generation(bucket, key, generation)
                    removed.set()
                    if not release.wait(15):
                        raise RuntimeError("audit scheduler did not release DELETE")
                    return result

            raw = PausingDelete(str(Path(folder) / "race-hot"))
            with SQLCatalog(Path(folder) / "catalog.db") as catalog:
                app = create_app(
                    CogniStoreGateway(catalog, {"hot": raw}), authentication=auth,
                    authorization=RBACAuthorizer(RBACPolicy({(ISSUER, "alice"): ["writer"]})),
                )
                statuses: list[int] = []
                target = "/v1/objects/hot/bucket/item"
                with TestClient(app) as client:
                    assert client.put(target, headers=alice, content=b"initial").status_code == 201
                    deleting = Thread(target=lambda: statuses.append(
                        client.delete(target, headers=alice).status_code,
                    ))
                    deleting.start()
                    try:
                        assert removed.wait(15)
                        competing = client.put(target, headers=alice, content=b"replacement")
                    finally:
                        release.set()
                        deleting.join(15)
                    assert not deleting.is_alive()
                    observed = client.get(target, headers=alice)
                    record = catalog.get("bucket", "item")
                    results["157"] = {
                        "competing_put": competing.status_code, "delete": statuses,
                        "final_get_before_retry": observed.status_code,
                        "catalog_present_before_retry": record is not None,
                    }
                    if expectation == "vulnerable":
                        retained = raw.get_object("bucket", "item")
                        results["157"]["backend_bytes_before_retry"] = retained.decode()
                        assert (competing.status_code, statuses, observed.status_code,
                                record, retained) == (201, [204], 404, None, b"replacement")
                    else:
                        assert (competing.status_code, statuses, observed.status_code,
                                record) == (409, [204], 404, None)
                        retried = client.put(target, headers=alice, content=b"replacement")
                        after = client.get(target, headers=alice)
                        results["157"].update({
                            "retry_put": retried.status_code, "get_after_retry": after.status_code,
                            "backend_bytes_after_retry": raw.get_object("bucket", "item").decode(),
                        })
                        assert (retried.status_code, after.status_code, after.content) == (
                            201, 200, b"replacement",
                        )
        results["assertions"] = "passed"
        return results
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--expect", choices=("vulnerable", "fixed"), required=True)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.source_root.resolve(), arguments.expect), indent=2))
