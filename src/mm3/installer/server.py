"""``mm3 installer serve``: HTTP/JSON on the installer socket (CORE-355).

The socket is ``/run/mm3-install/installer.sock`` (mode 0660, group ``mm3``),
in the unit's own runtime directory. Only HTTP/1.1 requests with a
``Content-Length`` body of at most 64 KiB are read, one per connection; the
one exception is ``POST /v1/backups/upload`` (CORE-361), whose archive of at
most 2 GiB is streamed to a file and never held in memory. The peer's uid
comes from ``SO_PEERCRED``: unlisted sources and the passphrase and TOTP setup
are accepted only from root (CORE-353, CORE-369); every listed operation needs
the passphrase, and the TOTP code when enabled (CORE-132). A failed
re-authentication answers 403, a locked installer 423 and a second
operation while one runs 409. ``POST /v1/system/<action>`` (restart the engine
or the kiosk, reboot, CORE-382) needs no passphrase: it answers 202, waits
:data:`~mm3.installer.system.DELAY_S` and runs the command. Bodies are never
logged, so passphrases and codes are not either.

Implements:
    CORE-355: Core spec 0.35, §10.7 Installer service.
    CORE-353: Core spec 0.33, §10.6 Module catalog.
    CORE-369: Core spec 0.37, §10.7 Installer service.
    CORE-132: Core spec 0.33, §10.7 Installer service.
    CORE-351: Core spec 0.35, §10.6 Module catalog.
    CORE-361: Core spec 0.45, §10.8 Backup and restore (installer side).
    CORE-364: Core spec 0.37, §10.8 Backup and restore.
    CORE-365: Core spec 0.37, §10.8 Backup and restore.
    CORE-382: Core spec 0.42, §9.1 Remote-control API.
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import os
import re
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from mm3.core.catalog import CatalogError
from mm3.core.logging import get_logger
from mm3.installer.age import valid_recipient
from mm3.installer.auth import Auth, AuthError, LockedError
from mm3.installer.backup import BackupError
from mm3.installer.operations import FINAL, BusyError, Installer, Operation, Request
from mm3.installer.remotes import REMOTE_NAME, RemoteError
from mm3.installer.schedule import BackupScheduler
from mm3.installer.system import ACTIONS, DELAY_S, SystemActions
from mm3.installer.uploads import MAX_UPLOAD
from mm3.installer.venv import BuildError

__all__ = ["MAX_BODY", "REFRESH_CHECK_S", "InstallerServer", "peer_uid"]

MAX_BODY = 64 * 1024
MAX_HEAD = 16 * 1024
READ_TIMEOUT_S = 30.0
REFRESH_CHECK_S = 3600.0
"""How often the index's age is checked for the daily refresh (CORE-351)."""
SOCKET_GROUP = "mm3"
_ACTIONS = {"install", "update", "remove", "restore_previous"}
_SOURCES = {"catalog", "pypi", "local"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_VERSION = re.compile(r"^[A-Za-z0-9.+!_-]{1,64}$")
_LENGTH = re.compile(r"[0-9]{1,6}")
_UPLOAD_LENGTH = re.compile(r"[0-9]{1,12}")
UPLOAD_PATH = "/v1/backups/upload"
"""The one route whose body is streamed to a file instead of read into memory (CORE-361)."""
CHUNK = 1 << 20
MAX_CLIENTS = 32
"""Connections served at once; more are closed at once (event streams count)."""
_SYSTEM_PATH = re.compile(r"^/v1/system/(" + "|".join(ACTIONS) + ")$")
_OP_PATH = re.compile(r"^/v1/operations/([0-9a-f]{16})(/events)?$")
_BACKUP_PATH = re.compile(r"^/v1/backups/([A-Za-z0-9][A-Za-z0-9._-]{0,200}\.mm3backup)$")
_REMOTE_PATH = re.compile(r"^/v1/backups/remotes/([^/]+)(/test)?$")
_PARTS = ("config", "data", "modules", "secrets")
MAX_SECRET = 16 * 1024

_log = get_logger("installer")


def peer_uid(sock: socket.socket) -> int:
    """The uid of the process on the other end of a Unix socket (SO_PEERCRED)."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return int(uid)


class HttpError(Exception):
    """An error answer: status and message."""

    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        """A machine-readable reason in the answer, such as ``busy`` or ``no_kiosk``."""


@dataclass
class HttpRequest:
    """One parsed request."""

    method: str
    path: str
    body: bytes
    uid: int
    length: int = 0
    """The announced size of a streamed body (the upload route)."""
    stream: asyncio.StreamReader | None = None
    """The connection to read a streamed body from."""

    def json(self) -> dict[str, Any]:
        """The body as a JSON object (empty body = ``{}``).

        Raises:
            HttpError: 400 when it is not a JSON object.
        """
        if not self.body:
            return {}
        try:
            value = json.loads(self.body)
        except ValueError as exc:
            raise HttpError(400, "body is not JSON") from exc
        if not isinstance(value, dict):
            raise HttpError(400, "body must be a JSON object")
        return value


Writer = Callable[[bytes], Awaitable[None]]


class InstallerServer:
    """The protocol of CORE-355 over an :class:`~mm3.installer.operations.Installer`.

    Args:
        installer: Runs operations.
        auth: Re-authentication (CORE-132, CORE-369).
        socket_gid: Group of the socket (``mm3``), or None to keep ours.
        peer: Reads the peer's uid from a connection (``SO_PEERCRED``; a fake in tests).
        scheduler: Scheduled backups (CORE-360).
        system: Restart and reboot commands (CORE-382); without it the routes answer 404.
        system_delay: Seconds between the 202 and the command (CORE-382).
    """

    def __init__(
        self,
        installer: Installer,
        auth: Auth,
        *,
        socket_gid: int | None = None,
        peer: Callable[[socket.socket], int] = peer_uid,
        scheduler: BackupScheduler | None = None,
        system: SystemActions | None = None,
        system_delay: float = DELAY_S,
    ) -> None:
        self.scheduler = scheduler
        self.system = system
        self._system_delay = system_delay
        self.installer = installer
        self.auth = auth
        self.socket_gid = socket_gid
        self._peer = peer
        self._changed = asyncio.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Future[Any]] = set()
        self._clients = 0
        installer.listeners.append(self._on_change)

    # --- change notification -------------------------------------------------------

    def _on_change(self, _op: Operation, _step: dict[str, Any] | None) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._notify()))

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    # --- serving -------------------------------------------------------------------

    async def serve(self, path: Path, *, refresh: bool = True) -> None:
        """Listen on ``path`` until cancelled."""
        self._loop = asyncio.get_running_loop()
        backups = self.installer.backups
        if backups is not None:
            # CORE-361: a temporary file left by a crash is deleted before the first upload.
            await asyncio.to_thread(backups.uploads.cleanup, crashed=True)
        await asyncio.to_thread(_prepare_socket_path, path)
        server = await asyncio.start_unix_server(self._client, path=str(path))
        os.chmod(path, 0o660)
        if self.socket_gid is not None:
            os.chown(path, -1, self.socket_gid)
        refresher = asyncio.create_task(self._refresh_loop()) if refresh else None
        scheduled = (
            asyncio.create_task(self.scheduler.run())
            if refresh and self.scheduler is not None
            else None
        )
        _log.info("installer_listening", socket=str(path))
        try:
            async with server:
                await server.serve_forever()
        finally:
            if refresher is not None:
                refresher.cancel()
            if scheduled is not None:
                scheduled.cancel()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

    async def _refresh_loop(self) -> None:
        while True:
            if self.installer.store.due():
                try:
                    meta = await asyncio.to_thread(self.installer.store.refresh)
                    _log.info("catalog_refreshed", serial=meta.serial)
                except CatalogError as exc:
                    _log.warning("catalog_refresh_failed", reason=str(exc))
            await asyncio.sleep(REFRESH_CHECK_S)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._clients >= MAX_CLIENTS:
            writer.close()
            return
        self._clients += 1
        sock = writer.get_extra_info("socket")
        try:
            uid = self._peer(sock)
            request = await asyncio.wait_for(self._read(reader, uid), READ_TIMEOUT_S)
            await self.dispatch(request, writer)
        except HttpError as exc:
            body = {"error": str(exc)}
            if exc.code is not None:
                body["code"] = exc.code
            await self._send(writer, exc.status, body)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            _log.exception("installer_request_crashed")
            with contextlib.suppress(ConnectionError):
                await self._send(writer, 500, {"error": "internal error"})
        finally:
            self._clients -= 1
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()

    @staticmethod
    async def _read(reader: asyncio.StreamReader, uid: int) -> HttpRequest:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as exc:
            raise HttpError(431, "headers too large") from exc
        if len(head) > MAX_HEAD:
            raise HttpError(431, "headers too large")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError as exc:
            raise HttpError(400, "bad request line") from exc
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if "transfer-encoding" in headers:
            raise HttpError(411, "send a Content-Length body")
        length = headers.get("content-length", "0")
        if method.upper() == "POST" and target.split("?", 1)[0] == UPLOAD_PATH:
            # CORE-361: the archive is streamed to a file; nothing is read here.
            if not _UPLOAD_LENGTH.fullmatch(length) or int(length) == 0:
                raise HttpError(400, "send a Content-Length body")
            if int(length) > MAX_UPLOAD:
                raise HttpError(413, "the archive is larger than 2 GiB", code="too_large")
            return HttpRequest("POST", UPLOAD_PATH, b"", uid, int(length), reader)
        if not _LENGTH.fullmatch(length):
            raise HttpError(400, "bad Content-Length")
        if int(length) > MAX_BODY:
            raise HttpError(413, "body too large")
        body = await reader.readexactly(int(length)) if int(length) else b""
        return HttpRequest(method.upper(), target.split("?", 1)[0], body, uid)

    @staticmethod
    async def _send(
        writer: asyncio.StreamWriter, status: int, body: Any, content_type: str = "application/json"
    ) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        reason = HTTPStatus(status).phrase
        head = (
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode() + data)
        await writer.drain()

    # --- routes --------------------------------------------------------------------

    async def dispatch(self, req: HttpRequest, writer: asyncio.StreamWriter) -> None:
        """Answer one request.

        Raises:
            HttpError: An error answer.
        """
        route = (req.method, req.path)
        if route == ("GET", "/v1/status"):
            await self._send(writer, 200, await asyncio.to_thread(self.status))
        elif route == ("POST", "/v1/catalog/refresh"):
            try:
                meta = await asyncio.to_thread(self.installer.store.refresh)
            except CatalogError as exc:
                await self._send(writer, 502, {"error": str(exc)})
                return
            await self._send(writer, 200, {"catalog": meta.to_json()})
        elif route == ("POST", "/v1/operations"):
            op = await self._submit(req)
            await self._send(writer, 202, {"id": op.id})
        elif route == ("GET", "/v1/operations"):
            ops = [o.public() for o in self.installer.recent()]
            await self._send(writer, 200, {"operations": ops})
        elif (m := _SYSTEM_PATH.match(req.path)) and req.method == "POST":
            await self._system_action(m.group(1), req, writer)
        elif req.path == "/v1/backups" or req.path.startswith("/v1/backups/"):
            await self._backups(req, writer)
        elif req.path.startswith("/v1/auth/"):
            await self._auth_route(req, writer)
        elif (m := _OP_PATH.match(req.path)) and req.method == "GET":
            found = self.installer.get(m.group(1))
            if found is None:
                raise HttpError(404, "no such operation")
            if m.group(2):
                await self._events(found.id, writer)
            else:
                await self._send(writer, 200, found.public())
        else:
            raise HttpError(404, f"no route {req.method} {req.path}")

    async def _system_action(
        self, action: str, req: HttpRequest, writer: asyncio.StreamWriter
    ) -> None:
        """``POST /v1/system/<action>``: 202 now, the command after a short delay (CORE-382)."""
        if self.system is None:
            raise HttpError(404, "restart and reboot are not set up")
        client = req.json().get("client", "")
        if not isinstance(client, str) or len(client) > 200:
            raise HttpError(400, "client must be a short string")
        client = client or ("root" if req.uid == 0 else f"uid {req.uid}")
        if action == "restart-kiosk" and not await asyncio.to_thread(self.system.kiosk_installed):
            raise HttpError(409, "mm3-kiosk.service is not installed", "no_kiosk")
        try:
            self.installer.reserve_system(action)
        except BusyError as exc:
            raise HttpError(409, str(exc), "busy") from exc
        requested_at = datetime.now(UTC).isoformat()
        task = asyncio.ensure_future(self._run_system(action, client))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await self._send(writer, 202, {"action": action, "requested_at": requested_at})

    async def _run_system(self, action: str, client: str) -> None:
        system = self.system
        assert system is not None  # noqa: S101 - only started when set
        try:
            await asyncio.sleep(self._system_delay)
            _log.warning("system_action", action=action, client=client)
            if not await asyncio.to_thread(system.run, action):
                _log.error("system_action_failed", action=action, client=client)
        finally:
            self.installer.release_system()

    def status(self) -> dict[str, Any]:
        """``GET /v1/status`` (CORE-355)."""
        meta = self.installer.store.meta()
        index = self.installer.store.kept()
        current = self.installer.builder.current()
        try:
            installed = self.installer.installed(index)
        except BuildError as exc:
            _log.warning("installer_list_failed", reason=str(exc))
            installed = []
        until = self.auth.locked_until()
        return {
            "busy": self.installer.busy,
            "locked_until": until.isoformat() if until else None,
            "previous": [p.name for p in self.installer.builder.kept() if p != current],
            "installed": installed,
            "catalog": meta.to_json() if meta else None,
            "backup": self.installer.backups.status.read() if self.installer.backups else {},
        }

    async def _submit(self, req: HttpRequest) -> Operation:
        body = req.json()
        request = self._request(body, req.uid)
        # Listed operations always re-authenticate, root included; unlisted
        # ones are root-only and need no passphrase (CORE-353).
        if request.source == "catalog":
            await self._check_auth(body)
        return self._start(request)

    def _start(self, request: Request) -> Operation:
        if self.installer.busy:
            raise HttpError(409, "another operation is running")
        try:
            op = self.installer.submit(request)
        except BusyError as exc:
            raise HttpError(409, str(exc)) from exc
        future = asyncio.get_running_loop().run_in_executor(None, self.installer.run, op, request)
        self._tasks.add(future)
        future.add_done_callback(self._tasks.discard)
        return op

    async def _check_auth(self, body: dict[str, Any]) -> None:
        passphrase = body.get("passphrase")
        totp = body.get("totp")
        if passphrase is not None and not isinstance(passphrase, str):
            raise HttpError(400, "passphrase must be a string")
        if totp is not None and not isinstance(totp, str):
            raise HttpError(400, "totp must be a string")
        try:
            await asyncio.to_thread(self.auth.check, passphrase, totp)
        except LockedError as exc:
            raise HttpError(423, str(exc)) from exc
        except AuthError as exc:
            raise HttpError(403, str(exc)) from exc

    @staticmethod
    def _request(body: dict[str, Any], uid: int) -> Request:
        action = body.get("action")
        if action not in _ACTIONS:
            raise HttpError(400, f"action must be one of {sorted(_ACTIONS)}")
        source = body.get("source", "catalog")
        if source not in _SOURCES:
            raise HttpError(400, f"source must be one of {sorted(_SOURCES)}")
        name = body.get("name")
        version = body.get("version")
        path = body.get("path")
        client = body.get("client", "")
        if not isinstance(client, str) or len(client) > 200:
            raise HttpError(400, "client must be a short string")
        if action == "restore_previous":
            if name is not None or version is not None or source != "catalog":
                raise HttpError(400, "restore_previous takes no name, version or source")
        else:
            if not isinstance(name, str) or not _NAME.match(name):
                raise HttpError(400, "name must be a package name")
        if version is not None and (
            action != "install" or not isinstance(version, str) or not _VERSION.match(version)
        ):
            raise HttpError(400, "version is only for install and must be a version")
        if source != "catalog":
            if uid != 0:
                raise HttpError(403, "unlisted packages and local wheels need root (sudo)")
            if action not in ("install", "update"):
                raise HttpError(400, "source is only for install and update")
        if source == "local":
            if (
                not isinstance(path, str)
                or not path.endswith(".whl")
                or not Path(path).is_absolute()
            ):
                raise HttpError(400, "path must be the absolute path of a wheel file")
        elif path is not None:
            raise HttpError(400, "path is only for source local")
        return Request(
            action=action,
            name=name,
            version=version,
            source=source,
            path=path,
            client=client or ("root" if uid == 0 else f"uid {uid}"),
            peer_uid=uid,
        )

    # --- backups (CORE-361, CORE-364, CORE-365) --------------------------------------

    def _runner(self) -> Any:
        runner = self.installer.backups
        if runner is None:
            raise HttpError(404, "backups are not set up")
        return runner

    @staticmethod
    def _text(body: dict[str, Any], key: str, limit: int = MAX_SECRET) -> str | None:
        value = body.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > limit:
            raise HttpError(400, f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _parts(body: dict[str, Any]) -> list[str] | None:
        parts = body.get("parts")
        if parts is None:
            return None
        if (
            not isinstance(parts, list)
            or not parts
            or any(p not in _PARTS for p in parts)
            or len(set(parts)) != len(parts)
        ):
            raise HttpError(400, f"parts must be a non-empty list of {', '.join(_PARTS)}")
        return list(parts)

    def _source(self, body: dict[str, Any], uid: int) -> dict[str, Any]:
        name = self._text(body, "name", 256)
        path = self._text(body, "path", 1024)
        upload = self._text(body, "upload", 64)
        if upload is not None:
            return {"upload": upload}
        if path is not None:
            if uid != 0:
                raise HttpError(403, "a backup file by path needs root (sudo)")
            if not Path(path).is_absolute():
                raise HttpError(400, "path must be absolute")
            return {"path": path}
        if name is None:
            raise HttpError(400, "name (or path, for root) is required")
        remote = body.get("remote", False)
        if not isinstance(remote, bool):
            raise HttpError(400, "remote must be true or false")
        return {"name": name, "remote": remote}

    async def _backups(self, req: HttpRequest, writer: asyncio.StreamWriter) -> None:
        runner = self._runner()
        route = (req.method, req.path)
        try:
            if route == ("GET", "/v1/backups"):
                entries = await asyncio.to_thread(runner.service.listing)
                await self._send(
                    writer,
                    200,
                    {"backups": [e.public() for e in entries], "status": runner.status.read()},
                )
            elif route == ("POST", "/v1/backups"):
                await self._create_backup(req, writer)
            elif route == ("POST", UPLOAD_PATH):
                await self._upload(req, writer)
            elif route == ("GET", "/v1/backups/remote"):
                listing = await asyncio.to_thread(runner.remote_listing)
                await self._send(writer, 200, {"backups": listing})
            elif route == ("POST", "/v1/backups/keygen"):
                keep = req.json().get("keep", False)
                if not isinstance(keep, bool):
                    raise HttpError(400, "keep must be true or false")
                made = await asyncio.to_thread(runner.keygen, keep=keep)
                await self._send(writer, 200, {**made, "kept": keep})
            elif route in (("POST", "/v1/backups/verify"), ("POST", "/v1/backups/preview")):
                await self._inspect(req, writer, preview=req.path.endswith("preview"))
            elif route == ("POST", "/v1/backups/restore"):
                await self._restore(req, writer)
            elif req.path == "/v1/backups/remotes" and req.method == "GET":
                await self._send(
                    writer, 200, {"remotes": await asyncio.to_thread(runner.remotes.listing)}
                )
            elif (m := _REMOTE_PATH.match(req.path)) is not None:
                await self._remote(req, writer, m.group(1), bool(m.group(2)))
            elif (m := _BACKUP_PATH.match(req.path)) is not None and req.method == "GET":
                await self._download(m.group(1), writer)
            else:
                raise HttpError(404, f"no route {req.method} {req.path}")
        except RemoteError as exc:
            raise HttpError(502 if "rclone" in str(exc) else 400, str(exc)) from exc
        except BackupError as exc:
            raise HttpError(400, str(exc)) from exc

    async def _upload(self, req: HttpRequest, writer: asyncio.StreamWriter) -> None:
        """Stage an uploaded archive; no re-authentication, it only waits for a restore (CORE-361).

        The body is written to a temporary file as it arrives, so 2 GiB never sit in memory;
        a connection that stalls for :data:`READ_TIMEOUT_S` or ends early leaves nothing.
        """
        if req.stream is None:
            raise HttpError(400, "send a Content-Length body")
        staging = await asyncio.to_thread(self._runner().uploads.begin)
        try:
            left = req.length
            while left:
                chunk = await asyncio.wait_for(req.stream.read(min(CHUNK, left)), READ_TIMEOUT_S)
                if not chunk:
                    raise HttpError(400, "the upload ended before Content-Length bytes")
                await asyncio.to_thread(staging.write, chunk)
                left -= len(chunk)
            upload_id = await asyncio.to_thread(staging.commit)
        except BaseException:
            await asyncio.to_thread(staging.abort)
            raise
        _log.info("installer_backup_uploaded", size=staging.size, client_uid=req.uid)
        await self._send(writer, 201, {"upload": upload_id, "size": staging.size})

    async def _create_backup(self, req: HttpRequest, writer: asyncio.StreamWriter) -> None:
        body = req.json()
        recipient = self._text(body, "recipient", 100)
        if recipient is not None and not valid_recipient(recipient):
            raise HttpError(400, "recipient must be an age public key (age1...)")
        passphrase = self._text(body, "archive_passphrase", 1024)
        if recipient and passphrase:
            raise HttpError(400, "encrypt to a key or with a passphrase, not both")
        configured = body.get("use_configured_key", False)
        if not isinstance(configured, bool):
            raise HttpError(400, "use_configured_key must be true or false")
        pre = body.get("pre", False)
        if not isinstance(pre, bool):
            raise HttpError(400, "pre must be true or false")
        if pre and req.uid != 0:
            raise HttpError(403, "a pre-operation backup on request needs root (sudo)")
        options = {
            "pre": pre,
            "parts": self._parts(body),
            "recipient": recipient,
            "archive_passphrase": passphrase,
            "use_configured_key": configured,
        }
        request = Request(
            action="backup",
            client=str(body.get("client") or ("root" if req.uid == 0 else f"uid {req.uid}"))[:200],
            peer_uid=req.uid,
            options=options,
        )
        op = self._start(request)
        await self._send(writer, 202, {"id": op.id})

    async def _inspect(
        self, req: HttpRequest, writer: asyncio.StreamWriter, *, preview: bool
    ) -> None:
        body = req.json()
        source = self._source(body, req.uid)
        instance = self._text(body, "instance", 64)
        installed: list[dict[str, str]] = []
        if preview:
            try:
                installed = await asyncio.to_thread(
                    self.installer.installed, self.installer.store.kept()
                )
            except BuildError:
                installed = []
        result = await asyncio.to_thread(
            lambda: self._runner().inspect(
                source,
                self._text(body, "key"),
                parts=self._parts(body),
                instance=instance,
                installed=installed,
                preview=preview,
            )
        )
        await self._send(writer, 200, result)

    async def _restore(self, req: HttpRequest, writer: asyncio.StreamWriter) -> None:
        body = req.json()
        source = self._source(body, req.uid)
        # A restore always re-authenticates, except from a root peer (CORE-361).
        if req.uid != 0:
            await self._check_auth(body)
        options = {
            "source": source,
            "parts": self._parts(body),
            "instance": self._text(body, "instance", 64),
            "key": self._text(body, "key"),
        }
        request = Request(
            action="restore",
            client=str(body.get("client") or ("root" if req.uid == 0 else f"uid {req.uid}"))[:200],
            peer_uid=req.uid,
            options=options,
        )
        op = self._start(request)
        await self._send(writer, 202, {"id": op.id})

    async def _remote(
        self, req: HttpRequest, writer: asyncio.StreamWriter, name: str, test: bool
    ) -> None:
        remotes = self._runner().remotes
        if not REMOTE_NAME.match(name):
            raise HttpError(400, "not a remote name")
        if test and req.method == "POST":
            path = self._text(req.json(), "path", 1024) or ""
            await asyncio.to_thread(remotes.test, name, path)
            await self._send(writer, 204, None)
        elif not test and req.method == "GET":
            try:
                found = await asyncio.to_thread(remotes.get, name)
            except RemoteError as exc:
                raise HttpError(404, str(exc)) from exc
            await self._send(writer, 200, found)
        elif not test and req.method == "PUT":
            body = req.json()
            await self._check_auth(body)
            kind, params = body.get("type"), body.get("params")
            if not isinstance(kind, str) or not isinstance(params, dict):
                raise HttpError(400, "type (string) and params (object) are required")
            await asyncio.to_thread(remotes.put, name, kind, params)
            await self._send(writer, 204, None)
        elif not test and req.method == "DELETE":
            await self._check_auth(req.json())
            try:
                await asyncio.to_thread(remotes.delete, name)
            except RemoteError as exc:
                raise HttpError(404, str(exc)) from exc
            await self._send(writer, 204, None)
        else:
            raise HttpError(404, f"no route {req.method} {req.path}")

    async def _download(self, name: str, writer: asyncio.StreamWriter) -> None:
        """Stream an archive; a download needs no re-authentication (CORE-361)."""
        try:
            _entry, path = await asyncio.to_thread(self._runner().service.find, name)
        except BackupError as exc:
            raise HttpError(404, str(exc)) from exc
        size = path.stat().st_size
        writer.write(
            (
                "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                f"Content-Length: {size}\r\n"
                f'Content-Disposition: attachment; filename="{name}"\r\n'
                "Connection: close\r\n\r\n"
            ).encode()
        )
        with path.open("rb") as fh:
            while chunk := await asyncio.to_thread(fh.read, 1 << 20):
                writer.write(chunk)
                await writer.drain()

    async def _auth_route(self, req: HttpRequest, writer: asyncio.StreamWriter) -> None:
        if req.uid != 0:
            raise HttpError(403, "the passphrase and TOTP are set only by root (sudo)")
        body = req.json()
        if (req.method, req.path) == ("POST", "/v1/auth/passphrase"):
            passphrase = body.get("passphrase")
            if not isinstance(passphrase, str):
                raise HttpError(400, "passphrase must be a string")
            try:
                await asyncio.to_thread(self.auth.set_passphrase, passphrase)
            except ValueError as exc:
                raise HttpError(400, str(exc)) from exc
            _log.info("installer_passphrase_set")
            await self._send(writer, 204, None)
        elif (req.method, req.path) == ("POST", "/v1/auth/totp"):
            code = body.get("code")
            if code is None:
                setup = await asyncio.to_thread(self.auth.begin_totp)
                await self._send(writer, 200, {"secret": setup.secret, "uri": setup.uri})
                return
            if not isinstance(code, str):
                raise HttpError(400, "code must be a string")
            try:
                await asyncio.to_thread(self.auth.confirm_totp, code)
            except AuthError as exc:
                raise HttpError(403, str(exc)) from exc
            _log.info("installer_totp_enabled")
            await self._send(writer, 204, None)
        elif (req.method, req.path) == ("DELETE", "/v1/auth/totp"):
            await asyncio.to_thread(self.auth.disable_totp)
            _log.info("installer_totp_disabled")
            await self._send(writer, 204, None)
        else:
            raise HttpError(404, f"no route {req.method} {req.path}")

    async def _events(self, op_id: str, writer: asyncio.StreamWriter) -> None:
        """Stream the operation's steps as NDJSON until it ends."""
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nConnection: close\r\n\r\n"
        )
        path = self.installer.events_path(op_id)
        offset = 0
        while True:
            try:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except FileNotFoundError:
                chunk = b""
            if chunk:
                complete = chunk[: chunk.rfind(b"\n") + 1]
                offset += len(complete)
                writer.write(complete)
                await writer.drain()
            current = self.installer.get(op_id)
            if current is None or (current.state in FINAL and not chunk):
                return
            async with self._changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), 5.0)


def _prepare_socket_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def group_id(name: str = SOCKET_GROUP) -> int | None:
    """The gid of ``name``, or None when there is no such group."""
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None
