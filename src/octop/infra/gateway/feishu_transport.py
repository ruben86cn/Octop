"""Instance-owned Feishu transport for lark-oapi 1.7.x.

The SDK's blocking start/connect/receive methods use a module-global loop.
Octop's multiple channels must never use or overwrite that loop. Keep the SDK's
protocol, authentication and reconnect policy, but own all I/O tasks here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import lark_oapi as lark
from lark_oapi.ws import client as sdk

from octop.infra.gateway.feishu_context import FeishuContextChannel

logger = logging.getLogger(__name__)
_MIN_HEARTBEAT_TIMEOUT = 90.0
_WATCHDOG_INTERVAL = 30.0
_RECONNECT_STALE_SECONDS = 180.0


class FeishuWSClient(sdk.Client):  # type: ignore[misc]  # SDK has no type stubs
    def __init__(
        self, *args: Any, channel_id: str, activity: Callable[[], None], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._conn: Any = None
        self.channel_id = channel_id
        self._activity = activity
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._frames: set[asyncio.Task[None]] = set()

    def _fmt_log(self, fmt: str, *args: Any) -> str:
        return f"[channel={self.channel_id}] {super()._fmt_log(fmt, *args)}"

    async def run(self) -> None:
        self._owner_loop = asyncio.get_running_loop()
        ping: asyncio.Task[Any] | None = None
        try:
            try:
                await self._connect()
            except sdk.ClientException:
                raise
            except Exception:
                await self._disconnect()
                await self._reconnect()
            ping = asyncio.create_task(self._ping_loop(), name=f"feishu-ping-{self.channel_id}")
            while True:
                receiver = self._receiver
                if receiver is None:
                    raise RuntimeError("Feishu receiver was not started")
                try:
                    await receiver
                except asyncio.CancelledError:
                    # An independently cancelled receiver is a failed transport,
                    # while cancelling run() is an intentional channel shutdown.
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                    logger.warning("Feishu receiver cancelled (channel=%s)", self.channel_id)
                except Exception as exc:
                    logger.warning(
                        "Feishu receiver failed (channel=%s, error=%s)",
                        self.channel_id,
                        type(exc).__name__,
                    )
                await self._disconnect()
                await self._reconnect()
        finally:
            if ping is not None:
                ping.cancel()
                await asyncio.gather(ping, return_exceptions=True)
            await self._disconnect()

    async def _connect(self) -> None:
        owner = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = owner
        if owner is not self._owner_loop:
            raise RuntimeError("Feishu client used outside its owning event loop")
        async with self._lock:
            if self._conn is not None:
                if self._receiver is not None and not self._receiver.done():
                    return
                raise RuntimeError("Feishu socket has no live receiver")
            conn_url = await self._fetch_conn_url()
            query = parse_qs(urlparse(conn_url).query)
            try:
                conn = await sdk.websockets.connect(conn_url, **sdk._ws_connect_kwargs())
            except sdk.InvalidHandshake as exc:
                sdk._parse_ws_conn_exception(exc)
                raise
            self._conn = conn
            self._conn_url = conn_url
            self._conn_id = query[sdk.DEVICE_ID][0]
            self._service_id = query[sdk.SERVICE_ID][0]
            self._receiver = owner.create_task(
                self._receive_message_loop(), name=f"feishu-receiver-{self.channel_id}"
            )
            self._activity()
            logger.info("Feishu transport connected (channel=%s)", self.channel_id)

    async def _fetch_conn_url(self) -> str:
        # Equivalent app-secret handshake to SDK _get_conn_url, but cancellable
        # and bounded: requests.post in the SDK blocks without a timeout.
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                self._domain + sdk.GEN_ENDPOINT_URI,
                headers={"locale": "zh", sdk.USER_AGENT: self._user_agent},
                json={"AppID": self._app_id, "AppSecret": self._app_secret},
            )
        if response.status_code != 200:
            raise sdk.ServerException(response.status_code, "Feishu endpoint unavailable")
        data = sdk.JSON.unmarshal(response.text, sdk.EndpointResp)
        if data.code in (sdk.SYSTEM_BUSY, sdk.INTERNAL_ERROR):
            raise sdk.ServerException(data.code, data.msg)
        if data.code != sdk.OK:
            raise sdk.ClientException(data.code, data.msg)
        if data.data.ClientConfig is not None:
            self._configure(data.data.ClientConfig)
        return str(data.data.URL)

    async def _receive_message_loop(self) -> None:
        conn = self._conn
        if conn is None:
            raise RuntimeError("Feishu socket missing")
        while True:
            # PONG frames count as activity: an idle group is not a dead socket.
            # A silent socket is failed even when its TCP connection stays open.
            msg = await asyncio.wait_for(
                conn.recv(), timeout=max(_MIN_HEARTBEAT_TIMEOUT, 3 * self._ping_interval)
            )
            self._activity()
            task = asyncio.create_task(self._handle_message(msg))
            self._frames.add(task)
            task.add_done_callback(self._frames.discard)

    async def _disconnect(self) -> None:
        tasks = list(self._frames)
        if self._receiver is not None:
            tasks.append(self._receiver)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._receiver = None
        self._frames.clear()
        async with self._lock:
            conn = self._conn
            self._conn = None
            self._conn_url = self._conn_id = self._service_id = ""
            if conn is not None:
                try:
                    await asyncio.wait_for(conn.close(), timeout=5.0)
                except Exception as exc:
                    logger.warning(
                        "Feishu socket close failed (channel=%s, error=%s)",
                        self.channel_id,
                        type(exc).__name__,
                    )


class IsolatedFeishuChannel(FeishuContextChannel):
    """Use the normal gateway message/mention handling with isolated WS tasks."""

    _transport_task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        client = self._ws_client
        return bool(
            self._transport_task is not None
            and not self._transport_task.done()
            and client is not None
            and client._conn is not None
            and client._receiver is not None
            and not client._receiver.done()
        )

    @property
    def runtime_error(self) -> str | None:
        return None if self.is_connected else "Feishu transport is connecting or reconnecting"

    async def _start_ws_client(self) -> None:
        self._ws_session_id = str(uuid.uuid4())
        self._ws_reconnecting_since = None
        self._mark_ws_activity()
        handler = (
            lark.EventDispatcherHandler.builder(
                self._config.encrypt_key or "", self._config.verification_token or ""
            )
            .register_p2_im_message_receive_v1(self._on_message_event)
            .build()
        )
        self._ws_client = FeishuWSClient(
            self._config.app_id,
            self._config.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
            channel_id=self.channel_id,
            activity=self._mark_ws_activity,
        )
        self._ws_client.on_reconnecting = self._on_ws_reconnecting
        self._ws_client.on_reconnected = self._on_ws_reconnected
        self._transport_task = asyncio.create_task(
            self._ws_client.run(), name=f"feishu-transport-{self.channel_id}"
        )
        self._transport_task.add_done_callback(self._transport_done)

    def _transport_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Feishu transport exited (channel=%s, error=%s)",
                self.channel_id,
                type(task.exception()).__name__,
            )

    async def _stop_ws_client(self, *, force: bool = True) -> bool:
        task = self._transport_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._transport_task = None
        self._ws_client = None
        self._ws_session_id = None
        self._ws_reconnecting_since = None
        return True

    async def _ws_watchdog_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            stalled = (
                self._ws_reconnecting_since is not None
                and time.time() - self._ws_reconnecting_since >= _RECONNECT_STALE_SECONDS
            )
            if self._running and (
                self._transport_task is None or self._transport_task.done() or stalled
            ):
                logger.warning("Restarting stopped Feishu transport (channel=%s)", self.channel_id)
                await self._restart_ws_client()
