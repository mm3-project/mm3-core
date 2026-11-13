"""The editing protocol of the admin: config set view, patch, validate and save.

The admin page holds the draft; the server keeps none (CORE-301). Each call
names the ETag it started from, ``"<SHA-256 of the config set>"``, and the
server compares it with the files on disk (CORE-300). Every file the server
returns shows secret values as ``!masked``; a ``!masked`` node sent back keeps
the value of the base (CORE-303). Validation is the engine's own, in a scratch
copy of the config directory, so the admin can never write a file that fails
``mm3 check-config`` (CORE-36).

Implements:
    CORE-35, CORE-36: Core spec 0.22, §6.4 Web configuration editor.
    CORE-300, CORE-303: Core spec 0.22, §6.4 Web configuration editor.
    CORE-320: Core spec 0.25, §6.4 Web configuration editor.
    CORE-321: Core spec 0.25, §6.8 Configuration history, undo and redo.
    CORE-301, CORE-302: Core spec 0.39, §6.4 Web configuration editor.
    CORE-324: Core spec 0.26, §6.8 Configuration history, undo and redo.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mm3.core.config.assets import CUSTOM_CSS, TEMPLATE_DIR
from mm3.core.config.history import (
    EXIT_CONFLICT,
    EXIT_DONE,
    EXIT_INVALID,
    EXIT_NOTHING,
    ConfigHistory,
    ConfigSet,
    OpResult,
    VersionMeta,
)
from mm3.core.config.history import (
    MASK as HISTORY_MASK,
)
from mm3.core.config.layout import layout_object
from mm3.core.config.loader import (
    PatchError,
    node_to_json,
    rt_load,
    rt_mask_strings,
    rt_patch,
    rt_unmask,
)
from mm3.core.config.validate import ConfigResult
from mm3.core.diag.secrets import MASK, Masker

__all__ = ["ConfigEditor", "EditorError", "etag_of", "file_role"]


class EditorError(Exception):
    """A refused editor or history call, mapped to a ``CORE_API_*`` code by the API.

    Args:
        code: A code of CORE-298.
        detail: English explanation.
        errors: Validation records.
        extra: Further members of the problem document.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        errors: Sequence[Mapping[str, Any]] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors = errors
        self.extra = extra


def etag_of(config_set: ConfigSet) -> str:
    """Return the ETag of a config set: its SHA-256 (CORE-153), quoted (CORE-300)."""
    return f'"{config_set.sha256()}"'


def file_role(config_set: ConfigSet, rel: str) -> str:
    """Return ``main``, ``include``, ``widget_template`` or ``custom_css`` (CORE-301)."""
    if rel == config_set.main:
        return "main"
    if rel == CUSTOM_CSS:
        return "custom_css"
    if rel.startswith(TEMPLATE_DIR + "/") or rel.startswith(TEMPLATE_DIR + os.sep):
        return "widget_template"
    return "include"


def _is_yaml(rel: str) -> bool:
    return rel.endswith((".yaml", ".yml"))


def _records(result: ConfigResult) -> list[dict[str, Any]]:
    return [r.as_dict() for r in result.records]


def _layout(result: ConfigResult) -> dict[str, Any] | None:
    """The draft's ``layout`` object, or ``None`` when it cannot be computed (CORE-301)."""
    geometry = result.geometry
    return layout_object(geometry) if geometry is not None else None


def _diff_text(text: str) -> str:
    """Show the history mask as the CORE-303 tag in a diff."""
    return text.replace(f"'{HISTORY_MASK}'", MASK).replace(HISTORY_MASK, MASK)


@dataclass
class _Draft:
    config_set: ConfigSet
    changed: list[str]


class ConfigEditor:
    """Admin editing and history on top of :class:`ConfigHistory` (CORE-301, CORE-321).

    The methods block on file and history I/O; the API calls them in a thread.

    Args:
        history: The configuration's history.
        masker: Returns the masker of the running configuration's secrets.

    Implements:
        CORE-301, CORE-302: Core spec 0.39, §6.4 Web configuration editor.
    """

    def __init__(
        self,
        history: ConfigHistory,
        masker: Callable[[], Masker] = Masker,
        applied: Callable[[], tuple[int, int] | None] = lambda: None,
    ) -> None:
        self.history = history
        self.masker = masker
        self.applied = applied

    def _check_draft(self, config_set: ConfigSet) -> ConfigResult:
        """Validate a draft at the viewport the engine uses (CORE-145, CORE-301)."""
        return self.history.validate_set(config_set, self.applied())

    # --- views ------------------------------------------------------------------------

    def _mask_file(self, rel: str, data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        masker = self.masker()
        if not masker.secrets:
            return text
        if not _is_yaml(rel):
            return masker.mask_text(text)
        try:
            masked, _ = rt_mask_strings(text, lambda v: masker.mask(v) == MASK, MASK)
        except Exception:
            return masker.mask_text(text)
        return masked

    def files_view(
        self, config_set: ConfigSet, names: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return ``[{path, role, content}]`` with secrets as ``!masked`` (CORE-301, CORE-303)."""
        rels = sorted(config_set.files) if names is None else list(names)
        main_first = sorted(rels, key=lambda r: (r != config_set.main, r))
        return [
            {
                "path": rel.replace(os.sep, "/"),
                "role": file_role(config_set, rel),
                "content": self._mask_file(rel, config_set.files[rel]),
            }
            for rel in main_first
            if rel in config_set.files
        ]

    def documents(self, config_set: ConfigSet) -> dict[str, Any]:
        """Return every YAML file of a set as data, secrets as ``{"!masked": null}`` (CORE-301).

        Values are before ``!env`` substitution, tagged nodes as in CORE-320;
        a file that does not parse maps to ``None``.
        """
        out: dict[str, Any] = {}
        for item in self.files_view(config_set):
            if not _is_yaml(item["path"]):
                continue
            try:
                out[item["path"]] = node_to_json(rt_load(item["content"]))
            except Exception:
                out[item["path"]] = None
        return out

    def version_files(self, version: int) -> dict[str, Any]:
        """``GET /api/v1/config/history/{version}``: ``{version, files}`` (CORE-324).

        Raises:
            EditorError: Unknown version (404).
        """
        try:
            config_set = self.history.store.read_set(version)
        except (KeyError, FileNotFoundError):
            raise EditorError("CORE_API_NOT_FOUND", f"Unknown version {version}") from None
        return {"version": version, "files": self.files_view(config_set)}

    def current_version(self) -> int | None:
        """The latest version in the history."""
        latest = self.history.store.latest()
        return latest.version if latest is not None else None

    def view(self) -> dict[str, Any]:
        """``GET /api/v1/config``: ``{etag, version, files}`` (CORE-301)."""
        config_set, _ = self.history.disk()
        return {
            "etag": etag_of(config_set),
            "version": self.current_version(),
            "files": self.files_view(config_set),
        }

    # --- the draft --------------------------------------------------------------------

    def _check_base(self, base: str | None) -> tuple[ConfigSet, ConfigResult]:
        """Return the disk set when ``base`` names it, else refuse as in CORE-300."""
        config_set, result = self.history.disk()
        if base is None:
            raise EditorError("CORE_API_PRECONDITION_REQUIRED", "If-Match or base is required")
        if base.strip() != etag_of(config_set):
            self._conflict(config_set, result)
        return config_set, result

    def _conflict(self, config_set: ConfigSet, result: ConfigResult) -> None:
        extra = {"etag": etag_of(config_set), "files": self.files_view(config_set)}
        if not result.ok:
            raise EditorError(
                "CORE_API_DISK_INVALID",
                "The configuration on disk changed and is invalid",
                errors=[r.as_dict() for r in result.errors],
                extra=extra,
            )
        raise EditorError(
            "CORE_API_CONFLICT", "The configuration changed since it was loaded", extra=extra
        )

    def _rel(self, path: str) -> str:
        rel = os.path.normpath(path.replace("/", os.sep))
        if os.path.isabs(rel) or rel.startswith("..") or rel in (".", ""):
            raise EditorError(
                "CORE_API_INVALID_REQUEST", f"{path!r} is not in the config directory"
            )
        return rel

    def _overlay(self, base_set: ConfigSet, files: Sequence[Mapping[str, Any]]) -> _Draft:
        """Apply the draft files, putting masked secrets back (CORE-303)."""
        masker = self.masker()
        out = base_set
        changed: list[str] = []
        for item in files:
            path, content = item.get("path"), item.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise EditorError("CORE_API_INVALID_REQUEST", "files need path and content")
            rel = self._rel(path)
            original = base_set.files.get(rel)
            text = content
            if _is_yaml(rel):
                base_text = original.decode("utf-8", errors="replace") if original else None
                try:
                    text = rt_unmask(content, base_text, lambda v: masker.mask(v) == MASK)
                except PatchError as exc:
                    raise EditorError(
                        "CORE_API_MASKED_UNKNOWN", f"{path}: {exc}", extra={"path": path}
                    ) from None
                except Exception:  # noqa: S110 - a syntax error becomes a record later
                    pass
            out = out.with_file(rel, text.encode("utf-8"))
            changed.append(rel)
        return _Draft(out, changed)

    def patch(
        self,
        base: str | None,
        files: Sequence[Mapping[str, Any]],
        ops: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """``POST /api/v1/config/patch``: apply edits to the draft, write nothing (CORE-301).

        Returns:
            ``{files, records, documents, layout}``: every changed file,
            secrets masked, the validation records of the whole draft, every
            YAML file of the draft as data, and the draft's layout object
            (CORE-301, 0.28).

        Raises:
            EditorError: Conflict, bad masked value or a patch that fails.
        """
        base_set, _ = self._check_base(base)
        draft = self._overlay(base_set, files)
        config_set = draft.config_set
        changed = list(draft.changed)
        for index, item in enumerate(ops):
            path, patch = item.get("file"), item.get("patch")
            if not isinstance(path, str) or not isinstance(patch, list):
                raise EditorError(
                    "CORE_API_PATCH_FAILED",
                    f"ops[{index}] needs file and patch",
                    extra={"index": index},
                )
            rel = self._rel(path)
            data = config_set.files.get(rel)
            if data is None or not _is_yaml(rel):
                raise EditorError(
                    "CORE_API_PATCH_FAILED",
                    f"ops[{index}]: {path} is not a YAML file of the set",
                    extra={"index": index},
                )
            try:
                text = rt_patch(data.decode("utf-8"), patch)
            except PatchError as exc:
                raise EditorError(
                    "CORE_API_PATCH_FAILED",
                    f"ops[{index}] operation {exc.index}: {exc}",
                    extra={"index": index, "op": exc.index, "pointer": exc.pointer},
                ) from None
            except Exception as exc:
                raise EditorError(
                    "CORE_API_PATCH_FAILED",
                    f"ops[{index}]: {path} does not parse: {exc}",
                    extra={"index": index},
                ) from None
            config_set = config_set.with_file(rel, text.encode("utf-8"))
            if rel not in changed:
                changed.append(rel)
        result = self._check_draft(config_set)
        return {
            "files": self.files_view(config_set, changed),
            "records": _records(result),
            "documents": self.documents(config_set),
            "layout": _layout(result),
        }

    def validate(self, base: str | None, files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """``POST /api/v1/config/validate``: records and the diff preview (CORE-302).

        Returns:
            ``{records, diff, layout}`` with ``diff`` ``[{section, before,
            after}]`` against the current version, secrets masked, and
            ``layout`` as for :meth:`patch` (CORE-302, 0.28).
        """
        base_set, base_result = self._check_base(base)
        draft = self._overlay(base_set, files)
        result = self._check_draft(draft.config_set)
        diff: list[dict[str, str]] = []
        if result.ok and base_result.ok:
            diff = [
                {"section": s, "before": _diff_text(a), "after": _diff_text(b)}
                for s, a, b in self.history.section_diff(
                    (base_set, base_result), (draft.config_set, result)
                )
            ]
        return {"records": _records(result), "diff": diff, "layout": _layout(result)}

    def save(
        self,
        if_match: str | None,
        base: str | None,
        files: Sequence[Mapping[str, Any]],
        author: str,
    ) -> dict[str, Any]:
        """``PUT /api/v1/config``: validate again, write and record a version (CORE-302).

        Returns:
            ``{version, etag}``.

        Raises:
            EditorError: Missing ``If-Match`` (428), conflict (409), invalid (422).
        """
        if if_match is None:
            raise EditorError("CORE_API_PRECONDITION_REQUIRED", "If-Match is required")
        base_set, _ = self._check_base(if_match)
        if base is not None and base.strip() != if_match.strip():
            self._conflict(*self.history.disk())
        draft = self._overlay(base_set, files)
        result = self.history.validate_set(draft.config_set)
        if not result.ok:
            raise EditorError(
                "CORE_API_CONFIG_INVALID",
                "The configuration is not valid; nothing was written",
                errors=_records(result),
            )
        outcome = self.history.save(draft.config_set, author, expect=base_set.sha256())
        return self._answer(outcome)

    # --- history ----------------------------------------------------------------------

    def _answer(self, outcome: OpResult) -> dict[str, Any]:
        if outcome.code == EXIT_DONE:
            config_set, _ = self.history.disk()
            return {"version": outcome.version, "etag": etag_of(config_set)}
        if outcome.code == EXIT_CONFLICT:
            self._conflict(*self.history.disk())
        if outcome.code == EXIT_NOTHING:
            raise EditorError("CORE_API_NOTHING_TO_DO", outcome.message or "Nothing to do")
        if outcome.code == EXIT_INVALID:
            raise EditorError(
                "CORE_API_CONFIG_INVALID",
                outcome.message or "The result does not validate; nothing was applied",
                errors=[r.as_dict() for r in outcome.records],
            )
        raise EditorError(
            "CORE_API_DISK_INVALID",
            outcome.message or "The configuration on disk is invalid",
            errors=[r.as_dict() for r in outcome.records],
        )

    def history_page(self, limit: int, before: int | None) -> dict[str, Any]:
        """``GET /api/v1/config/history``: ``{items, next}``, newest first (CORE-321, CORE-299)."""
        versions = [
            v for v in reversed(self.history.store.versions()) if before is None or v < before
        ]
        page = versions[:limit]
        current = self.current_version()
        items = [self._item(self.history.store.meta(v), current) for v in page]
        more = len(versions) > limit
        return {"items": items, "next": str(page[-1]) if more and page else None}

    @staticmethod
    def _item(meta: VersionMeta, current: int | None) -> dict[str, Any]:
        return {
            "version": meta.version,
            "time": meta.time,
            "origin": meta.origin,
            "author": meta.author,
            "parent": meta.parent,
            "sections": list(meta.sections),
            "sha256": meta.sha256,
            "current": meta.version == current,
        }

    def history_diff(self, a: int, b: int, section: str | None) -> list[dict[str, str]]:
        """``GET /api/v1/config/history/{a}/diff/{b}`` (CORE-321).

        Raises:
            EditorError: Unknown version or section (404).
        """
        try:
            va, vb = self.history.version_set(a), self.history.version_set(b)
        except KeyError as exc:
            raise EditorError("CORE_API_NOT_FOUND", f"Unknown version {exc.args[0]}") from None
        if section is not None:
            known = set(self.history.sections(*va)) | set(self.history.sections(*vb))
            if section not in known:
                raise EditorError("CORE_API_NOT_FOUND", f"Unknown section {section!r}")
        out = [
            {"section": s, "before": _diff_text(x), "after": _diff_text(y)}
            for s, x, y in self.history.section_diff(va, vb)
            if section is None or s == section
        ]
        return out

    def undo(self, if_match: str | None, author: str) -> dict[str, Any]:
        """``POST /api/v1/config/undo`` (CORE-321)."""
        return self._answer(self.history.undo(author, expect=self._expect(if_match)))

    def redo(self, if_match: str | None, author: str) -> dict[str, Any]:
        """``POST /api/v1/config/redo`` (CORE-321)."""
        return self._answer(self.history.redo(author, expect=self._expect(if_match)))

    def restore(
        self, if_match: str | None, version: int, section: str | None, author: str
    ) -> dict[str, Any]:
        """``POST /api/v1/config/restore`` (CORE-321).

        Raises:
            EditorError: Unknown version or section (404) and the errors of :meth:`_answer`.
        """
        expect = self._expect(if_match)
        if version not in self.history.store.versions():
            raise EditorError("CORE_API_NOT_FOUND", f"Unknown version {version}")
        if section is not None:
            known = set(self.history.sections(*self.history.version_set(version)))
            known |= set(self.history.sections(*self.history.disk()))
            if section not in known:
                raise EditorError("CORE_API_NOT_FOUND", f"Unknown section {section!r}")
        return self._answer(self.history.restore(version, author, section=section, expect=expect))

    @staticmethod
    def _expect(if_match: str | None) -> str:
        if if_match is None:
            raise EditorError("CORE_API_PRECONDITION_REQUIRED", "If-Match is required")
        return if_match.strip().strip('"')
