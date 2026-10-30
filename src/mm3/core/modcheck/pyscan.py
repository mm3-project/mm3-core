"""Static checks of a module's Python source by AST (Contract 12.1).

Nothing is imported: the module class is read from its source, its
declarations as literals and its code as a tree.

Implements:
    MOD-11, MOD-12, MOD-28: Module Contract 1.0 rev 10, §3.3 Rules.
    MOD-17: Module Contract 1.0 rev 10, §3.4 Error semantics.
    MOD-23: Module Contract 1.0 rev 10, §4.3 HttpClient.
    MOD-40: Module Contract 1.0 rev 10, §6.1 Messaging.
    MOD-45, MOD-46, MOD-47: Module Contract 1.0 rev 10, §6.2 Configuration.
    MOD-61: Module Contract 1.0 rev 10, §7 Internationalization.
    MOD-70: Module Contract 1.0 rev 10, §8 Capabilities and grants.
    MOD-80, MOD-86: Module Contract 1.0 rev 10, §9 Logging, health and errors.
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from mm3.core.modcheck.report import Finding

__all__ = ["UPPER_SNAKE", "ClassFacts", "PyScan", "config_findings", "find_class", "scan_python"]

UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
_USER_TEXT = re.compile(r"[A-Za-zÀ-￿]+\s+[A-Za-zÀ-￿]")
_SECRET_NAME = re.compile(r"(?:^|_)(?:key|token|password|passwd|secret|credential)s?(?:_|$)", re.I)

WALL_CLOCK = {
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today",
    "datetime.date.today", "time.time", "time.time_ns",
}  # fmt: skip
HTTP_LIBS = {
    "httpx", "requests", "aiohttp", "urllib3", "urllib.request", "http.client", "httplib2",
    "pycurl", "treq",
}  # fmt: skip
BLOCKING = {
    "time.sleep", "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "os.system", "urllib.request.urlopen",
    "requests.get", "requests.post", "requests.put", "requests.delete", "requests.request",
}  # fmt: skip
HOOKS = (
    "start", "stop", "suspend", "resume", "reconfigure", "on_notification",
    "on_client_message", "on_request",
)  # fmt: skip
CAPABILITY_USE = (
    (re.compile(r"(?:^|\.)ctx\.http\b"), "http.outbound"),
    (re.compile(r"(?:^|\.)ctx\.mqtt\.publish\b"), "mqtt.publish"),
    (re.compile(r"(?:^|\.)ctx\.mqtt\.subscribe\b"), "mqtt.subscribe"),
    (re.compile(r"(?:^|\.)ctx\.mqtt\.ha_entity\b"), "ha.discovery"),
    (re.compile(r"(?:^|\.)ctx\.system\b"), "system.power"),
    (re.compile(r"(?:^|\.)ctx\.router\b"), "api.routes"),
    (re.compile(r"(?:^|\.)ctx\.layout\.(?:hide|show)\b"), "layout.control"),
)


@dataclass
class ClassFacts:
    """What the module class declares, read from its source.

    Attributes:
        cls: Class name.
        file: Relative path of the file defining it.
        line: Line of the ``class`` statement.
        name: ``name`` attribute, if a literal.
        element: ``element`` attribute, if a literal string.
        scripts: ``scripts`` entries.
        styles: ``styles`` entries.
        locales_dir: ``locales_dir`` (``"locales"`` unless assigned).
        capabilities: Declared capabilities.
        emits: Declared notification names with their lines.
        consumes: Same for ``consumes``.
        answers: Same for ``answers``.
        non_literal: Declaration attributes that are not literals.
        min_size: ``min_size_px`` is assigned.
        api_version: ``api_version`` attribute, if a literal.
        config_model: Name of the ``config_model`` class.
        hooks: Hook name to ``(is_async, line)``.
        admin: ``admin_element`` or ``admin_scripts`` assigned.
    """

    cls: str
    file: str
    line: int
    name: str | None = None
    element: str | None = None
    scripts: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    locales_dir: str | None = "locales"
    capabilities: set[str] = field(default_factory=set)
    emits: dict[str, int] = field(default_factory=dict)
    consumes: dict[str, int] = field(default_factory=dict)
    answers: dict[str, int] = field(default_factory=dict)
    non_literal: list[str] = field(default_factory=list)
    min_size: bool = False
    api_version: str | None = None
    config_model: str | None = None
    hooks: dict[str, tuple[bool, int]] = field(default_factory=dict)
    admin: bool = False


@dataclass
class PyScan:
    """Everything one Python file contributes to the static checks.

    Attributes:
        findings: ``(MOD id, finding)`` pairs.
        used_caps: Capability to the first place it is used.
        emitted: Literal names passed to ``send_notification``.
        keys: Literal catalog keys of raised errors with their place.
    """

    findings: list[tuple[str, Finding]] = field(default_factory=list)
    used_caps: dict[str, tuple[str, int]] = field(default_factory=dict)
    emitted: list[tuple[str, str, int]] = field(default_factory=list)
    keys: list[tuple[str, str, int]] = field(default_factory=list)


def _str_items(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
        if node.func.id in ("set", "frozenset", "list", "tuple"):
            return _str_items(node.args[0])
    if isinstance(node, ast.Call) and not node.args and isinstance(node.func, ast.Name):
        return [] if node.func.id in ("set", "frozenset", "list", "tuple") else None
    if isinstance(node, ast.Set | ast.List | ast.Tuple):
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            return [str(e.value) for e in node.elts if isinstance(e, ast.Constant)]
    return None


def _dict_keys(node: ast.expr) -> dict[str, int] | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
        if node.func.id in ("MappingProxyType", "dict"):
            return _dict_keys(node.args[0])
    if isinstance(node, ast.Dict):
        out: dict[str, int] = {}
        for k in node.keys:
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return None
            out[k.value] = k.lineno
        return out
    return None


def find_class(
    trees: dict[str, ast.Module], class_name: str, prefer: str | None = None
) -> ClassFacts | None:
    """Read the module class from the package's source.

    Args:
        trees: Parsed files by relative path.
        class_name: Class named by the entry point.
        prefer: File to prefer when several define the class.

    Returns:
        The facts, or ``None`` when no file defines the class.
    """
    found: list[tuple[str, ast.ClassDef]] = [
        (rel, node)
        for rel, tree in trees.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if not found:
        return None
    rel, node = next(((r, n) for r, n in found if r == prefer), found[0])
    facts = ClassFacts(node.name, rel, node.lineno)
    for stmt in node.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            facts.hooks[stmt.name] = (isinstance(stmt, ast.AsyncFunctionDef), stmt.lineno)
            continue
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        _read_attribute(facts, target.id, value)
    return facts


def _read_attribute(facts: ClassFacts, attr: str, value: ast.expr) -> None:
    const = value.value if isinstance(value, ast.Constant) else None
    if attr == "name" and isinstance(const, str):
        facts.name = const
    elif attr == "api_version" and isinstance(const, str):
        facts.api_version = const
    elif attr == "element":
        facts.element = const if isinstance(const, str) else None
    elif attr == "locales_dir":
        facts.locales_dir = const if isinstance(const, str) else None
    elif attr in ("scripts", "styles"):
        items = _str_items(value)
        if items is None:
            facts.non_literal.append(attr)
        else:
            getattr(facts, attr).extend(items)
    elif attr == "capabilities":
        items = _str_items(value)
        if items is None:
            facts.non_literal.append(attr)
        else:
            facts.capabilities = set(items)
    elif attr in ("emits", "consumes", "answers"):
        keys = _dict_keys(value)
        if keys is None:
            facts.non_literal.append(attr)
        else:
            getattr(facts, attr).update(keys)
    elif attr == "min_size_px":
        facts.min_size = True
    elif attr == "config_model" and isinstance(value, ast.Name):
        facts.config_model = value.id
    elif attr in ("admin_element", "admin_scripts") and not (
        const is None and attr == "admin_element"
    ):
        facts.admin = True


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, facts: ClassFacts | None, tree: ast.Module) -> None:
        self.rel = rel
        self.facts = facts
        self.scan = PyScan()
        self.aliases: dict[str, str] = {}
        self.async_depth = 0
        self._collect_aliases(tree)

    def hit(self, mod_id: str, node: ast.AST, message: str, severity: str = "fail") -> None:
        finding = Finding(message, self.rel, getattr(node, "lineno", None))
        finding.severity = "warn" if severity == "warn" else "fail"
        self.scan.findings.append((mod_id, finding))

    def _collect_aliases(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.aliases[a.asname or a.name.split(".")[0]] = (
                        a.name if a.asname else a.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for a in node.names:
                    self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"

    def resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    # imports (MOD-23)
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._http_import(node, a.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.level == 0 and not self._http_import(node, node.module):
            for a in node.names:
                if self._http_import(node, f"{node.module}.{a.name}"):
                    break

    def _http_import(self, node: ast.AST, dotted: str) -> bool:
        for lib in HTTP_LIBS:
            if dotted == lib or dotted.startswith(lib + "."):
                self.hit("MOD-23", node, f"imports {lib}; outbound HTTP goes through ctx.http")
                return True
        return False

    # functions (MOD-11)
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.async_depth += 1
        self.generic_visit(node)
        self.async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        depth, self.async_depth = self.async_depth, 0
        self.generic_visit(node)
        self.async_depth = depth

    # broad except (MOD-86)
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        names: list[str] = []
        if node.type is None:
            names = ["bare except"]
        else:
            elts = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            names = [
                e.id
                for e in elts
                if isinstance(e, ast.Name) and e.id in ("Exception", "BaseException")
            ]
        if names and not any(isinstance(n, ast.Raise) for s in node.body for n in ast.walk(s)):
            self.hit(
                "MOD-86",
                node,
                f"{names[0]} swallows errors; catch what you expect or re-raise",
                "warn",
            )
        self.generic_visit(node)

    # capability use (MOD-70)
    def visit_Attribute(self, node: ast.Attribute) -> None:
        text = ast.unparse(node)
        for pattern, cap in CAPABILITY_USE:
            if pattern.search(text) and cap not in self.scan.used_caps:
                self.scan.used_caps[cap] = (self.rel, node.lineno)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        exc = node.exc
        if isinstance(exc, ast.Call):
            kind = (self.resolve(exc.func) or "").split(".")[-1]
            if kind.endswith("Error"):
                self._literals(exc.args, exc.keywords, "an error message")
            key = self._message_key(kind, exc)
            if key is not None:
                self.scan.keys.append((str(key.value), self.rel, key.lineno))
        self.generic_visit(node)

    @staticmethod
    def _message_key(kind: str, call: ast.Call) -> ast.Constant | None:
        arg: ast.expr | None = None
        if kind == "TransientError":
            arg = call.args[0] if call.args else None
        elif kind == "ConfigurationError":
            arg = call.args[1] if len(call.args) > 1 else None
        for kw in call.keywords:
            if kw.arg == "message_key":
                arg = kw.value
        return arg if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None

    def visit_Call(self, node: ast.Call) -> None:
        dotted = self.resolve(node.func)
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if dotted in WALL_CLOCK:
            self.hit(
                "MOD-28",
                node,
                f"{dotted}() reads the wall clock; use ctx.now()",
            )
        if dotted in ("asyncio.create_task", "asyncio.ensure_future"):
            self.hit(
                "MOD-12",
                node,
                f"{dotted}: start background work with ctx.create_task or ctx.scheduler",
            )
        elif attr == "create_task" and isinstance(node.func, ast.Attribute):
            base = ast.unparse(node.func.value)
            if re.search(
                r"asyncio\.(?:get_event_loop|get_running_loop)\(\)$|^loop$|\._loop$", base
            ):
                self.hit("MOD-12", node, "loop.create_task: use ctx.create_task or ctx.scheduler")
        if dotted == "print":
            self.hit("MOD-80", node, "print() is not allowed; use ctx.log")
        elif dotted is not None and dotted.split(".")[0] == "logging" and "." in dotted:
            self.hit("MOD-80", node, f"{dotted}() is not allowed; use ctx.log")
        if dotted in BLOCKING and self.async_depth:
            self.hit(
                "MOD-11",
                node,
                f"{dotted}() blocks the event loop; use ctx.run_blocking or an async API",
            )
        if attr in ("set_state", "patch_state"):
            self._literals(node.args, node.keywords, "state")
        elif attr in ("send_to_client", "send_notification"):
            self._literals(node.args[1:], node.keywords, "a payload")
        if attr == "send_notification" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self.scan.emitted.append((first.value, self.rel, first.lineno))
                if not UPPER_SNAKE.match(first.value):
                    self.hit(
                        "MOD-40", first, f"notification name {first.value!r} is not UPPER_SNAKE"
                    )
        if attr == "request" and node.args and "ctx" in ast.unparse(node.func):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if not UPPER_SNAKE.match(first.value):
                    self.hit("MOD-40", first, f"request name {first.value!r} is not UPPER_SNAKE")
        self.generic_visit(node)

    def _literals(self, args: list[ast.expr], keywords: list[ast.keyword], where: str) -> None:
        for expr in [*args, *(k.value for k in keywords)]:
            for const in _literal_strings(expr):
                text = str(const.value)
                if _USER_TEXT.search(text):
                    self.hit(
                        "MOD-61",
                        const,
                        f"user-visible literal {text[:40]!r} in {where}; use ctx.t()",
                    )


def _literal_strings(node: ast.expr) -> Iterator[ast.Constant]:
    """Yield string constants that end up as values; calls are not entered."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            yield node
    elif isinstance(node, ast.JoinedStr):
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                yield v
    elif isinstance(node, ast.Dict):
        for v in node.values:
            yield from _literal_strings(v)
    elif isinstance(node, ast.List | ast.Tuple | ast.Set):
        for e in node.elts:
            yield from _literal_strings(e)
    elif isinstance(node, ast.BinOp):
        yield from _literal_strings(node.left)
        yield from _literal_strings(node.right)


def scan_python(rel: str, tree: ast.Module, facts: ClassFacts | None) -> PyScan:
    """Run every Python rule on one file.

    Args:
        rel: Path relative to the module root.
        tree: The parsed file.
        facts: The module class, for rules that need its declarations.

    Returns:
        The findings and the facts other checks combine.
    """
    visitor = _Visitor(rel, facts, tree)
    visitor.visit(tree)
    return visitor.scan


def _classes(trees: dict[str, ast.Module]) -> dict[str, tuple[str, ast.ClassDef]]:
    return {
        n.name: (rel, n)
        for rel, tree in trees.items()
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef)
    }


def _is_module_config(
    name: str, classes: dict[str, tuple[str, ast.ClassDef]], seen: set[str]
) -> bool:
    if name in seen or name not in classes:
        return False
    seen.add(name)
    for base in classes[name][1].bases:
        text = ast.unparse(base)
        if text.split(".")[-1] == "ModuleConfig" or _is_module_config(
            text.split(".")[-1], classes, seen
        ):
            return True
    return False


def _field_call(value: ast.expr | None) -> ast.Call | None:
    if isinstance(value, ast.Call) and ast.unparse(value.func).split(".")[-1] == "Field":
        return value
    return None


def config_findings(
    trees: dict[str, ast.Module], facts: ClassFacts, en_keys: set[str] | None
) -> list[tuple[str, Finding]]:
    """Check the configuration model (MOD-45, MOD-46, MOD-47).

    Args:
        trees: Parsed package files by relative path.
        facts: The module class.
        en_keys: Keys of the English catalog, ``None`` when it cannot be read.

    Returns:
        ``(MOD id, finding)`` pairs.
    """
    out: list[tuple[str, Finding]] = []
    if facts.config_model is None:
        return out
    classes = _classes(trees)
    if facts.config_model not in classes:
        return out  # imported from elsewhere: the kit validates it at run time
    rel, node = classes[facts.config_model]
    if not _is_module_config(facts.config_model, classes, set()):
        out.append(("MOD-45", Finding(f"{node.name} must subclass ModuleConfig", rel, node.lineno)))
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
        ):
            text = ast.unparse(stmt.value)
            if re.search(r"extra\s*=\s*['\"](?:allow|ignore)['\"]", text):
                out.append(
                    ("MOD-45", Finding("model_config must keep extra='forbid'", rel, stmt.lineno))
                )
    for stmt in node.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
            continue
        if ast.unparse(stmt.annotation).startswith("ClassVar"):
            continue
        name, annotation = stmt.target.id, ast.unparse(stmt.annotation)
        call = _field_call(stmt.value)
        for kw in call.keywords if call else []:
            if kw.arg in ("title", "description") and isinstance(kw.value, ast.Constant):
                key = kw.value.value
                if not isinstance(key, str) or not UPPER_SNAKE.match(key):
                    out.append(
                        (
                            "MOD-46",
                            Finding(
                                f"{name}: {kw.arg} {key!r} must be a catalog key (UPPER_SNAKE)",
                                rel,
                                kw.value.lineno,
                            ),
                        )
                    )
                elif en_keys is not None and key not in en_keys:
                    out.append(
                        (
                            "MOD-46",
                            Finding(
                                f"{name}: {kw.arg} key {key} is not in the English catalog",
                                rel,
                                kw.value.lineno,
                            ),
                        )
                    )
        if "SecretStr" in annotation:
            extra = next(
                (
                    ast.unparse(k.value)
                    for k in (call.keywords if call else [])
                    if k.arg == "json_schema_extra"
                ),
                "",
            )
            if not re.search(r"['\"]secret['\"]\s*:\s*True", extra):
                out.append(
                    (
                        "MOD-47",
                        Finding(
                            f"{name} is a SecretStr without json_schema_extra={{'secret': True}}",
                            rel,
                            stmt.lineno,
                        ),
                    )
                )
        elif _SECRET_NAME.search(name) and re.search(r"\bstr\b", annotation):
            out.append(
                (
                    "MOD-47",
                    Finding(
                        f"{name} looks like a secret but is a plain str; use SecretStr",
                        rel,
                        stmt.lineno,
                        "warn",
                    ),
                )
            )
    return out
