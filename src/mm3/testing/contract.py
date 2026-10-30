"""``check_contract()``: the test-kit rows of Module Contract section 11.

Every check runs on fresh instances built from the harness arguments, with
every ``SecretStr`` field of the configuration replaced by a random canary
that must never leave the instance (MOD-16, MOD-42, MOD-83). A standard
workload exercises each instance: every consumed notification with a sample
payload, every answered request, 15 virtual minutes, one hide and show.

Implements:
    MOD-06: Module Contract 1.0 rev 9, §2 Package and metadata.
    MOD-10, MOD-11, MOD-13: Module Contract 1.0 rev 9, §3.3 Rules.
    MOD-14, MOD-15, MOD-16: Module Contract 1.0 rev 9, §3.3 Rules.
    MOD-24, MOD-25, MOD-26, MOD-27: Module Contract 1.0 rev 9, §6.1 Messaging.
    MOD-29, MOD-42, MOD-43: Module Contract 1.0 rev 9, §6.1 Messaging.
    MOD-70, MOD-72: Module Contract 1.0 rev 9, §8 Capabilities and grants.
    MOD-83: Module Contract 1.0 rev 9, §9 Logging, health and errors.
    -: Module Contract 1.0 rev 9, §11 Compliance checklist.
    -: Module Contract 1.0 rev 9, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import secrets
import sys
import time
import typing
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, SecretStr

from mm3.core.supervisor import State
from mm3.errors import RemoteError, RequestTimeoutError
from mm3.messages import MAX_PAYLOAD_BYTES, Notification
from mm3.notifications import declaration_problems

if TYPE_CHECKING:
    from mm3.module import MirrorModule
    from mm3.testing.harness import ModuleHarness

__all__ = [
    "KIT_CHECKS",
    "CheckResult",
    "ContractError",
    "run_kit_checks",
    "sample_payload",
]

KIT_CHECKS: tuple[str, ...] = (
    "MOD-06",
    "MOD-10",
    "MOD-11",
    "MOD-13",
    "MOD-14",
    "MOD-15",
    "MOD-16",
    "MOD-24",
    "MOD-25",
    "MOD-26",
    "MOD-27",
    "MOD-29",
    "MOD-42",
    "MOD-43",
    "MOD-70",
    "MOD-72",
    "MOD-83",
)
"""Section 11 rows checked by the test kit, in report order."""

SLOW_CALLBACK_S = 0.1
"""A callback running longer blocks the loop (MOD-11)."""

SUSPEND_CYCLES = 20
"""Hide/show cycles of MOD-27."""

SCOPED_CAPABILITIES = frozenset({"http.outbound", "mqtt.publish", "mqtt.subscribe"})
"""Capabilities whose grants MOD-72 withholds."""

UNHANDLED_REQUEST = "MM3_KIT_UNHANDLED"
"""A request no module answers (MOD-24)."""

_UTC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


@dataclass
class CheckResult:
    """The outcome of one section 11 row.

    Attributes:
        id: MOD id.
        level: ``MUST`` or ``SHOULD``.
        status: ``pass``, ``fail`` or ``warn``.
        findings: What failed or needs attention.
        category: Always ``kit`` here (``mm3 module check`` adds the others).
    """

    id: str
    level: Literal["MUST", "SHOULD"] = "MUST"
    status: Literal["pass", "fail", "warn"] = "pass"
    findings: list[str] = field(default_factory=list)
    category: str = "kit"

    def fail(self, finding: str) -> None:
        """Record a finding that fails the check."""
        self.status = "fail"
        self.findings.append(finding)


class ContractError(AssertionError):
    """``check_contract()`` found MUST failures.

    Args:
        failed: The failed checks.
        notes: Extra lines for the report (e.g. unmocked HTTP calls).

    Attributes:
        failed: The failed checks.
        ids: Their MOD ids.
    """

    def __init__(self, failed: list[CheckResult], notes: list[str] | None = None) -> None:
        self.failed = failed
        self.ids = [r.id for r in failed]
        lines = [f"{r.id}: {f}" for r in failed for f in r.findings or ["failed"]]
        lines += list(notes or [])
        super().__init__("contract checks failed: " + ", ".join(self.ids) + "\n" + "\n".join(lines))


# --- sample payloads -----------------------------------------------------------


def _sample(schema: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 8:
        return None
    if "$ref" in schema:
        return _sample(defs.get(schema["$ref"].rsplit("/", 1)[-1], {}), defs, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    for key in ("anyOf", "oneOf", "allOf"):
        options = [o for o in schema.get(key, []) if o.get("type") != "null"]
        if options:
            return _sample(options[0], defs, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-01-01T12:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        if fmt in ("uri", "url"):
            return "https://example.com/"
        return "x" * max(int(schema.get("minLength", 1)), 1)
    if kind == "integer":
        return int(schema.get("minimum", schema.get("exclusiveMinimum", 0) + 1))
    if kind == "number":
        return float(schema.get("minimum", schema.get("exclusiveMinimum", 0) + 1))
    if kind == "boolean":
        return True
    if kind == "array":
        item = _sample(schema.get("items", {}), defs, depth + 1)
        return [item] * int(schema.get("minItems", 1))
    if kind == "object" or "properties" in schema:
        return {
            name: _sample(sub, defs, depth + 1)
            for name, sub in schema.get("properties", {}).items()
        }
    return None


def sample_payload(model: type[BaseModel]) -> dict[str, Any] | None:
    """Build a payload that validates against ``model``, with every field set.

    Args:
        model: Payload model.

    Returns:
        The payload, or ``None`` when no valid sample could be built.
    """
    schema = model.model_json_schema()
    value = _sample(schema, schema.get("$defs", {}))
    if not isinstance(value, dict):
        value = {}
    try:
        model.model_validate(value)
    except Exception:
        return None
    return value


def _optional_fields(model: type[BaseModel]) -> list[str]:
    return [name for name, f in model.model_fields.items() if not f.is_required()]


# --- configuration with canaries ----------------------------------------------


def _is_secret(annotation: Any) -> bool:
    if annotation is SecretStr:
        return True
    return any(_is_secret(a) for a in typing.get_args(annotation))


def canary_config(
    module: type[MirrorModule], config: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    """Fill every ``SecretStr`` field of the configuration with a canary.

    Args:
        module: The module class.
        config: The harness configuration.

    Returns:
        The configuration and the canary.
    """
    canary = "mm3canary" + secrets.token_hex(8)
    out = dict(config)
    for name, info in module.config_model.model_fields.items():
        if _is_secret(info.annotation):
            out[info.alias or name] = canary
    return out, canary


# --- runtime probes ------------------------------------------------------------


class _Audit:
    """Records file writes and I/O seen by ``sys.addaudithook`` (MOD-06, MOD-10).

    The audit hook cannot be removed, so one is installed per process and
    only records while a probe is active.
    """

    installed = False
    probe: _Audit | None = None

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.init_io: list[str] = []
        self.in_init = False

    @classmethod
    def install(cls) -> None:
        if not cls.installed:
            sys.addaudithook(cls._hook)
            cls.installed = True

    @staticmethod
    def _hook(event: str, args: tuple[Any, ...]) -> None:
        probe = _Audit.probe
        if probe is None:
            return
        with contextlib.suppress(Exception):  # an audit hook must never raise
            probe._event(event, args)

    def _event(self, event: str, args: tuple[Any, ...]) -> None:
        if self.in_init and (
            event == "open"
            or event.startswith(("socket.", "subprocess.", "os.exec", "os.posix_spawn"))
            or event in ("os.system", "os.spawn")
        ):
            self.init_io.append(f"{event} {args[0] if args else ''}".strip())
        path: Any = None
        if event == "open":
            target, mode, flags = [*args, None, None, None][:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int)
                and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
            )
            if writing:
                path = target
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.rename", "shutil.rmtree"):
            path = args[0] if args else None
        if path is None or isinstance(path, int):
            return
        text = os.fsdecode(path)
        if text.endswith(".pyc") or "__pycache__" in text or text.startswith(("/dev/", "/proc/")):
            return
        self.writes.append(text)

    @contextlib.contextmanager
    def active(self) -> Iterator[_Audit]:
        self.install()
        previous, _Audit.probe = _Audit.probe, self
        try:
            yield self
        finally:
            _Audit.probe = previous


class _SlowCallbacks:
    """Times every event-loop callback in real time (MOD-11).

    asyncio's debug mode measures with the loop clock, which the harness
    makes virtual, so the callbacks are timed here with ``perf_counter``.
    """

    def __init__(self) -> None:
        self.slow: list[str] = []

    @contextlib.contextmanager
    def active(self) -> Iterator[_SlowCallbacks]:
        handle_cls = asyncio.events.Handle
        original = handle_cls._run
        slow = self.slow

        def timed(handle: asyncio.events.Handle) -> None:
            started = time.perf_counter()
            try:
                original(handle)
            finally:
                spent = time.perf_counter() - started
                if spent > SLOW_CALLBACK_S:
                    slow.append(f"{handle!r} took {spent * 1000:.0f} ms")

        handle_cls._run = timed  # type: ignore[method-assign,assignment]
        try:
            yield self
        finally:
            handle_cls._run = original  # type: ignore[method-assign]


def _module_dir(module: type[MirrorModule]) -> Path:
    return Path(inspect.getfile(module)).resolve().parent


def _mutable_globals(module: type[MirrorModule]) -> dict[str, str]:
    """``repr`` of the mutable containers at module and class level (MOD-15)."""
    found: dict[str, str] = {}
    mod = sys.modules.get(module.__module__)
    spaces: list[tuple[str, Mapping[str, Any]]] = [(module.__qualname__, vars(module))]
    if mod is not None:
        spaces.append((module.__module__, vars(mod)))
    for prefix, space in spaces:
        for name, value in space.items():
            if name.startswith("__"):
                continue
            if isinstance(value, list | dict | set | bytearray):
                with contextlib.suppress(Exception):
                    found[f"{prefix}.{name}"] = repr(value)
    return found


@contextlib.contextmanager
def _patched(obj: Any, name: str, value: Any) -> Iterator[None]:
    had = name in vars(obj)
    old = vars(obj).get(name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if had:
            setattr(obj, name, old)
        else:
            delattr(obj, name)


# --- the run -------------------------------------------------------------------


class _Run:
    """One kit run: the original harness arguments, the canary and the results."""

    def __init__(self, base: ModuleHarness) -> None:
        self.base = base
        self.module = base.module
        self.config, self.canary = canary_config(base.module, base.config)
        self.results = {mid: CheckResult(mid) for mid in KIT_CHECKS}
        self.notes: list[str] = []
        self.requests_seen = 0
        self.failed_alone = False
        self._count = 0

    def result(self, mid: str) -> CheckResult:
        return self.results[mid]

    def harness(self, **overrides: Any) -> ModuleHarness:
        from mm3.testing.harness import ModuleHarness

        self._count += 1
        base = self.base
        kwargs: dict[str, Any] = {
            "config": self.config,
            "grants": base.grants,
            "instance_id": f"{base.module.name}_kit{self._count}",
            "locale": base.locale,
            "timezone": base.timezone,
            "units": base.units,
            "pages": base._pages.infos,
            "page": base._pages.page,
        }
        kwargs.update(overrides)
        fresh = ModuleHarness(base.module, **kwargs)
        fresh.http.copy_mocks(base.http)  # the test's canned answers apply too
        fresh._responders = dict(base._responders)
        return fresh

    @contextlib.asynccontextmanager
    async def open(self, h: ModuleHarness) -> AsyncIterator[ModuleHarness]:
        """Open a harness, collecting what every run checks on exit."""
        try:
            async with h:
                yield h
                self._collect_live(h)
        finally:
            self._collect(h)

    def _collect_live(self, h: ModuleHarness) -> None:
        obj = h.instance
        if obj is None:
            return
        r = self.result("MOD-16")
        try:
            snap = obj.snapshot()
            text = json.dumps(snap, allow_nan=False)
        except Exception as exc:
            r.fail(f"snapshot() is not JSON-serialisable: {type(exc).__name__}: {exc}")
            return
        if len(text.encode()) > MAX_PAYLOAD_BYTES:
            r.fail(f"snapshot() is {len(text.encode())} bytes, limit {MAX_PAYLOAD_BYTES}")
        if self.canary in text:
            r.fail("snapshot() contains a secret configuration value")

    def _collect(self, h: ModuleHarness) -> None:
        for v in h.violations:
            self.result("MOD-29").fail(v)
        for cap in sorted(set(h.denied)):
            self.result("MOD-70").fail(f"uses capability {cap} without declaring it")
        self.requests_seen += len(h.requests_sent)
        r42 = self.result("MOD-42")
        outgoing = [(n.name, n.payload) for n in h.notifications]
        outgoing += [(f"client:{c.type}", c.payload) for c in h.client_messages]
        for name, payload in outgoing:
            text = json.dumps(payload, default=str)
            if len(text.encode()) > MAX_PAYLOAD_BYTES:
                r42.fail(f"{name} payload is larger than 256 KiB")
            if self.canary in text:
                r42.fail(f"{name} payload contains a secret configuration value")
            for value in _strings(payload):
                if _UTC_TIME.match(value) and not value.endswith(("Z", "+00:00")):
                    r42.fail(f"{name} carries the time {value!r}, not ISO 8601 UTC")
        r83 = self.result("MOD-83")
        for record in h.logs.at_least("info"):
            if self.canary in json.dumps(record, default=str):
                r83.fail(f"log event {record.get('event')!r} at {record.get('level')} has a secret")
        for call in h.http.unmocked:
            self.notes.append(f"unmocked HTTP call: {call.method} {call.url}")
        if _failed(h):
            self.notes.append(f"{h.instance_id} failed: {h.managed.reason}")


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _failed(h: ModuleHarness) -> bool:
    try:
        inst = h.managed
    except Exception:
        return False
    return inst.state in (State.FAILED, State.DISABLED) or len(inst.failures) > 0


async def _exercise(h: ModuleHarness) -> None:
    """The standard workload."""
    for name, model in dict(h.module.consumes).items():
        payload = sample_payload(model)
        if payload is not None:
            with contextlib.suppress(ValueError):
                await h.notify(name, payload)
    for name, (req_model, _) in dict(h.module.answers).items():
        payload = sample_payload(req_model)
        if payload is None:
            continue
        with contextlib.suppress(RequestTimeoutError, RemoteError, ValueError):
            await h.request(name, payload)
    for _ in range(15):
        await h.advance(minutes=1)
    if not _failed(h):
        await h.suspend()
        await h.resume()


# --- the checks ----------------------------------------------------------------


async def _main_run(run: _Run) -> None:
    """MOD-06, 10, 11, 13 (stop twice), 24, 25, 29, 42, 83 on one instance."""
    module = run.module
    h = run.harness()
    allowed: Path | None = None
    audit = _Audit()
    slow = _SlowCallbacks()
    original_init = module.__init__
    original_request = module.on_request

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        audit.in_init = True
        before = set(asyncio.all_tasks())
        try:
            original_init(self, *args, **kwargs)
        finally:
            audit.in_init = False
        started = set(asyncio.all_tasks()) - before
        if started:
            run.result("MOD-10").fail(
                f"__init__ started tasks: {sorted(t.get_name() for t in started)}"
            )

    in_request = {"active": False}

    async def on_request(self: Any, req: Any) -> Any:
        in_request["active"] = True
        try:
            return await original_request(self, req)
        finally:
            in_request["active"] = False

    with (
        audit.active(),
        slow.active(),
        _patched(module, "__init__", init),
        _patched(module, "on_request", on_request),
    ):
        async with run.open(h):
            allowed = h.storage_dir.parent.resolve()
            ctx = h.managed.context
            if ctx is not None:
                original_ask = ctx.request

                async def ask(*args: Any, **kwargs: Any) -> Any:
                    if in_request["active"]:
                        run.result("MOD-25").fail("on_request sends a request itself")
                    return await original_ask(*args, **kwargs)

                ctx.request = ask  # type: ignore[method-assign]
            await _exercise(h)
            run.failed_alone = _failed(h)
            if _failed(h) and h.requests_sent:
                run.result("MOD-26").fail(
                    f"stops working when its requests get no reply: {h.managed.reason}"
                )
            # MOD-24: an undeclared request gets None, i.e. no reply.
            try:
                await h.request(UNHANDLED_REQUEST, {})
                run.result("MOD-24").fail(f"on_request answered {UNHANDLED_REQUEST}")
            except RequestTimeoutError:
                pass
            except RemoteError as exc:
                run.result("MOD-24").fail(
                    f"on_request raised for an undeclared request ({exc.code}) instead of None"
                )
            # MOD-13: stop() twice while the context is open.
            obj = h.instance
            if obj is not None:
                try:
                    await h.clock.drive(obj.stop())
                    await h.clock.drive(obj.stop())
                except Exception as exc:
                    run.result("MOD-13").fail(
                        f"stop() is not idempotent: {type(exc).__name__}: {exc}"
                    )
    for line in audit.init_io:
        run.result("MOD-10").fail(f"__init__ does I/O: {line}")
    for path in _outside(audit.writes, allowed):
        run.result("MOD-06").fail(f"writes outside the storage directory: {path}")
    for line in slow.slow:
        run.result("MOD-11").fail(f"blocks the event loop: {line}")
    for problem in declaration_problems(module):
        run.result("MOD-29").fail(problem)
    # MOD-13: nothing of the module's keeps running after stop().
    await asyncio.sleep(0)
    left = _module_tasks(module)
    if left:
        run.result("MOD-13").fail(f"tasks still running after stop(): {left}")


def _outside(paths: list[str], allowed: Path | None) -> list[str]:
    """The written paths that are not under ``allowed``."""
    if allowed is None:
        return sorted(set(paths))
    root = str(allowed)
    out = set()
    for p in paths:
        real = os.path.realpath(p)
        if real != root and not real.startswith(root + os.sep):
            out.add(p)
    return sorted(out)


def _module_tasks(module: type[MirrorModule]) -> list[str]:
    root = _module_dir(module)
    found = []
    for task in asyncio.all_tasks():
        if task.done() or task is asyncio.current_task():
            continue
        coro = task.get_coro()
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
        if code is not None and Path(code.co_filename).resolve().is_relative_to(root):
            found.append(task.get_name())
    return sorted(found)


async def _cancellation_run(run: _Run) -> None:
    """MOD-14: cancelled hooks and tasks end cancelled."""
    h = run.harness()
    r = run.result("MOD-14")
    async with run.open(h):
        prefix = f"{h.instance_id}:"
        for task in list(asyncio.all_tasks()):
            name = task.get_name()
            if not name.startswith(prefix) or name.endswith((":inbox", ":scheduler")):
                continue
            if task.done():
                continue
            task.cancel()
            await h.settle()
            if task.done() and not task.cancelled() and task.exception() is None:
                r.fail(f"task {name!r} swallowed CancelledError")
        obj = h.instance
        if obj is None:
            return
        for name, model in dict(run.module.consumes).items():
            payload = sample_payload(model)
            if payload is None:
                continue
            nid = uuid4()
            note = Notification(
                id=nid,
                trace=nid,
                ts=h.clock.wall(),
                name=name,
                source="kit/harness",
                payload=payload,
            )
            task = asyncio.ensure_future(obj.on_notification(note))
            await asyncio.sleep(0)
            if task.done():
                continue
            task.cancel()
            await h.settle()
            if task.done() and not task.cancelled() and task.exception() is None:
                r.fail(f"on_notification({name}) swallowed CancelledError")


async def _two_instances(run: _Run) -> None:
    """MOD-15: two instances side by side; no mutable module-level state."""
    r = run.result("MOD-15")
    before = _mutable_globals(run.module)
    a, b = run.harness(), run.harness()
    async with run.open(a), run.open(b):
        await _exercise(a)
        await _exercise(b)
        for h in (a, b):
            if _failed(h) and not run.failed_alone:
                r.fail(f"{h.instance_id} failed while another instance ran: {h.managed.reason}")
    after = _mutable_globals(run.module)
    for name, value in before.items():
        if after.get(name) != value:
            r.fail(f"mutable module-level state {name} changed while instances ran")


async def _requests_fail(run: _Run) -> None:
    """MOD-26: no responder (main run) and a failing responder."""
    r = run.result("MOD-26")
    h = run.harness()
    h._fail_requests = True
    async with run.open(h):
        await _exercise(h)
        if _failed(h):
            r.fail(f"stops working when its requests fail: {h.managed.reason}")


async def _suspend_cycles(run: _Run) -> None:
    """MOD-27: rapid suspend/resume."""
    r = run.result("MOD-27")
    h = run.harness()
    async with run.open(h):
        for i in range(SUSPEND_CYCLES):
            await h.suspend()
            await h.resume()
            if _failed(h):
                r.fail(f"failed after {i + 1} hide/show cycles: {h.managed.reason}")
                return
        if h.instance_state is not State.RUNNING:
            r.fail(f"not RUNNING after {SUSPEND_CYCLES} cycles: {h.instance_state}")


async def _payload_fuzz(run: _Run) -> None:
    """MOD-43: consumed notifications with an extra field and without optional ones."""
    r = run.result("MOD-43")
    h = run.harness()
    async with run.open(h):
        for name, model in dict(run.module.consumes).items():
            base = sample_payload(model)
            if base is None:
                continue
            variants = [("an unknown field", {**base, "mm3_kit_extra": "x"})]
            for opt in _optional_fields(model):
                variants.append((f"no {opt}", {k: v for k, v in base.items() if k != opt}))
            for label, payload in variants:
                with contextlib.suppress(ValueError):
                    await h.notify(name, payload)
                if _failed(h):
                    r.fail(f"{name} with {label} makes the instance fail: {h.managed.reason}")
                    return


async def _no_grants(run: _Run) -> None:
    """MOD-72: degrades cleanly without grants."""
    r = run.result("MOD-72")
    if not (set(run.module.capabilities) & SCOPED_CAPABILITIES):
        return
    h = run.harness(grants=None)
    async with run.open(h):
        await _exercise(h)
        if _failed(h):
            r.fail(f"fails without grants: {h.managed.reason}")
        if not h.logs.at_least("warning"):
            r.fail("logs no warning when its grants are missing")


async def run_kit_checks(h: ModuleHarness) -> list[CheckResult]:
    """Run the test-kit checks for the module of an open harness.

    Args:
        h: The harness whose arguments the fresh instances reuse.

    Returns:
        One result per row of :data:`KIT_CHECKS`; notes such as unmocked HTTP
        calls are attached to the first result's findings as ``note:`` lines.
    """
    run = _Run(h)
    for step in (
        _main_run,
        _cancellation_run,
        _two_instances,
        _requests_fail,
        _suspend_cycles,
        _payload_fuzz,
        _no_grants,
    ):
        try:
            await step(run)
        except Exception as exc:
            mid = {
                "_main_run": "MOD-10",
                "_cancellation_run": "MOD-14",
                "_two_instances": "MOD-15",
                "_requests_fail": "MOD-26",
                "_suspend_cycles": "MOD-27",
                "_payload_fuzz": "MOD-43",
                "_no_grants": "MOD-72",
            }[step.__name__]
            run.result(mid).fail(f"the check could not run: {type(exc).__name__}: {exc}")
    results = list(run.results.values())
    if run.notes:
        results[0].findings.extend(f"note: {n}" for n in sorted(set(run.notes)))
    return results
