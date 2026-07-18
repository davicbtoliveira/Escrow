from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TestCase, override_settings

from escrow.messaging.envelope import MessageEnvelope

AGREEMENT_ID = UUID("2cb79e39-9e0b-420a-85d0-ca765f5272a1")


def status_envelope(*, payload: dict[str, object] | None = None) -> MessageEnvelope:
    return MessageEnvelope.build(
        message_id=UUID("89cc00ba-e41e-4b46-a37e-a3876a4c4981"),
        message_type="AgreementStatusChanged.v1",
        version=1,
        occurred_at=datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
        correlation_id="correlation-001",
        causation_id="funding-001",
        tenant_id="b90bcdb4-c082-4a6f-8e47-80b5f8a599d7",
        payload=payload
        or {
            "agreement_id": str(AGREEMENT_ID),
            "status": "HELD",
            "sequence": 3,
        },
    )


class RecordingChannelLayer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def group_send(self, group: str, message: dict[str, object]) -> None:
        self.sent.append((group, message))


class RealtimeTaskTests(TestCase):
    def test_status_task_emits_only_the_safe_snapshot_to_the_agreement_group(self) -> None:
        from escrow.notifications.tasks import publish_realtime

        channel_layer = RecordingChannelLayer()
        with patch(
            "escrow.notifications.tasks.get_channel_layer",
            return_value=channel_layer,
        ):
            result = publish_realtime.apply(args=(status_envelope().to_dict(),))

        assert result.successful()
        assert channel_layer.sent == [
            (
                f"agreement-status.{AGREEMENT_ID}",
                {
                    "type": "agreement.status",
                    "agreement_id": str(AGREEMENT_ID),
                    "status": "HELD",
                    "sequence": 3,
                },
            )
        ]

    def test_status_task_rejects_payloads_that_contain_extra_data(self) -> None:
        from escrow.notifications.tasks import publish_realtime

        with self.assertRaises(Exception) as raised:
            publish_realtime.run(
                status_envelope(
                    payload={
                        "agreement_id": str(AGREEMENT_ID),
                        "status": "HELD",
                        "sequence": 3,
                        "customer_email": "buyer@example.test",
                    }
                ).to_dict()
            )

        assert type(raised.exception).__name__ == "Reject"


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
class PublicCheckoutStatusConsumerTests(TestCase):
    def test_authorized_checkout_receives_a_safe_status_event(self) -> None:
        from escrow.notifications.realtime import agreement_status_group_name
        from escrow.notifications.routing import websocket_urlpatterns

        application = URLRouter(websocket_urlpatterns)
        checkout_token = "chk_a_public_checkout_token"
        with patch(
            "escrow.notifications.consumers.find_checkout_agreement",
            return_value=SimpleNamespace(id=AGREEMENT_ID),
        ) as find_agreement:

            async def receive_status_event() -> tuple[bool, dict[str, object]]:
                communicator = WebsocketCommunicator(
                    application,
                    f"/ws/checkout/{checkout_token}/",
                )
                connected, _ = await communicator.connect()
                layer = get_channel_layer()
                assert layer is not None
                await layer.group_send(
                    agreement_status_group_name(AGREEMENT_ID),
                    {
                        "type": "agreement.status",
                        "agreement_id": str(AGREEMENT_ID),
                        "status": "HELD",
                        "sequence": 3,
                    },
                )
                event = await communicator.receive_json_from()
                await communicator.disconnect()
                return connected, event

            connected, event = async_to_sync(receive_status_event)()

        find_agreement.assert_called_once_with(checkout_token)
        assert connected
        assert event == {
            "agreement_id": str(AGREEMENT_ID),
            "status": "HELD",
            "sequence": 3,
        }

    def test_unknown_checkout_token_cannot_join_an_agreement_group(self) -> None:
        from escrow.notifications.routing import websocket_urlpatterns

        application = URLRouter(websocket_urlpatterns)
        with patch(
            "escrow.notifications.consumers.find_checkout_agreement",
            return_value=None,
        ):
            communicator = WebsocketCommunicator(
                application,
                "/ws/checkout/chk_unknown_checkout_token/",
            )
            connected, _ = async_to_sync(communicator.connect)()

        assert not connected
