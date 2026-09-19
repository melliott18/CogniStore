"""Typed audit access, bounded exports, checkpoints, and transport errors."""

import httpx
import pytest

from cognistore.core.catalog import Catalog
from cognistore.sdk import (
    AuditCheckpointResource,
    AuditEventPage,
    AuditEventResource,
    AuditExportResponse,
    AuditVerificationRequest,
    AuditVerificationResponse,
    CogniStoreClient,
    PermissionDeniedError,
    ResponseContractError,
)
from tests.unit.test_api_audit import client_for, seed
from tests.unit.test_api_authentication import identity_provider as identity_provider


def test_sdk_audit_authenticated_round_trip_and_export_pagination(identity_provider):
    catalog = Catalog()
    events = seed(catalog, "alpha", count=3)
    with client_for(catalog, identity_provider) as http_client:
        with CogniStoreClient("http://testserver", http_client=http_client) as sdk:
            page = sdk.list_audit_events(event_type="policy.decision", limit=2)
            assert isinstance(page, AuditEventPage)
            assert len(page.items) == 2 and page.page.next_cursor is not None
            remaining = sdk.list_audit_events(event_type="policy.decision", limit=2,
                                             cursor=page.page.next_cursor)
            assert len(remaining.items) == 1 and remaining.page.next_cursor is None
            event = sdk.get_audit_event(events[0].event_id)
            assert isinstance(event, AuditEventResource)
            assert event.details == events[0].details
            export = sdk.export_audit_events(limit=2)
            assert isinstance(export, AuditExportResponse)
            checkpoint = export.checkpoint
            assert isinstance(checkpoint, AuditCheckpointResource)
            count = len(export.records)
            while not export.complete:
                export = sdk.export_audit_events(limit=2, cursor=export.page.next_cursor)
                assert export.checkpoint == checkpoint
                count += len(export.records)
                assert count <= checkpoint.sequence
            assert count == checkpoint.sequence and export.page.next_cursor is None
            verified = sdk.verify_audit_integrity(AuditVerificationRequest(checkpoint=checkpoint))
            assert isinstance(verified, AuditVerificationResponse)
            assert verified.valid and verified.anchored
            assert sdk.verify_audit_integrity().valid
    with client_for(catalog, identity_provider, role="reader") as http_client:
        with CogniStoreClient("http://testserver", http_client=http_client) as sdk:
            with pytest.raises(PermissionDeniedError):
                sdk.export_audit_events()


def test_sdk_audit_quotes_event_id_and_validates_response():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"event_id": "not-an-event"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        with CogniStoreClient("http://testserver", http_client=http_client) as sdk:
            with pytest.raises(ResponseContractError):
                sdk.get_audit_event("../reserved ?#")
    assert requests[0].url.raw_path == b"/v1/audit/events/..%2Freserved%20%3F%23"
