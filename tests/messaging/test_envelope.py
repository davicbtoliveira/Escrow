from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from django.test import SimpleTestCase

from escrow.messaging.envelope import MessageEnvelope


class MessageEnvelopeTests(SimpleTestCase):
    def test_versioned_envelope_round_trips_as_json(self) -> None:
        envelope = MessageEnvelope.build(
            message_id=UUID("89cc00ba-e41e-4b46-a37e-a3876a4c4981"),
            message_type="EvaluateFundingRisk.v1",
            version=1,
            occurred_at=datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
            correlation_id="correlation-001",
            causation_id="pix-callback-001",
            tenant_id="b90bcdb4-c082-4a6f-8e47-80b5f8a599d7",
            payload={"transfer_id": "2cb79e39-9e0b-420a-85d0-ca765f5272a1"},
        )

        restored = MessageEnvelope.from_json(envelope.to_json())

        assert restored == envelope
        assert json.loads(envelope.to_json()) == {
            "message_id": "89cc00ba-e41e-4b46-a37e-a3876a4c4981",
            "type": "EvaluateFundingRisk.v1",
            "version": 1,
            "occurred_at": "2026-07-18T12:30:00Z",
            "correlation_id": "correlation-001",
            "causation_id": "pix-callback-001",
            "tenant_id": "b90bcdb4-c082-4a6f-8e47-80b5f8a599d7",
            "payload": {"transfer_id": "2cb79e39-9e0b-420a-85d0-ca765f5272a1"},
        }
