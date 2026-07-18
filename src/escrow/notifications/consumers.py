"""Public checkout WebSocket consumer with opaque-token authorization."""

from __future__ import annotations

from typing import Any

from channels.db import database_sync_to_async  # type: ignore[import-untyped]
from channels.generic.websocket import AsyncJsonWebsocketConsumer  # type: ignore[import-untyped]

from escrow.agreements.services import find_checkout_agreement
from escrow.notifications.realtime import agreement_status_group_name, public_status_snapshot


class PublicCheckoutStatusConsumer(AsyncJsonWebsocketConsumer):  # type: ignore[misc]
    """Allow a checkout capability to observe only its agreement status sequence."""

    agreement_status_group: str | None = None

    async def connect(self) -> None:
        checkout_token = _checkout_token_from_scope(self.scope)
        if checkout_token is None:
            await self.close(code=4404)
            return
        agreement = await database_sync_to_async(find_checkout_agreement)(checkout_token)
        if agreement is None:
            await self.close(code=4404)
            return
        channel_layer = self.channel_layer
        if channel_layer is None:
            await self.close(code=1011)
            return
        self.agreement_status_group = agreement_status_group_name(agreement.id)
        await channel_layer.group_add(self.agreement_status_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code: int) -> None:
        del close_code
        if self.agreement_status_group is None or self.channel_layer is None:
            return
        await self.channel_layer.group_discard(self.agreement_status_group, self.channel_name)

    async def agreement_status(self, event: dict[str, object]) -> None:
        """Forward only the safe snapshot, even if an internal event grows later."""
        try:
            snapshot = public_status_snapshot(
                {
                    "agreement_id": event.get("agreement_id"),
                    "status": event.get("status"),
                    "sequence": event.get("sequence"),
                }
            )
        except ValueError:
            return
        await self.send_json(snapshot)


def _checkout_token_from_scope(scope: dict[str, Any]) -> str | None:
    route = scope.get("url_route")
    if not isinstance(route, dict):
        return None
    kwargs = route.get("kwargs")
    if not isinstance(kwargs, dict):
        return None
    token = kwargs.get("checkout_token")
    return token if isinstance(token, str) else None
