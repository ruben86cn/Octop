"""Real SDK framing/reconnect with loop-bound fake sockets; no Feishu traffic."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from octop_gateway.channels.feishu import FeishuConfig

from octop.infra.gateway.channel_manager import ChannelManager
from octop.infra.gateway.feishu_transport import FeishuWSClient, IsolatedFeishuChannel, sdk


class Socket:
    def __init__(self) -> None:
        self.owner = asyncio.get_running_loop()
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.close_error = False
        self.sent: list[bytes] = []

    async def recv(self) -> bytes:
        assert asyncio.get_running_loop() is self.owner
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return bytes(value)

    async def send(self, value: bytes) -> None:
        assert asyncio.get_running_loop() is self.owner
        self.sent.append(value)

    async def close(self) -> None:
        assert asyncio.get_running_loop() is self.owner
        await asyncio.sleep(0)
        self.closed = True
        if self.close_error:
            raise OSError("synthetic close failure")


@pytest.fixture
def sockets(monkeypatch: pytest.MonkeyPatch) -> list[Socket]:
    created: list[Socket] = []

    async def connect(*args: Any, **kwargs: Any) -> Socket:
        conn = Socket()
        created.append(conn)
        return conn

    monkeypatch.setattr(sdk.websockets, "connect", connect)
    monkeypatch.setattr(
        FeishuWSClient,
        "_fetch_conn_url",
        AsyncMock(return_value="wss://offline.invalid/?device_id=test&service_id=1"),
    )
    return created


def client(name: str) -> FeishuWSClient:
    value = FeishuWSClient("dummy", "dummy", channel_id=name, activity=Mock())
    value._reconnect_nonce = 0
    value._reconnect_interval = 0
    value._reconnect_count = 2
    return value


async def until(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.001)
    pytest.fail("transport did not reach expected state")


def pong() -> bytes:
    frame = sdk._new_ping_frame(1)
    frame.headers[0].value = sdk.MessageType.PONG.value
    return bytes(frame.SerializeToString())


def test_two_owner_loops_reconnect_after_sdk_global_overwrite(
    sockets: list[Socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loops = [asyncio.new_event_loop(), asyncio.new_event_loop()]
    clients = [client("A"), client("B")]
    try:
        for loop, item in zip(loops, clients, strict=True):
            loop.run_until_complete(item._connect())
        # Reproduce the old failure trigger: SDK global points at another loop.
        monkeypatch.setattr(sdk, "loop", loops[1])
        for _ in range(3):
            for loop, item in zip(loops, clients, strict=True):
                old = item._conn
                loop.run_until_complete(item._disconnect())
                assert old.closed
                loop.run_until_complete(item._reconnect())
                assert item._receiver is not None and item._receiver.get_loop() is loop
                item._conn.incoming.put_nowait(pong())
                loop.run_until_complete(asyncio.sleep(0.01))
                assert not item._receiver.done()
                assert item._conn.owner is loop
                assert not item._lock.locked()
        assert sdk.loop is loops[1]  # The fix never replaces global SDK state.
    finally:
        for loop, item in zip(loops, clients, strict=True):
            loop.run_until_complete(item._disconnect())
            assert not asyncio.all_tasks(loop)
            loop.close()


@pytest.mark.asyncio
async def test_receiver_and_close_failure_reconnect_without_disturbing_peer(
    sockets: list[Socket],
) -> None:
    a, b = client("A"), client("B")
    tasks = [asyncio.create_task(c.run()) for c in (a, b)]
    try:
        await until(lambda: len(sockets) == 2)
        peer = b._conn
        first = a._conn
        first.close_error = True
        first.incoming.put_nowait(OSError("synthetic receive failure"))
        await until(lambda: a._conn is not None and a._conn is not first)
        assert first.closed
        assert b._conn is peer and not peer.closed
        second = a._conn
        a._receiver.cancel()
        await until(lambda: a._conn is not None and a._conn is not second)
        assert second.closed and b._conn is peer
        a._conn.incoming.put_nowait(pong())
        peer.incoming.put_nowait(pong())
        await until(lambda: a._activity.call_count >= 4 and b._activity.call_count >= 2)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert all(s.closed for s in sockets)
    assert all(c._receiver is None and not c._frames and c._conn is None for c in (a, b))


@pytest.mark.asyncio
async def test_connect_is_idempotent_and_does_not_leak_lock(sockets: list[Socket]) -> None:
    c = client("A")
    try:
        await c._connect()
        await asyncio.wait_for(c._connect(), timeout=1)
        assert not c._lock.locked() and len(sockets) == 1
        c._receiver.cancel()
        await asyncio.gather(c._receiver, return_exceptions=True)
        with pytest.raises(RuntimeError, match="no live receiver"):
            await c._connect()
        assert not c._lock.locked()
    finally:
        await c._disconnect()


@pytest.mark.asyncio
async def test_channel_stop_cleans_receiver_ping_frames_and_socket(
    sockets: list[Socket],
) -> None:
    channel = IsolatedFeishuChannel(AsyncMock(), FeishuConfig(app_id="dummy", app_secret="dummy"))
    await channel._start_ws_client()
    await until(lambda: channel.is_connected)
    c = channel._ws_client
    c._conn.incoming.put_nowait(pong())
    await until(lambda: channel._last_ws_activity_at > 0 and bool(c._conn.sent))
    await channel._stop_ws_client()
    assert not channel.is_connected
    assert c._receiver is None and not c._frames
    assert all(s.closed for s in sockets)
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("feishu-")]


@pytest.mark.asyncio
async def test_temporary_probe_uses_fixed_transport_and_leaves_existing_socket(
    sockets: list[Socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(IsolatedFeishuChannel, "_refresh_token", AsyncMock(return_value="dummy"))
    monkeypatch.setattr(IsolatedFeishuChannel, "_ensure_bot_open_id", AsyncMock(return_value="bot"))
    manager = ChannelManager(processor=AsyncMock())
    await manager.start()
    try:
        await manager.add_channel(
            "feishu", {"app_id": "dummy", "app_secret": "dummy"}, channel_id="A"
        )
        a = manager.get_channel("A")
        assert isinstance(a, IsolatedFeishuChannel)
        await until(lambda: a.is_connected)
        connection = a._ws_client._conn
        for _ in range(3):
            await manager.probe_channel(
                "feishu",
                {"app_id": "probe", "app_secret": "dummy"},
                channel_id="__probe__",
                tenant_id="test",
            )
            assert a.is_connected and a._ws_client._conn is connection
        assert not connection.closed
    finally:
        await manager.stop()
    assert all(s.closed for s in sockets)


@pytest.mark.asyncio
async def test_idle_pongs_keep_receiver_alive_but_silent_socket_reconnects(
    sockets: list[Socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from octop.infra.gateway import feishu_transport

    monkeypatch.setattr(feishu_transport, "_MIN_HEARTBEAT_TIMEOUT", 0.05)
    c = client("idle")
    c._ping_interval = 0.01
    task = asyncio.create_task(c.run())
    try:
        await until(lambda: c._conn is not None)
        first = c._conn
        for _ in range(10):
            first.incoming.put_nowait(pong())
            await asyncio.sleep(0.01)
            assert c._conn is first
        assert c._activity.call_count >= 10
        await until(lambda: c._conn is not None and c._conn is not first)
        assert first.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_sdk_group_mention_dispatch_survives_reconnect(
    sockets: list[Socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    channel = IsolatedFeishuChannel(
        AsyncMock(),
        FeishuConfig(app_id="dummy", app_secret="dummy"),
        channel_id="group",
    )
    channel._running = True
    channel._main_loop = asyncio.get_running_loop()
    # Normal start() resolves identity before opening the transport.
    channel._bot_open_id = "bot"
    received: list[Any] = []
    channel.set_enqueue_callback(received.append)
    # Typing reactions would be real HTTP; only transport/dispatch are under test.
    monkeypatch.setattr(channel, "_add_reaction_async", Mock())
    await channel._start_ws_client()
    try:
        await until(lambda: channel.is_connected)
        c = channel._ws_client
        c._reconnect_nonce = c._reconnect_interval = 0
        for number in range(3):
            frame = sdk.Frame(method=sdk.FrameType.DATA.value, SeqID=0, LogID=0, service=1)
            for key, value in {
                sdk.HEADER_MESSAGE_ID: str(number),
                sdk.HEADER_TRACE_ID: "test",
                sdk.HEADER_SUM: "1",
                sdk.HEADER_SEQ: "0",
                sdk.HEADER_TYPE: "event",
            }.items():
                header = frame.headers.add()
                header.key, header.value = key, value
            frame.payload = json.dumps(
                {
                    "schema": "2.0",
                    "header": {
                        "event_id": str(number),
                        "event_type": "im.message.receive_v1",
                        "create_time": "1",
                    },
                    "event": {
                        "sender": {"sender_id": {"open_id": "human"}, "sender_type": "user"},
                        "message": {
                            "message_id": f"message-{number}",
                            "chat_id": "group-id",
                            "chat_type": "group",
                            "message_type": "text",
                            "content": json.dumps({"text": "@_user_1 test"}),
                            "mentions": [
                                {"key": "@_user_1", "id": {"open_id": "bot"}, "name": "Test"}
                            ],
                        },
                    },
                }
            ).encode()
            c._conn.incoming.put_nowait(frame.SerializeToString())
            await until(lambda number=number: len(received) == number + 1)
            assert received[-1]["mentions"][0]["open_id"] == "bot"
            previous = c._conn
            previous.incoming.put_nowait(OSError("drop"))
            await until(lambda previous=previous: c._conn is not None and c._conn is not previous)
    finally:
        channel._running = False
        await channel._stop_ws_client()
    assert len(received) == 3


@pytest.mark.asyncio
async def test_endpoint_discovery_keeps_sdk_configuration_and_error_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    c = client("endpoint")
    post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "URL": "wss://offline.invalid/?device_id=test&service_id=1",
                    "ClientConfig": {
                        "ReconnectCount": 4,
                        "ReconnectInterval": 7,
                        "ReconnectNonce": 2,
                        "PingInterval": 15,
                    },
                },
            },
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await c._fetch_conn_url() == "wss://offline.invalid/?device_id=test&service_id=1"
    assert c._ping_interval == 15 and c._reconnect_interval == 7
    assert post.call_args.kwargs["json"] == {"AppID": "dummy", "AppSecret": "dummy"}
    post.return_value = httpx.Response(503)
    with pytest.raises(sdk.ServerException):
        await c._fetch_conn_url()
    post.return_value = httpx.Response(200, json={"code": 100004, "msg": "bad credentials"})
    with pytest.raises(sdk.ClientException):
        await c._fetch_conn_url()


@pytest.mark.asyncio
async def test_watchdog_restarts_dead_transport_not_idle_healthy_channel(
    sockets: list[Socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from octop.infra.gateway import feishu_transport

    monkeypatch.setattr(feishu_transport, "_WATCHDOG_INTERVAL", 0.01)
    channel = IsolatedFeishuChannel(AsyncMock(), FeishuConfig(app_id="dummy", app_secret="dummy"))
    channel._running = True
    await channel._start_ws_client()
    channel._ws_watchdog_task = asyncio.create_task(channel._ws_watchdog_loop())
    try:
        await until(lambda: channel.is_connected)
        first = channel._ws_client._conn
        channel._last_ws_activity_at = 1  # No user messages for years is not a fault.
        await asyncio.sleep(0.04)
        assert channel._ws_client._conn is first
        channel._transport_task.cancel()
        await until(lambda: channel.is_connected and channel._ws_client._conn is not first)
        assert first.closed
    finally:
        await channel.stop()
    assert all(s.closed for s in sockets)
