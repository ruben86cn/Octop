"""Octop-owned transport selection, including temporary credential probes."""

from __future__ import annotations

import uuid
from typing import Any

from octop_gateway.channel import BaseChannel, MessageProcessor
from octop_gateway.manager import ChannelManager as BaseChannelManager


class ChannelManager(BaseChannelManager):
    def _instantiate_channel(
        self,
        channel_type: str,
        config: Any,
        *,
        processor: MessageProcessor,
        channel_id: str | None,
        tenant_id: str | None,
        **kwargs: Any,
    ) -> BaseChannel:
        if channel_type != "feishu":
            return super()._instantiate_channel(
                channel_type,
                config,
                processor=processor,
                channel_id=channel_id,
                tenant_id=tenant_id,
                **kwargs,
            )
        from octop.infra.gateway.feishu_transport import IsolatedFeishuChannel

        cfg = self._build_config(IsolatedFeishuChannel, config)
        return IsolatedFeishuChannel(
            processor,
            config=cfg,
            channel_id=channel_id or cfg.channel_id or uuid.uuid4().hex,
            tenant_id=tenant_id or cfg.tenant_id,
            **kwargs,
        )
