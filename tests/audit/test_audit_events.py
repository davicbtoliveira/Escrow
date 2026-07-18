from __future__ import annotations

from django.db.models.deletion import ProtectedError
from django.test import TestCase

from escrow.audit.models import AuditEvent
from escrow.audit.services import record_audit_event
from escrow.organizations.models import Organization


class AuditEventTests(TestCase):
    def test_audit_event_is_attributable_and_blocks_parent_deletion(self) -> None:
        organization = Organization.objects.create(name="Auditável")
        event = record_audit_event(
            event_type="sandbox_callback_received",
            organization=organization,
            payload={"provider_event_id": "evt_safe_reference"},
            correlation_id="correlation-test-0001",
        )

        assert event.organization_id == organization.id
        assert event.payload == {"provider_event_id": "evt_safe_reference"}
        assert AuditEvent.objects.count() == 1
        with self.assertRaises(ProtectedError):
            organization.delete()
