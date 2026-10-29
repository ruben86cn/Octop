from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest
from octop_gateway.channels.feishu import FeishuChannel, FeishuConfig
from octop_gateway.group_context import GroupContextConfig
from octop_gateway.models import ChannelSubject

from octop.infra.gateway.feishu_transport import IsolatedFeishuChannel


@pytest.fixture
def channel() -> IsolatedFeishuChannel:
    channel = IsolatedFeishuChannel(
        Mock(),
        FeishuConfig(
            app_id="test", app_secret="test", group_context=GroupContextConfig(enabled=True)
        ),
    )
    channel._bot_open_id = "bot"
    return channel


def inbound(channel: IsolatedFeishuChannel, *, thread: str = "", mentioned: bool = True):
    return channel.parse_inbound(
        {
            "message_id": "current",
            "chat_id": "chat",
            "chat_type": "group",
            "thread_id": thread,
            "create_time": "2000000",
            "content": json.dumps({"text": "@bot"}),
            "mentions": [{"key": "@bot", "open_id": "bot"}] if mentioned else [],
            "sender": {"sender_id": "user"},
        }
    )


def record(mid: str, timestamp: int = 1999000, **extra):
    return {
        "message_id": mid,
        "chat_id": "chat",
        "msg_type": "text",
        "create_time": str(timestamp),
        "sender": {"id": "speaker"},
        "body": {"content": json.dumps({"text": f"original {mid}"})},
        **extra,
    }


async def test_quotes_are_separate_and_history_stays_in_time_and_chat(channel, monkeypatch):
    async def get(mid="", **params):
        if mid == "current":
            return {"items": [record(mid, parent_id="quote", root_id="root")]}
        if mid:
            return {"items": [record(mid, timestamp=1)]}
        assert params["container_id_type"] == "chat"
        assert params["start_time"] == "200"
        assert params["page_size"] == "50"
        return {
            "has_more": True,
            "items": [
                record("current"),
                record("quote"),
                record("late", 2001000),
                record("foreign", chat_id="other"),
                record("old", 199000),
                record("two"),
                record("one", 1998000),
                record("two"),
            ],
        }

    monkeypatch.setattr(channel, "_context_get", get)
    result = await channel._conversation_context(inbound(channel))
    assert [r["role"] for r in result["quoted_messages"]] == ["quoted_original", "topic_root"]
    assert result["quoted_messages"][0]["text"] == "original quote"
    assert [r["message_id"] for r in result["recent_messages"]] == ["one", "two"]
    assert result["incomplete"] == ["history_limit_reached"]


async def test_thread_history_uses_thread_container(channel, monkeypatch):
    get = AsyncMock(side_effect=[{"items": [record("current")]}, {"items": []}])
    monkeypatch.setattr(channel, "_context_get", get)
    await channel._conversation_context(inbound(channel, thread="topic"))
    assert get.call_args.kwargs["container_id_type"] == "thread"
    assert get.call_args.kwargs["container_id"] == "topic"
    assert "start_time" not in get.call_args.kwargs
    assert "end_time" not in get.call_args.kwargs


async def test_unavailable_history_still_passes_current_request(channel, monkeypatch):
    monkeypatch.setattr(channel, "_context_get", AsyncMock(side_effect=ValueError("no permission")))
    process = AsyncMock(return_value=True)
    monkeypatch.setattr(FeishuChannel, "_process_inbound", process)
    msg = inbound(channel)
    assert await channel._process_inbound(msg, msg.channel_subject)
    assert "quoted_messages_unavailable" in msg.text
    assert "recent_messages_unavailable" in msg.text
    process.assert_awaited_once()


async def test_passive_group_does_not_fetch_or_reply(channel, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(channel, "_conversation_context", fetch)
    await channel.handle_inbound(inbound(channel, mentioned=False))
    fetch.assert_not_awaited()


async def test_group_reply_opens_topic_and_failure_does_not_send_to_chat(channel, monkeypatch):
    reply = AsyncMock(return_value={"code": 0})
    send = AsyncMock()
    monkeypatch.setattr(channel, "_reply_message", reply)
    monkeypatch.setattr(channel, "_send_message", send)
    subject = ChannelSubject(
        subject_id="chat", metadata={"chat_type": "group", "message_id": "current"}
    )
    await channel._deliver(subject, msg_type="text", content="reply")
    reply.assert_awaited_once_with("current", msg_type="text", content="reply")
    send.assert_not_awaited()
    reply.return_value = {"code": 999}
    with pytest.raises(RuntimeError, match="topic reply failed"):
        await channel._deliver(subject, msg_type="text", content="reply")
    send.assert_not_awaited()


def test_rich_quotes_and_truncation(channel):
    item = record(
        "rich",
        msg_type="post",
        body={
            "content": json.dumps(
                {
                    "zh_cn": {
                        "title": "Title",
                        "content": [[{"tag": "text", "text": "Quoted original"}]],
                    }
                }
            )
        },
    )
    assert "Quoted original" in channel._context_record(item, "quoted_original")["text"]
    item["body"] = {"content": json.dumps({"text": "x" * 3000})}
    item["msg_type"] = "text"
    result = channel._context_record(item, "quoted_original")
    assert len(result["text"]) == 2000 and result["truncated"]


async def test_context_total_budget_keeps_recent_messages(channel, monkeypatch):
    items = [
        record(str(i), 1900000 + i, body={"content": json.dumps({"text": "x" * 3000})})
        for i in range(50)
    ]
    get = AsyncMock(side_effect=[{"items": [record("current")]}, {"items": items}])
    monkeypatch.setattr(channel, "_context_get", get)
    result = await channel._conversation_context(inbound(channel))
    assert len(json.dumps(result, ensure_ascii=False)) <= 24000
    assert result["recent_messages"][-1]["message_id"] == "49"
    assert "text_limit_reached" in result["incomplete"]


async def test_quote_from_other_chat_is_never_included(channel, monkeypatch):
    get = AsyncMock(
        side_effect=[
            {"items": [record("current", parent_id="foreign")]},
            {"items": [record("foreign", chat_id="other")]},
            {"items": []},
        ]
    )
    monkeypatch.setattr(channel, "_context_get", get)
    result = await channel._conversation_context(inbound(channel))
    assert result["quoted_messages"] == []
    assert "quoted_original_unavailable" in result["incomplete"]


async def test_direct_reply_uses_existing_delivery(channel, monkeypatch):
    deliver = AsyncMock()
    monkeypatch.setattr(FeishuChannel, "_deliver", deliver)
    subject = ChannelSubject(
        subject_id="user", metadata={"chat_type": "p2p", "message_id": "current"}
    )
    await channel._deliver(subject, msg_type="text", content="reply")
    deliver.assert_awaited_once_with(subject, msg_type="text", content="reply")


def file_record(mid, filename="report.pdf", key="file_key"):
    return record(
        mid, msg_type="file", body={"content": json.dumps({"file_key": key, "file_name": filename})}
    )


async def test_history_files_use_original_message_id_and_are_exposed_to_model(channel, monkeypatch):
    original = channel._context_record(file_record("original"), "quoted_original")
    assert (
        "/messages/original/resources/file_key?type=file"
        in original["attachments"][0]["content"]["url"]
    )
    context = {"quoted_messages": [original], "recent_messages": []}

    async def download(part):
        part.local_path = "feishu/test/history/original/report.pdf"
        part.size = 3

    monkeypatch.setattr(channel, "_download_context_attachment", download)
    msg = inbound(channel)
    await channel._attach_context_media(msg, context)
    assert msg.content[-1].local_path == "feishu/test/history/original/report.pdf"
    assert original["attachments"][0]["status"] == "available"
    assert original["attachments"][0]["local_path"] == msg.content[-1].local_path
    from octop.infra.gateway.process.harness_request import build_content_from_message

    model_content = await build_content_from_message(msg, locale="zh")
    assert isinstance(model_content, str)
    assert "inbound/feishu/test/history/original/report.pdf" in model_content


async def test_attachments_prioritize_quote_and_bound_downloads(channel, monkeypatch):
    context = {
        "quoted_messages": [channel._context_record(file_record("quote"), "quoted_original")],
        "recent_messages": [
            channel._context_record(file_record(str(i)), "recent_message") for i in range(10)
        ],
    }
    seen = []

    async def download(part):
        seen.append(part.url)
        part.local_path = "saved"

    monkeypatch.setattr(channel, "_download_context_attachment", download)
    await channel._attach_context_media(inbound(channel), context)
    assert len(seen) == 5
    assert "/quote/" in seen[0] and "/9/" in seen[1]
    assert context["recent_messages"][0]["attachments"][0]["status"] == "attachment_limit_reached"


async def test_unavailable_attachment_is_not_offered_as_readable(channel, monkeypatch):
    original = channel._context_record(file_record("original"), "quoted_original")
    context = {"quoted_messages": [original]}
    monkeypatch.setattr(
        channel, "_download_context_attachment", AsyncMock(side_effect=ValueError("no access"))
    )
    msg = inbound(channel)
    count = len(msg.content)
    await channel._attach_context_media(msg, context)
    assert len(msg.content) == count
    assert original["attachments"][0]["status"] == "download_failed_or_unavailable"


def test_deleted_attachment_is_not_downloaded(channel):
    item = file_record("deleted")
    item["deleted"] = True
    assert channel._context_record(item, "quoted_original")["attachments"] == []


async def test_download_saves_bytes_via_backend_and_reuses_cached_original(channel, monkeypatch):
    from octop_gateway.models import FileContent

    class Body:
        async def iter_chunked(self, size):
            yield b"file bytes"

    class Response:
        status = 200
        content_length = 10
        content_type = "application/pdf"
        content = Body()

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    backend = Mock(exists=AsyncMock(return_value=False), save=AsyncMock())
    channel._media_backend = backend
    http = Mock(get=Mock(return_value=Response()))
    monkeypatch.setattr(channel, "_ensure_http", AsyncMock(return_value=http))
    monkeypatch.setattr(
        channel, "_get_auth_headers", AsyncMock(return_value={"Authorization": "test"})
    )
    url = "https://open.feishu.cn/open-apis/im/v1/messages/original/resources/key?type=file"
    part = FileContent(url=url, filename="../report.pdf")
    await channel._download_context_attachment(part)
    assert part.local_path.endswith("/report.pdf") and ".." not in part.local_path
    backend.save.assert_awaited_once_with(b"file bytes", part.local_path)
    http.get.assert_called_once_with(url, headers={"Authorization": "test"}, allow_redirects=False)
    backend.exists.return_value = True
    again = FileContent(url=url, filename="../report.pdf")
    await channel._download_context_attachment(again)
    assert again.local_path == part.local_path
    assert http.get.call_count == 1


async def test_download_rejects_oversize_before_reading_body(channel, monkeypatch):
    from octop_gateway.models import FileContent

    class Response:
        status = 200
        content_length = 21 * 1024 * 1024

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    backend = Mock(exists=AsyncMock(return_value=False), save=AsyncMock())
    channel._media_backend = backend
    monkeypatch.setattr(
        channel, "_ensure_http", AsyncMock(return_value=Mock(get=Mock(return_value=Response())))
    )
    monkeypatch.setattr(channel, "_get_auth_headers", AsyncMock(return_value={}))
    part = FileContent(
        url="https://open.feishu.cn/open-apis/im/v1/messages/original/resources/key?type=file"
    )
    with pytest.raises(ValueError, match="attachment_size_limit"):
        await channel._download_context_attachment(part)
    assert part.local_path is None
    backend.save.assert_not_awaited()


@pytest.mark.parametrize("mentions", [[], [{"open_id": "other"}], [{"open_id": "all"}]])
async def test_unaddressed_group_never_reaches_shared_pipeline(channel, monkeypatch, mentions):
    from octop_gateway.channel import BaseChannel

    prepare = AsyncMock()
    monkeypatch.setattr(BaseChannel, "_prepare_inbound", prepare)
    assert await channel._prepare_inbound({"chat_type": "group", "mentions": mentions}) is None
    prepare.assert_not_called()


def test_event_reaches_only_addressed_bot_once(channel, monkeypatch):
    from types import SimpleNamespace

    other = IsolatedFeishuChannel(Mock(), FeishuConfig(app_id="other", app_secret="test"))
    other._bot_open_id = "other"
    for bot in (channel, other):
        bot._running = True
        bot._enqueue_callback = Mock()
        monkeypatch.setattr(bot, "_add_reaction_async", Mock())
    event = SimpleNamespace(
        header=SimpleNamespace(create_time="2000000"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="user")),
            message=SimpleNamespace(
                message_id="unique",
                message_type="text",
                content='{"text":"@bot"}',
                chat_id="chat",
                chat_type="group",
                mentions=[{"open_id": "bot"}],
            ),
        ),
    )
    for bot in (channel, other):
        bot._on_message_event(event)
        bot._on_message_event(event)
    channel._enqueue_callback.assert_called_once()
    channel._add_reaction_async.assert_called_once()
    other._enqueue_callback.assert_not_called()
    other._add_reaction_async.assert_not_called()
    assert not other._known_subjects


def test_unknown_identity_does_not_accept_any_group_mention(channel):
    channel._bot_open_id = ""
    assert not channel._is_bot_mentioned("group", [{"open_id": "bot"}])
    assert channel._is_bot_mentioned("p2p", [])


async def test_direct_messages_still_use_normal_pipeline(channel, monkeypatch):
    from octop_gateway.channel import BaseChannel

    prepare = AsyncMock(return_value=None)
    monkeypatch.setattr(BaseChannel, "_prepare_inbound", prepare)
    await channel._prepare_inbound({"chat_type": "p2p"})
    prepare.assert_awaited_once()
