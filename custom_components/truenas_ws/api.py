"""Minimal asyncio client for the TrueNAS JSON-RPC 2.0 over WebSocket API.

TrueNAS 25.04+ exposes its middleware at ``wss://<host>/api/current``.
API keys must only be sent over TLS: TrueNAS revokes a key that is used
over plain HTTP, so this client always uses ``wss://``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import itertools
import json
import logging
from typing import Any

import aiohttp

from .const import JOB_FINAL_STATES

_LOGGER = logging.getLogger(__name__)

JSONRPC_METHOD_NOT_FOUND = -32601
CONNECT_TIMEOUT = 15
CALL_TIMEOUT = 60
MAX_CONCURRENT_CALLS = 5


class TrueNASError(Exception):
    """Base error for the TrueNAS client."""


class TrueNASConnectionError(TrueNASError):
    """The NAS could not be reached or the connection dropped."""


class TrueNASAuthError(TrueNASError):
    """The API key was refused."""


class TrueNASCallError(TrueNASError):
    """A method call returned an error."""

    def __init__(
        self, method: str, code: int | None, message: str, reason: str | None = None
    ) -> None:
        """Store the details of the failed call."""
        self.method = method
        self.code = code
        self.reason = reason or message
        super().__init__(f"{method}: {self.reason}")


class TrueNASMethodNotFound(TrueNASCallError):
    """The method does not exist on this TrueNAS version."""


EventCallback = Callable[[str, dict[str, Any]], None]


class TrueNASClient:
    """Persistent, authenticated WebSocket connection to one TrueNAS system."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        api_key: str,
        verify_ssl: bool = False,
    ) -> None:
        """Initialise the client (no network activity yet)."""
        self._session = session
        self._host = host.strip().removeprefix("https://").removeprefix("wss://")
        self._host = self._host.removeprefix("http://").rstrip("/")
        self._api_key = api_key
        self._verify_ssl = verify_ssl
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._pending_methods: dict[int, str] = {}
        self._subscriptions: dict[str, EventCallback] = {}
        self._connect_lock = asyncio.Lock()
        self._call_sem = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        self._missing_methods: set[str] = set()

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #
    @property
    def url(self) -> str:
        """Return the WebSocket endpoint."""
        return f"wss://{self._host}/api/current"

    @property
    def connected(self) -> bool:
        """Return True when the socket is open and the reader is alive."""
        return (
            self._ws is not None
            and not self._ws.closed
            and self._reader is not None
            and not self._reader.done()
        )

    async def connect(self) -> None:
        """Open the socket, log in and restore event subscriptions."""
        async with self._connect_lock:
            if self.connected:
                return
            await self._close_socket()
            try:
                self._ws = await self._session.ws_connect(
                    self.url,
                    ssl=self._verify_ssl,
                    heartbeat=30,
                    timeout=aiohttp.ClientWSTimeout(ws_close=10),
                    max_msg_size=0,
                    autoping=True,
                )
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                raise TrueNASConnectionError(
                    f"Cannot connect to {self.url}: {err}"
                ) from err

            self._reader = asyncio.create_task(
                self._read_loop(), name=f"truenas_ws reader {self._host}"
            )

            try:
                ok = await self._raw_call(
                    "auth.login_with_api_key", [self._api_key], CONNECT_TIMEOUT
                )
            except TrueNASCallError as err:
                await self._close_socket()
                raise TrueNASAuthError(str(err)) from err
            if ok is not True:
                await self._close_socket()
                raise TrueNASAuthError("API key refused by TrueNAS")

            for name in list(self._subscriptions):
                try:
                    await self._raw_call("core.subscribe", [name], CONNECT_TIMEOUT)
                except TrueNASCallError as err:
                    _LOGGER.warning("Cannot subscribe to %s: %s", name, err)

    async def close(self) -> None:
        """Close the connection for good."""
        self._subscriptions.clear()
        await self._close_socket()

    async def _close_socket(self) -> None:
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        self._reader = None
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        self._fail_pending(TrueNASConnectionError("Connection closed"))

    def _fail_pending(self, err: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
                # Mark retrieved so a cancelled caller does not log a warning.
                fut.exception()
        self._pending.clear()
        self._pending_methods.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the reader from dying silently
            _LOGGER.exception("Unexpected error while reading from TrueNAS")
        finally:
            _LOGGER.debug("TrueNAS WebSocket reader stopped")
            self._fail_pending(TrueNASConnectionError("Connection lost"))

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON message: %.200s", raw)
            return

        msg_id = data.get("id")
        if msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            method_name = self._pending_methods.pop(msg_id, "?")
            if fut.done():
                return
            if "error" in data and data["error"] is not None:
                fut.set_exception(_build_error(method_name, data["error"]))
            else:
                fut.set_result(data.get("result"))
            return

        method = data.get("method")
        if method == "collection_update":
            params = data.get("params") or {}
            collection = str(params.get("collection", ""))
            fields = params.get("fields") or {}
            base = collection.split(":", 1)[0]
            for name, callback in self._subscriptions.items():
                if name.split(":", 1)[0] == base:
                    try:
                        callback(collection, fields)
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Error in %s event handler", collection)
        elif method == "notify_unsubscribed":
            _LOGGER.debug("TrueNAS ended subscription: %s", data.get("params"))

    # ------------------------------------------------------------------ #
    # Calls
    # ------------------------------------------------------------------ #
    async def _raw_call(
        self, method: str, params: list[Any] | None, timeout: float
    ) -> Any:
        if self._ws is None or self._ws.closed:
            raise TrueNASConnectionError("Not connected")
        msg_id = next(self._ids)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = fut
        self._pending_methods[msg_id] = method
        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params if params is not None else [],
        }
        try:
            await self._ws.send_str(json.dumps(payload))
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError as err:
            raise TrueNASConnectionError(f"{method}: timed out") from err
        except (aiohttp.ClientError, ConnectionResetError) as err:
            raise TrueNASConnectionError(f"{method}: {err}") from err
        finally:
            self._pending.pop(msg_id, None)
            self._pending_methods.pop(msg_id, None)

    async def call(
        self,
        method: str,
        params: list[Any] | None = None,
        timeout: float = CALL_TIMEOUT,
    ) -> Any:
        """Call a middleware method, reconnecting first if needed."""
        if method in self._missing_methods:
            raise TrueNASMethodNotFound(method, JSONRPC_METHOD_NOT_FOUND, "missing")
        if not self.connected:
            await self.connect()
        async with self._call_sem:
            try:
                return await self._raw_call(method, params, timeout)
            except TrueNASMethodNotFound:
                self._missing_methods.add(method)
                raise

    async def call_first(
        self,
        candidates: list[tuple[str, list[Any] | None]],
        timeout: float = CALL_TIMEOUT,
    ) -> Any:
        """Try several equivalent methods, for API differences across versions."""
        last: TrueNASError | None = None
        for method, params in candidates:
            try:
                return await self.call(method, params, timeout)
            except TrueNASMethodNotFound as err:
                last = err
        assert last is not None
        raise last

    async def call_job(
        self,
        method: str,
        params: list[Any] | None = None,
        timeout: float = 3600,
        wait: bool = True,
    ) -> Any:
        """Start a job and (optionally) wait for its result."""
        job_id = await self.call(method, params)
        if not wait or not isinstance(job_id, int):
            return job_id
        async with asyncio.timeout(timeout):
            while True:
                jobs = await self.call("core.get_jobs", [[["id", "=", job_id]]])
                if jobs:
                    job = jobs[0]
                    state = job.get("state")
                    if state in JOB_FINAL_STATES:
                        if state != "SUCCESS":
                            raise TrueNASCallError(
                                method,
                                None,
                                state,
                                job.get("error") or f"job {state.lower()}",
                            )
                        return job.get("result")
                await asyncio.sleep(2)

    async def subscribe(self, name: str, callback: EventCallback) -> None:
        """Subscribe to an event collection (re-subscribed after reconnects)."""
        self._subscriptions[name] = callback
        if not self.connected:
            await self.connect()
            return  # connect() already subscribed
        await self.call("core.subscribe", [name])


def _build_error(method: str, error: dict[str, Any]) -> TrueNASCallError:
    code = error.get("code")
    message = str(error.get("message") or "error")
    data = error.get("data") or {}
    reason = None
    if isinstance(data, dict):
        reason = data.get("reason") or data.get("errname")
    if code == JSONRPC_METHOD_NOT_FOUND or (
        reason
        and "does not exist" in str(reason).lower()
        and "method" in str(reason).lower()
    ):
        return TrueNASMethodNotFound(method, code, message, reason)
    return TrueNASCallError(method, code, message, reason)
