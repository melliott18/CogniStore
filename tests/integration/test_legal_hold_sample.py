"""Sample ingestion uses the same preservation guard as production uploads."""

from pathlib import Path

import pytest

from cognistore.core.audit import AuditContext, AuditQuery
from cognistore.core.embedding_index import HnswIndexConfig
from cognistore.core.legal_holds import LegalHoldError
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.samples.content_search import ContentSearchRuntime, read_sample_corpus

pytestmark = pytest.mark.integration


def test_sample_reload_cannot_overwrite_held_bytes(tmp_path: Path, postgres_dsn: str) -> None:
    corpus = read_sample_corpus()
    item = corpus.manifest.objects[0]
    bucket = corpus.manifest.bucket
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm", "cold")}
    drivers[item.tier].put_object(bucket, item.key, b"held evidence")
    with SQLCatalog(postgres_dsn) as catalog:
        catalog.place_legal_hold(
            bucket, key=item.key, reason="preserve evidence",
            context=AuditContext("sample-hold", "user", "custodian"),
        )
        with ContentSearchRuntime(
            catalog, drivers, keyword_index_path=tmp_path / "keyword",
            hnsw=HnswIndexConfig(enabled=False),
        ) as runtime:
            with pytest.raises(LegalHoldError):
                runtime.load(corpus, overwrite=True)
        assert drivers[item.tier].get_object(bucket, item.key) == b"held evidence"
        events = catalog.list_audit_events(AuditQuery(event_types=frozenset({"legal_hold.denied"})))
        assert len(events) == 1
        assert events[0].details["operation"] == "sample.load"
