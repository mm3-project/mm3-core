"""Static checks of a module's JavaScript (Contract 12.1) with an in-house tokenizer.

The tokenizer understands comments, strings, template literals (with nested
substitutions) and regular expressions, and accepts any modern syntax: it
never builds a syntax tree, so syntax it does not know cannot make it fail.
The rules look at token sequences.

Implements:
    MOD-30, MOD-31, MOD-32, MOD-33, MOD-35, MOD-37: Module Contract 1.0 rev 10, §5.3 Rules.
    MOD-61: Module Contract 1.0 rev 10, §7 Internationalization.
    MOD-80: Module Contract 1.0 rev 10, §9 Logging, health and errors.
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from mm3.core.modcheck.report import Finding

__all__ = ["JsContext", "Tok", "notified_names", "scan_js", "tokenize"]


@dataclass(slots=True)
class Tok:
    """A token.

    Attributes:
        kind: ``name``, ``num``, ``str``, ``tmpl``, ``regex``, ``punct`` or ``comment``.
        text: The source text (for ``str`` the decoded-free inner text, for ``tmpl`` empty).
        line: 1-based line of the first character.
        parts: Static parts of a template literal.
        exprs: Tokens of each substitution of a template literal.
        tag: Name of the tag function of a template literal.
    """

    kind: str
    text: str
    line: int
    parts: list[str] = field(default_factory=list)
    exprs: list[list[Tok]] = field(default_factory=list)
    tag: str | None = None


_OPS = (
    ">>>=", "...", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=", "??=",
    "=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--", "+=", "-=",
    "*=", "/=", "%=", "&=", "|=", "^=", "**", "<<", ">>",
)  # fmt: skip
_NAME = re.compile(r"#?[A-Za-z_$\u0080-\U0010ffff][\w$\u0080-\U0010ffff]*")
_NUM = re.compile(r"(?:0[xXoObB][\da-fA-F_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?|\.\d[\d_]*)n?")
_REGEX_AFTER = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
    "do", "else", "yield", "await",
}  # fmt: skip


def tokenize(src: str) -> list[Tok]:
    """Split JavaScript source into tokens.

    Args:
        src: The source text.

    Returns:
        The tokens, comments included, in source order.
    """
    toks, _, _ = _scan(src, 0, 1, stop_at_brace=False)
    return toks


def _regex_allowed(prev: Tok | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.text not in (")", "]")
    if prev.kind == "name":
        return prev.text in _REGEX_AFTER
    return False


def _scan(src: str, i: int, line: int, *, stop_at_brace: bool) -> tuple[list[Tok], int, int]:
    toks: list[Tok] = []
    prev: Tok | None = None  # last significant token
    depth = 0
    n = len(src)

    def add(tok: Tok) -> None:
        nonlocal prev
        toks.append(tok)
        if tok.kind != "comment":
            prev = tok

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace() or c == "\ufeff":
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            add(Tok("comment", src[i:j], line))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            add(Tok("comment", src[i:j], line))
            line += src.count("\n", i, j)
            i = j
        elif c in "\"'":
            j, start_line = i + 1, line
            while j < n and src[j] != c:
                if src[j] == "\\" and j + 1 < n:
                    if src[j + 1] == "\n":
                        line += 1
                    j += 1
                elif src[j] == "\n":
                    break  # unterminated: stop at the line end
                j += 1
            add(Tok("str", src[i + 1 : j], start_line))
            i = min(j + 1, n)
        elif c == "`":
            tag = prev.text if prev is not None and prev.kind == "name" else None
            tok, i, line = _template(src, i, line)
            tok.tag = tag
            add(tok)
        elif c == "/" and _regex_allowed(prev):
            j, in_class = i + 1, False
            while j < n and src[j] != "\n":
                ch = src[j]
                if ch == "\\":
                    j += 1
                elif ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and (src[j].isalpha()):
                j += 1
            add(Tok("regex", src[i:j], line))
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM.match(src, i)
            end = m.end() if m else i + 1
            add(Tok("num", src[i:end], line))
            i = end
        elif (m := _NAME.match(src, i)) is not None:
            add(Tok("name", m.group(), line))
            i = m.end()
        else:
            if c == "}" and stop_at_brace and depth == 0:
                return toks, i + 1, line
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            op = next((o for o in _OPS if src.startswith(o, i)), c)
            if op == "?." and i + 2 < n and src[i + 2].isdigit():
                op = "?"
            add(Tok("punct", op, line))
            i += len(op)
    return toks, i, line


def _template(src: str, i: int, line: int) -> tuple[Tok, int, int]:
    tok = Tok("tmpl", "", line)
    n = len(src)
    i += 1
    buf: list[str] = []
    while i < n:
        c = src[i]
        if c == "`":
            tok.parts.append("".join(buf))
            return tok, i + 1, line
        if c == "\\" and i + 1 < n:
            buf.append(src[i : i + 2])
            line += src[i + 1] == "\n"
            i += 2
        elif c == "$" and src.startswith("${", i):
            tok.parts.append("".join(buf))
            buf = []
            inner, i, line = _scan(src, i + 2, line, stop_at_brace=True)
            tok.exprs.append(inner)
        else:
            buf.append(c)
            line += c == "\n"
            i += 1
    tok.parts.append("".join(buf))
    return tok, i, line


def walk(tokens: list[Tok]) -> Iterator[Tok]:
    """Yield the tokens with template substitutions expanded after their template."""
    for t in tokens:
        yield t
        for expr in t.exprs:
            yield from walk(expr)


# --- rules -------------------------------------------------------------------------

_USER_TEXT = re.compile(r"[A-Za-zÀ-￿]+\s+[A-Za-zÀ-￿]")
_TAG_TEXT = re.compile(r"<[^>]*>|\$\{[^}]*\}")
_GLOBAL_OBJECTS = {"window", "globalThis", "self"}
_NET = {"fetch": "fetch", "XMLHttpRequest": "XMLHttpRequest", "WebSocket": "WebSocket",
        "EventSource": "EventSource"}  # fmt: skip
_DOC_STYLE = {"head", "styleSheets", "adoptedStyleSheets", "fonts"}


@dataclass(frozen=True)
class JsContext:
    """What the rules need to know about the module.

    Attributes:
        module: The module ``name``.
        element: The tag the backend declares (``mm3-<name>``).
        static_rel: Path of the file inside the module's ``static/`` directory.
    """

    module: str
    element: str | None
    static_rel: str


def _sig(tokens: list[Tok]) -> list[Tok]:
    return [t for t in tokens if t.kind != "comment"]


def _prev_dot(sig: list[Tok], i: int) -> Tok | None:
    """Return the token before ``i`` when it is ``.`` or ``?.``; otherwise ``None``."""
    if i > 0 and sig[i - 1].kind == "punct" and sig[i - 1].text in (".", "?."):
        return sig[i - 1]
    return None


def _member_of(sig: list[Tok], i: int) -> str | None:
    """Return the name before the dot that precedes ``i`` (``a`` in ``a.b``)."""
    if _prev_dot(sig, i) is not None and i >= 2 and sig[i - 2].kind == "name":
        return sig[i - 2].text
    return None


def scan_js(rel: str, text: str, ctx: JsContext) -> tuple[list[tuple[str, Finding]], int]:
    """Run every JavaScript rule on one file.

    Args:
        rel: Path relative to the module root, for findings.
        text: The source.
        ctx: What is known about the module.

    Returns:
        ``(MOD id, finding)`` pairs, and the number of ``customElements.define`` calls.
    """
    out: list[tuple[str, Finding]] = []

    def hit(mod_id: str, tok: Tok, message: str) -> None:
        out.append((mod_id, Finding(message, rel, tok.line)))

    sig = _sig(list(walk(tokenize(text))))
    defines = 0
    for i, t in enumerate(sig):
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        nxt2 = sig[i + 2] if i + 2 < len(sig) else None
        if t.kind == "tmpl":
            _template_rules(t, hit)
        if t.kind != "name":
            continue
        member = _member_of(sig, i)
        after_dot = _prev_dot(sig, i) is not None
        # MOD-32: no network from the element.
        if t.text in _NET and (not after_dot or member in _GLOBAL_OBJECTS):
            if t.text != "fetch" or (nxt is not None and nxt.text in ("(", "?.")):
                hit(
                    "MOD-32",
                    t,
                    f"{t.text} is not allowed in a module element; data comes from its backend",
                )
        if t.text == "sendBeacon" and after_dot:
            hit("MOD-32", t, "navigator.sendBeacon is a network call")
        # MOD-31: no innerHTML with data.
        if t.text in ("innerHTML", "outerHTML") and after_dot and nxt is not None:
            if nxt.kind == "punct" and nxt.text in ("=", "+="):
                value = nxt2
                static = (
                    value is not None
                    and (value.kind == "str" or (value.kind == "tmpl" and not value.exprs))
                    and i + 3 < len(sig)
                    and sig[i + 3].text in (";", "}")
                )
                if not static:
                    hit("MOD-31", t, f"{t.text} assigned from data; render with html`` or DOM APIs")
        if t.text == "insertAdjacentHTML" and after_dot:
            hit("MOD-31", t, "insertAdjacentHTML takes markup; use html`` or DOM APIs")
        if t.text == "write" and member == "document":
            hit("MOD-31", t, "document.write takes markup; use html`` or DOM APIs")
        # MOD-35: timers through the element.
        if t.text in ("setInterval", "setTimeout") and member != "this":
            if nxt is not None and nxt.text == "(":
                hit(
                    "MOD-35", t, f"use this.{t.text}, which is cleared when the element disconnects"
                )
        # MOD-80: console.
        if t.text == "console" and nxt is not None and nxt.text == "." and nxt2 is not None:
            if nxt2.text in ("log", "info", "warn", "error", "trace"):
                hit("MOD-80", t, f"console.{nxt2.text} is not allowed; use this.log")
        # MOD-33: document-level styling.
        if (
            t.text == "document"
            and nxt is not None
            and nxt.text in (".", "?.")
            and nxt2 is not None
        ):
            if nxt2.text in _DOC_STYLE:
                hit("MOD-33", t, f"document.{nxt2.text} reaches outside the shadow root")
            elif nxt2.text in ("body", "documentElement") and i + 4 < len(sig):
                if sig[i + 3].text == "." and sig[i + 4].text == "style":
                    hit("MOD-33", t, f"document.{nxt2.text}.style reaches outside the shadow root")
        # MOD-30: no globals; exactly one element.
        if t.text in _GLOBAL_OBJECTS and nxt is not None and nxt.text == ".":
            if i + 3 < len(sig) and sig[i + 2].kind == "name" and sig[i + 3].text in ("=", "+="):
                if t.text != "self" or sig[i + 2].text not in ("onmessage",):
                    hit("MOD-30", t, f"{t.text}.{sig[i + 2].text} creates a global")
        if t.text == "define" and member == "customElements":
            defines += 1
            tag = nxt2 if nxt is not None and nxt.text == "(" else None
            if tag is None or tag.kind != "str":
                hit("MOD-30", t, "customElements.define needs the literal tag name")
            elif tag.text != (ctx.element or f"mm3-{ctx.module}"):
                hit(
                    "MOD-30",
                    t,
                    f"defines {tag.text!r}, the tag must be {ctx.element or 'mm3-' + ctx.module!r}",
                )
        # MOD-37: imports.
        if t.text == "import" and not after_dot:
            _imports(sig, i, ctx, hit)
        if t.text == "export" and not after_dot:
            j = i + 1
            while j < len(sig) and j < i + 400 and sig[j].text not in (";", "from"):
                if sig[j].kind == "name" and sig[j].text in (
                    "class",
                    "function",
                    "const",
                    "let",
                    "var",
                ):
                    break
                j += 1
            if j + 1 < len(sig) and sig[j].text == "from" and sig[j + 1].kind == "str":
                _check_specifier(sig[j + 1], ctx, hit)
    _text_rules(sig, hit)
    return out, defines


def _template_rules(t: Tok, hit) -> None:  # type: ignore[no-untyped-def]
    if t.tag != "html":
        return
    flat = "\x00".join(t.parts)
    stripped = _TAG_TEXT.sub(
        " ", re.sub(r"<[^>]*(?:\x00[^>]*)*>", " ", flat.replace("\x00", "${}"))
    )
    for chunk in stripped.splitlines():
        if _USER_TEXT.search(chunk):
            hit("MOD-61", t, f"user-visible literal text {chunk.strip()[:40]!r}; use this.t()")
            break


def _text_rules(sig: list[Tok], hit) -> None:  # type: ignore[no-untyped-def]
    for i, t in enumerate(sig):
        if t.kind != "name":
            continue
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        nxt2 = sig[i + 2] if i + 2 < len(sig) else None
        if t.text in ("textContent", "innerText") and nxt is not None and nxt.text == "=":
            if nxt2 is not None and nxt2.kind in ("str", "tmpl"):
                text = nxt2.text if nxt2.kind == "str" else " ".join(nxt2.parts)
                if _USER_TEXT.search(text):
                    hit("MOD-61", t, f"user-visible literal text {text[:40]!r}; use this.t()")
        if t.text in ("notify", "send") and _prev_dot(sig, i) and _member_of(sig, i) == "this":
            depth, j = 0, i + 1
            while j < len(sig):
                if sig[j].text == "(":
                    depth += 1
                elif sig[j].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif (
                    sig[j].kind == "str"
                    and j > 0
                    and sig[j - 1].text == ":"
                    and _USER_TEXT.search(sig[j].text)
                ):
                    hit(
                        "MOD-61",
                        sig[j],
                        f"user-visible literal text {sig[j].text[:40]!r} in a payload",
                    )
                j += 1


def _imports(sig: list[Tok], i: int, ctx: JsContext, hit) -> None:  # type: ignore[no-untyped-def]
    nxt = sig[i + 1] if i + 1 < len(sig) else None
    if nxt is None or nxt.text == ".":
        return  # import.meta
    if nxt.kind == "str":
        _check_specifier(nxt, ctx, hit)
    elif nxt.text == "(":
        arg = sig[i + 2] if i + 2 < len(sig) else None
        if arg is not None and arg.kind == "str":
            _check_specifier(arg, ctx, hit)
        else:
            hit(
                "MOD-37",
                sig[i],
                "dynamic import() needs a literal specifier inside static/ or /core/",
            )
    else:
        for j in range(i + 1, min(i + 400, len(sig) - 1)):
            if sig[j].text == "from" and sig[j + 1].kind == "str":
                _check_specifier(sig[j + 1], ctx, hit)
                return


def _check_specifier(tok: Tok, ctx: JsContext, hit) -> None:  # type: ignore[no-untyped-def]
    spec = tok.text
    if spec.startswith("/core/") or spec.startswith(f"/modules/{ctx.module}/"):
        return
    if spec.startswith(("./", "../")):
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(ctx.static_rel), spec))
        if not resolved.startswith(".."):
            return
        hit("MOD-37", tok, f"import {spec!r} leaves the module's static/ directory")
        return
    hit(
        "MOD-37",
        tok,
        f"import {spec!r}: only /core/mm3.js and files under the module's static/ are allowed",
    )


def notified_names(text: str) -> list[tuple[str, int]]:
    """Return the literal names the file passes to ``this.notify(...)``.

    Args:
        text: The source.

    Returns:
        ``(name, line)`` pairs.
    """
    sig = _sig(list(walk(tokenize(text))))
    out: list[tuple[str, int]] = []
    for i, t in enumerate(sig[:-3]):
        if t.kind == "name" and t.text == "notify" and _member_of(sig, i) == "this":
            if sig[i + 1].text == "(" and sig[i + 2].kind == "str":
                out.append((sig[i + 2].text, sig[i + 2].line))
    return out
