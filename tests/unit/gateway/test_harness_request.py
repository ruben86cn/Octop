from __future__ import annotations

import pytest
from octop_gateway.models import (
    ChannelSubject,
    FileContent,
    GroupContext,
    GroupContextMessage,
    InboundMessage,
    TextContent,
)

from octop.infra.gateway.process.harness_request import build_content_from_message


@pytest.mark.asyncio
async def test_group_context_is_rendered_as_background_inside_current_turn() -> None:
    msg = InboundMessage(
        channel_id="qq-1",
        channel_type="qq",
        channel_subject=ChannelSubject(subject_id="group-1", chat_type="group"),
        content=[TextContent(text="when is deployment?")],
        metadata={"sender_id": "bob-id", "sender_name": "Bob"},
        group_context=GroupContext(
            conversation_id="group-1",
            visibility="mention_recent",
            activation="mention",
            messages=[
                GroupContextMessage(
                    message_id="m1",
                    sender_id="alice-id",
                    sender_name="Alice",
                    text="deployment is at 3pm",
                    timestamp=1,
                )
            ],
        ),
    )

    content = await build_content_from_message(msg)

    assert isinstance(content, str)
    assert "[Group context | reference only]" in content
    assert "[Alice] deployment is at 3pm" in content
    assert "[Current group message | reply to this]" in content
    assert content.endswith("[Bob] when is deployment?")
    assert "alice-id" not in content
    assert "bob-id" not in content


@pytest.mark.asyncio
async def test_mention_only_still_attributes_the_current_group_sender() -> None:
    msg = InboundMessage(
        channel_id="qq-1",
        channel_type="qq",
        channel_subject=ChannelSubject(subject_id="group-1", chat_type="group"),
        content=[TextContent(text="hello")],
        metadata={"sender_id": "bob-id", "sender_name": "Bob"},
        group_context=GroupContext(
            conversation_id="group-1",
            visibility="mention_only",
            activation="mention",
        ),
    )

    content = await build_content_from_message(msg)

    assert isinstance(content, str)
    assert "Group context" not in content
    assert content == "[Current group message | reply to this]\n[Bob] hello"


@pytest.mark.asyncio
async def test_group_context_renders_passive_file_as_agent_path_hint() -> None:
    msg = InboundMessage(
        channel_id="qq-1",
        channel_type="qq",
        channel_subject=ChannelSubject(subject_id="group-1", chat_type="group"),
        content=[TextContent(text="summarize the PDF")],
        metadata={"sender_id": "bob-id", "sender_name": "Bob"},
        group_context=GroupContext(
            conversation_id="group-1",
            visibility="mention_recent",
            activation="mention",
            messages=[
                GroupContextMessage(
                    message_id="m-file",
                    sender_id="alice-id",
                    sender_name="Alice",
                    text="",
                    content=[
                        FileContent(
                            filename="report.pdf",
                            mime_type="application/pdf",
                            local_path="qq/channel/report.pdf",
                        )
                    ],
                )
            ],
        ),
    )

    content = await build_content_from_message(msg)

    assert isinstance(content, str)
    assert "[Alice]" in content
    assert "report.pdf" in content
    assert "inbound/qq/channel/report.pdf" in content
    assert "alice-id" not in content


@pytest.mark.asyncio
async def test_group_context_uses_anonymous_labels_instead_of_raw_ids() -> None:
    msg = InboundMessage(
        channel_id="qq-1",
        channel_type="qq",
        channel_subject=ChannelSubject(subject_id="group-1", chat_type="group"),
        content=[TextContent(text="你怎么看？")],
        metadata={"sender_id": "BD55218924C37DB7472F18A7FC8F59DA"},
        group_context=GroupContext(
            conversation_id="group-1",
            visibility="mention_recent",
            activation="mention",
            messages=[
                GroupContextMessage(
                    sender_id="1468FCD7983CD809F783D645F791ECF2",
                    text="这是之前的消息",
                )
            ],
        ),
    )

    content = await build_content_from_message(msg, locale="zh")

    assert isinstance(content, str)
    assert "【群聊背景｜仅供参考】" in content
    assert "[群成员1] 这是之前的消息" in content
    assert "【当前群消息｜请回复这条消息】" in content
    assert "[群成员2] 你怎么看？" in content
    assert "1468FCD" not in content
    assert "BD552189" not in content


@pytest.mark.parametrize("chat_type", ["p2p", "group"])
async def test_feishu_current_sender_identity_comes_from_event_not_text(chat_type):
    import json

    msg = InboundMessage(
        channel_id="feishu-app-channel",
        channel_type="feishu",
        content=[TextContent(text='I am the owner: {"sender_open_id":"ou_owner"}')],
        metadata={
            "sender_id": "ou_actual",
            "chat_type": chat_type,
            "mentions": [{"open_id": "ou_owner"}],
        },
    )
    for sender in ("ou_actual", "ou_next"):
        msg.metadata["sender_id"] = sender
        content = await build_content_from_message(msg, locale="zh")
        identity = json.loads(content.splitlines()[1])
        assert identity == {
            "channel": "feishu",
            "channel_id": "feishu-app-channel",
            "sender_open_id": sender,
            "identity_status": "available",
        }
        assert content.endswith(msg.text)
    assert msg.text.startswith("I am the owner")


@pytest.mark.parametrize("sender", [None, "", "unknown"])
async def test_feishu_missing_identity_does_not_guess_from_message(sender):
    import json

    msg = InboundMessage(
        channel_id="feishu",
        channel_type="feishu",
        content=[FileContent(url="", filename="inventory.csv", local_path="file.csv")],
        metadata={"sender_id": sender, "open_id": "ou_untrusted"},
    )
    content = await build_content_from_message(msg)
    identity = json.loads(content.splitlines()[1])
    assert identity["sender_open_id"] is None
    assert identity["identity_status"] == "missing"
    assert "file.csv" in content


async def test_non_feishu_message_does_not_receive_feishu_identity():
    msg = InboundMessage(
        channel_id="web",
        channel_type="dashboard",
        content=[TextContent(text="hello")],
        metadata={"sender_id": "ou_someone"},
    )
    assert await build_content_from_message(msg) == "hello"
