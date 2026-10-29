"""Bounded Feishu history and explicit quoted-message context for mentions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from octop_gateway.channels.feishu import FeishuChannel
from octop_gateway.models import (
    ChannelSubject,
    FileContent,
    ImageContent,
    InboundMessage,
    TextContent,
)

from octop.i18n import tr

logger = logging.getLogger(__name__)
_API = "https://open.feishu.cn/open-apis/im/v1/messages"
_HISTORY_SECONDS = 30 * 60
_HISTORY_LIMIT = 50
_TEXT_LIMIT = 2000
_CONTEXT_LIMIT = 24000
_ATTACHMENT_LIMIT = 5
_ATTACHMENT_BYTES = 20 * 1024 * 1024


class FeishuContextChannel(FeishuChannel):
    """Add context only after the shared gateway has accepted an activation."""

    def _is_bot_mentioned(self, chat_type: str, mentions: list[Any]) -> bool:
        if chat_type != "group":
            return True
        # With several bots in one group, an unknown identity must fail closed.
        return bool(self._bot_open_id) and any(
            isinstance(mention, dict) and mention.get("open_id") == self._bot_open_id
            for mention in mentions
        )

    def _on_message_event(self, data: Any) -> None:
        message = getattr(getattr(data, "event", None), "message", None)
        if message is not None and getattr(message, "chat_type", "") == "group":
            mentions = self._normalize_mentions(getattr(message, "mentions", None))
            if not self._is_bot_mentioned("group", mentions):
                return
        # Filter before the SDK adds Typing, logs, deduplicates or enqueues.
        super()._on_message_event(data)

    async def _prepare_inbound(
        self, raw_payload: object
    ) -> tuple[InboundMessage, ChannelSubject] | None:
        # Also protect direct/queued ingress, regardless of old channel settings.
        if isinstance(raw_payload, dict) and raw_payload.get("chat_type") == "group":
            mentions = self._normalize_mentions(raw_payload.get("mentions"))
            if not self._is_bot_mentioned("group", mentions):
                return None
        return await super()._prepare_inbound(raw_payload)

    async def _context_get(self, message_id: str = "", **params: str) -> dict[str, Any]:
        http = await self._ensure_http()
        headers = await self._get_auth_headers()
        url = f"{_API}/{quote(message_id, safe='')}" if message_id else _API
        async with http.get(url, headers=headers, params=params) as response:
            response.raise_for_status()
            data: dict[str, Any] = await response.json()
        if data.get("code") != 0:
            # Never log response bodies: they may contain chat content.
            raise ValueError(f"Feishu history API code={data.get('code')}")
        return dict(data.get("data") or {})

    async def _context_message(self, message_id: str, chat_id: str) -> dict[str, Any]:
        data = await self._context_get(message_id)
        for item in data.get("items", []):
            if item.get("message_id") == message_id and item.get("chat_id") == chat_id:
                return dict(item)
        return {}

    def _context_record(self, item: dict[str, Any], role: str) -> dict[str, Any]:
        kind = str(item.get("msg_type") or "unknown")
        raw = (item.get("body") or {}).get("content", "{}")
        try:
            body = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            body = {}
        parts = []
        if (
            not item.get("deleted")
            and kind in {"text", "post", "file", "image"}
            and isinstance(body, dict)
        ):
            parts = self._parse_content_parts(
                kind, body, message_id=str(item.get("message_id") or "")
            )
        text = "\n".join(part.text for part in parts if isinstance(part, TextContent))
        attachments = [
            {"content": part.model_dump(mode="json", exclude_none=True), "status": "not_downloaded"}
            for part in parts
            if isinstance(part, (FileContent, ImageContent))
        ]
        return {
            "role": role,
            "message_id": item.get("message_id", ""),
            "parent_id": item.get("parent_id", ""),
            "sender": item.get("sender") or {},
            "timestamp_ms": item.get("create_time", ""),
            "message_type": kind,
            "deleted": bool(item.get("deleted")),
            "text": text[:_TEXT_LIMIT],
            "attachments": attachments,
            "truncated": len(text) > _TEXT_LIMIT,
        }

    async def _conversation_context(self, message: InboundMessage) -> dict[str, Any]:
        meta = message.metadata
        chat_id = str(meta.get("chat_id") or "")
        message_id = str(meta.get("message_id") or "")
        result: dict[str, Any] = {"quoted_messages": [], "recent_messages": [], "incomplete": []}
        if not chat_id or not message_id:
            return result
        current: dict[str, Any] = {}
        try:
            current = await self._context_message(message_id, chat_id)
            if not current:
                result["incomplete"].append("current_message_unavailable")
            seen = {message_id}
            # Direct parent is the quoted original; root labels the topic, not
            # necessarily the exact quoted text. Keep those roles distinct.
            for key, role in (("parent_id", "quoted_original"), ("root_id", "topic_root")):
                target = str(current.get(key) or "")
                if target and target not in seen:
                    seen.add(target)
                    original = await self._context_message(target, chat_id)
                    if original:
                        result["quoted_messages"].append(self._context_record(original, role))
                    else:
                        result["incomplete"].append(f"{role}_unavailable")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            result["incomplete"].append("quoted_messages_unavailable")
            logger.warning("Feishu quote lookup unavailable (channel=%s)", self.channel_id)
        if meta.get("chat_type") != "group":
            return result
        thread_id = str(meta.get("thread_id") or current.get("thread_id") or "")
        end = int(message.timestamp)
        try:
            params = {
                "container_id_type": "thread" if thread_id else "chat",
                "container_id": thread_id or chat_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": str(_HISTORY_LIMIT),
            }
            # Feishu does not support server-side time bounds for threads.
            # Enforce the same cutoff below for both container types.
            if not thread_id:
                params.update(start_time=str(end - _HISTORY_SECONDS), end_time=str(end + 1))
            data = await self._context_get(**params)
            quoted_ids = {r["message_id"] for r in result["quoted_messages"]}
            records = []
            seen_ids = {message_id, *quoted_ids}
            for item in data.get("items", []):
                mid = str(item.get("message_id") or "")
                timestamp = int(item.get("create_time") or 0) / 1000
                if (
                    not mid
                    or mid in seen_ids
                    or item.get("chat_id") != chat_id
                    or not end - _HISTORY_SECONDS <= timestamp <= message.timestamp
                ):
                    continue
                seen_ids.add(mid)
                records.append(self._context_record(item, "recent_message"))
            records.sort(key=lambda r: int(r["timestamp_ms"] or 0))
            result["recent_messages"] = records[-_HISTORY_LIMIT:]
            if data.get("has_more"):
                result["incomplete"].append("history_limit_reached")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            result["incomplete"].append("recent_messages_unavailable")
            logger.warning("Feishu history lookup unavailable (channel=%s)", self.channel_id)
        while (
            len(json.dumps(result, ensure_ascii=False)) > _CONTEXT_LIMIT
            and result["recent_messages"]
        ):
            result["recent_messages"].pop(0)
            if "text_limit_reached" not in result["incomplete"]:
                result["incomplete"].append("text_limit_reached")
        return result

    async def _download_context_attachment(self, part: FileContent | ImageContent) -> None:
        """Download through the bot identity and save through the agent workspace adapter."""
        backend = self._media_backend
        if backend is None:
            raise ValueError("attachment_workspace_unavailable")
        # URLs are built from same-chat API records, never from chat text.
        if not part.url.startswith(_API + "/") or "/resources/" not in part.url:
            raise ValueError("invalid_attachment_resource")
        digest = hashlib.sha256(part.url.encode()).hexdigest()[:24]
        name = self._safe_media_filename(
            part.filename if isinstance(part, FileContent) else "image.png"
        )
        key = f"feishu/{self.channel_id}/history/{digest}/{name}"
        if await backend.exists(key):
            part.local_path = key
            return
        http = await self._ensure_http()
        headers = await self._get_auth_headers()
        async with http.get(part.url, headers=headers, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("attachment_download_failed")
            if response.content_length and response.content_length > _ATTACHMENT_BYTES:
                raise ValueError("attachment_size_limit")
            chunks = bytearray()
            async for block in response.content.iter_chunked(65536):
                if len(chunks) + len(block) > _ATTACHMENT_BYTES:
                    raise ValueError("attachment_size_limit")
                chunks.extend(block)
            mime = response.content_type or "application/octet-stream"
        await backend.save(bytes(chunks), key)
        part.local_path = key
        part.size = len(chunks)
        part.mime_type = part.mime_type or mime

    async def _attach_context_media(self, message: InboundMessage, context: dict[str, Any]) -> None:
        records = [
            *context.get("quoted_messages", []),
            *reversed(context.get("recent_messages", [])),
        ]
        available: dict[str, FileContent | ImageContent] = {}
        attempts = 0
        deadline = asyncio.get_running_loop().time() + 60
        for record in records:
            for attachment in record.get("attachments", []):
                content = attachment["content"]
                part = (FileContent if content["type"] == "file" else ImageContent).model_validate(
                    content
                )
                if part.url in available:
                    attachment.update(status="available", local_path=available[part.url].local_path)
                    continue
                if attempts >= _ATTACHMENT_LIMIT or asyncio.get_running_loop().time() >= deadline:
                    attachment["status"] = "attachment_limit_reached"
                    continue
                attempts += 1
                try:
                    async with asyncio.timeout(
                        min(20, deadline - asyncio.get_running_loop().time())
                    ):
                        await self._download_context_attachment(part)
                except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
                    attachment["status"] = "download_failed_or_unavailable"
                    logger.warning(
                        "Feishu historical attachment unavailable (channel=%s)", self.channel_id
                    )
                    continue
                available[part.url] = part
                message.content.append(part)
                attachment.update(status="available", local_path=part.local_path)

    async def _process_inbound(self, message: InboundMessage, subject: ChannelSubject) -> bool:
        if message.metadata.get("bot_mentioned") and not message.text.lstrip().startswith("/"):
            try:
                async with asyncio.timeout(12):
                    context = await self._conversation_context(message)
            except TimeoutError:
                context = {"incomplete": ["context_lookup_timeout"]}
            await self._attach_context_media(message, context)
            # Keep the actual user's text intact, then attach labeled data.
            message.content.append(
                TextContent(
                    text=tr("channel.feishu_context", "zh")
                    + "\n"
                    + json.dumps(context, ensure_ascii=False)
                )
            )
        return bool(await super()._process_inbound(message, subject))

    async def _deliver(self, subject: ChannelSubject, *, msg_type: str, content: str) -> None:
        meta = subject.metadata or {}
        reply_to = str(meta.get("message_id") or "")
        if meta.get("chat_type") == "group" and reply_to:
            data = await self._reply_message(reply_to, msg_type=msg_type, content=content)
            if data.get("code") != 0:
                # Do not turn a failed/uncertain topic reply into a duplicate
                # top-level message. Let the gateway report delivery failure.
                raise RuntimeError(f"Feishu topic reply failed: code={data.get('code')}")
            return
        await super()._deliver(subject, msg_type=msg_type, content=content)
