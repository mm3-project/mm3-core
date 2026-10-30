"""The static stage of ``mm3 module check``: every row checked from the source and the wheel.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    MOD-01, MOD-02, MOD-03, MOD-04: Module Contract 1.0 rev 10, §2 Package and metadata.
    MOD-05, MOD-07, MOD-08: Module Contract 1.0 rev 10, §2 Package and metadata.
    MOD-11, MOD-12: Module Contract 1.0 rev 10, §3.3 Rules.
    MOD-17: Module Contract 1.0 rev 10, §3.4 Error semantics.
    MOD-23: Module Contract 1.0 rev 10, §4.3 HttpClient.
    MOD-32, MOD-33, MOD-35: Module Contract 1.0 rev 10, §5.3 Rules.
    MOD-24, MOD-29, MOD-40, MOD-41, MOD-44: Module Contract 1.0 rev 10, §6.1 Messaging.
    MOD-52: Module Contract 1.0 rev 10, §6.3 Layout and preview.
    MOD-60, MOD-61, MOD-62, MOD-63, MOD-67: Module Contract 1.0 rev 10, §7 Internationalization.
    MOD-70, MOD-71: Module Contract 1.0 rev 10, §8 Capabilities and grants.
    MOD-80, MOD-86: Module Contract 1.0 rev 10, §9 Logging, health and errors.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from mm3.core.i18n import REQUIRED_LOCALES
from mm3.core.modcheck.jsscan import JsContext, notified_names, scan_js
from mm3.core.modcheck.project import GROUP, ModuleProject
from mm3.core.modcheck.pyscan import (
    UPPER_SNAKE,
    ClassFacts,
    PyScan,
    config_findings,
    find_class,
    scan_python,
)
from mm3.core.modcheck.report import Check, Finding, apply_suppressions, checks_from_findings
from mm3_build.catalogs import CatalogError, compiled, po_files

__all__ = ["bounded_to_one_major", "run_static"]

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SEMVER_NORMALISED = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(?:a|b|rc)\d+)?(?:\.dev\d+)?$"
)
SCOPED = {"http.outbound": "http", "mqtt.publish": "mqtt", "mqtt.subscribe": "mqtt"}
CLIENT_MESSAGE_TYPE = re.compile(r"""\.(?:send_to_client|send)\(\s*["']([^"']+)["']""")
Found = dict[str, list[Finding]]


def bounded_to_one_major(spec: SpecifierSet) -> bool:
    """Say whether a requirement is bounded to one major version (MOD-03).

    Args:
        spec: The specifier of the ``mm3-core`` requirement.

    Returns:
        ``True`` for ``>=1.0,<2``, ``~=1.0``, ``==1.*`` and the like.
    """
    lower: Version | None = None
    upper: Version | None = None
    for item in spec:
        version = Version(item.version.replace(".*", ".0"))
        if item.operator in (">=", ">", "==", "~="):
            lower = version if lower is None else max(lower, version)
        if item.operator == "~=" or (item.operator == "==" and item.version.endswith(".*")):
            upper = Version(str(version.major + 1))
        elif item.operator in ("<", "<="):
            upper = version if upper is None else min(upper, version)
    if lower is None or upper is None or upper <= lower:
        return False
    # The upper bound must not reach past the next major: <2, <2.0, <2.0.0 or <=1.x.
    return upper <= Version(str(lower.major + 1))


def _section(readme: str, title: str) -> str | None:
    """Return the body of the README section whose heading starts with ``title``."""
    lines = readme.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m and m.group(2).lower().startswith(title.lower()):
            level, body = len(m.group(1)), []
            for nxt in lines[i + 1 :]:
                n = re.match(r"^(#{1,6})\s", nxt)
                if n and len(n.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body)
    return None


class _Static:
    def __init__(self, mod: ModuleProject) -> None:
        self.mod = mod
        self.found: Found = defaultdict(list)
        self.facts: ClassFacts | None = None
        self.en_keys: set[str] | None = None
        self.scans: list[PyScan] = []

    def add(self, mod_id: str, message: str, file: str | None = None, line: int | None = None,
            severity: str = "fail") -> None:  # fmt: skip
        f = Finding(message, file, line)
        f.severity = "warn" if severity == "warn" else "fail"
        self.found[mod_id].append(f)

    def run(self) -> None:
        self.read_class()
        self.package()
        self.catalogs()
        self.python()
        self.frontend()
        self.declarations()
        self.capabilities()
        self.readme()

    # --- §2 package and metadata -----------------------------------------------------
    def package(self) -> None:
        mod = self.mod
        dist = re.sub(r"[-_.]+", "-", mod.dist).lower()
        if not dist.startswith("mm3-") or len(dist) <= 4:
            self.add(
                "MOD-01", f"distribution name {mod.dist!r} must start with 'mm3-'", "pyproject.toml"
            )
        expected = "mm3_" + dist[4:].replace("-", "_")
        if mod.pkg and mod.pkg != expected and dist.startswith("mm3-"):
            self.add("MOD-01", f"import package {mod.pkg!r} must be {expected!r}", "pyproject.toml")
        if mod.pkg and mod.pkg_dir is None:
            self.add(
                "MOD-01", f"package directory {mod.pkg!r} not found under src/ or the project root"
            )
        version = mod.version or next(iter(mod.metadata.get("Version", [])), None)
        if version is None:
            self.add(
                "MOD-02", "no version (set project.version or build the wheel)", "pyproject.toml"
            )
        elif not (
            SEMVER.match(version) or (mod.version is None and SEMVER_NORMALISED.match(version))
        ):
            self.add(
                "MOD-02",
                f"version {version!r} is not Semantic Versioning (MAJOR.MINOR.PATCH)",
                "pyproject.toml",
            )
        self._core_dependency()
        eps = mod.entry_points
        if len(eps) != 1:
            self.add(
                "MOD-04",
                f"expected exactly one entry point in group {GROUP!r}, found {len(eps)}",
                "pyproject.toml",
            )
        for rel, line, msg in mod.syntax_errors:
            self.add("MOD-04", f"the module does not parse: {msg}", rel, line)
        self._wheel_contents()

    def _core_dependency(self) -> None:
        for dep in self.mod.deps:
            try:
                req = Requirement(dep)
            except InvalidRequirement:
                continue
            if re.sub(r"[-_.]+", "-", req.name).lower() == "mm3-core":
                if not bounded_to_one_major(req.specifier):
                    self.add(
                        "MOD-03",
                        f"{dep!r} is not bounded to one API major (use mm3-core>=1.0,<2)",
                        "pyproject.toml",
                    )
                return
        self.add("MOD-03", "no dependency on mm3-core (use mm3-core>=1.0,<2)", "pyproject.toml")

    def _wheel_contents(self) -> None:
        mod = self.mod
        if mod.wheel is None:
            return
        names, pkg = mod.wheel_names, mod.pkg
        has_static = any(n.startswith(f"{pkg}/static/") for n in names)
        src_static = mod.pkg_dir is not None and (mod.pkg_dir / "static").is_dir()
        if src_static and not has_static:
            self.add("MOD-05", "static/ exists in the source but not in the wheel (package data)")
        for po in (n for n in names if n.endswith("/LC_MESSAGES/messages.po")):
            base = po.rsplit("/LC_MESSAGES/", 1)[0]
            for need in (f"{base}/LC_MESSAGES/messages.mo", f"{base}/messages.json"):
                if need not in names:
                    self.add(
                        "MOD-05",
                        f"{need} is missing in the wheel; enable the mm3-i18n build hook",
                        mod.wheel.name,
                    )
        src_locales = mod.pkg_dir is not None and (mod.pkg_dir / "locales").is_dir()
        if src_locales and not any(n.startswith(f"{pkg}/locales/") for n in names):
            self.add("MOD-05", "locales/ exists in the source but not in the wheel (package data)")
        tag = mod.wheel.name.removesuffix(".whl").split("-")[-3:]
        pure = tag == ["py3", "none", "any"] or (
            len(tag) == 3 and tag[0].startswith("py") and tag[2] == "any"
        )
        manylinux = tag[-1].startswith("manylinux") and tag[-1].endswith("x86_64")
        if not pure and not manylinux:
            self.add(
                "MOD-07",
                f"wheel tag {'-'.join(tag)} is neither pure Python nor manylinux x86-64",
                mod.wheel.name,
            )

    # --- Python ----------------------------------------------------------------------
    def read_class(self) -> None:
        mod = self.mod
        entry = next(iter(mod.entry_points.values()), "")
        cls = entry.split(":")[1] if ":" in entry else ""
        if cls:
            self.facts = find_class(mod.trees, cls)
            if self.facts is None and not mod.syntax_errors:
                self.add(
                    "MOD-04",
                    f"class {cls} named by the entry point is not defined in the package",
                    "pyproject.toml",
                )
        elif entry:
            self.add("MOD-04", f"entry point {entry!r} must be 'module:Class'", "pyproject.toml")

    def python(self) -> None:
        mod, facts = self.mod, self.facts
        if facts is not None:
            if facts.name is None:
                self.add(
                    "MOD-04", "the class has no literal name attribute", facts.file, facts.line
                )
            elif mod.entry_points and (key := next(iter(mod.entry_points))) != facts.name:
                self.add(
                    "MOD-04",
                    f"entry point key {key!r} must equal name = {facts.name!r}",
                    "pyproject.toml",
                )
            for hook in (
                "start",
                "stop",
                "suspend",
                "resume",
                "reconfigure",
                "on_notification",
                "on_client_message",
                "on_request",
            ):
                if hook in facts.hooks and not facts.hooks[hook][0]:
                    self.add("MOD-11", f"{hook}() must be async", facts.file, facts.hooks[hook][1])
            if facts.element is not None and facts.name and facts.element != f"mm3-{facts.name}":
                self.add(
                    "MOD-30",
                    f"element {facts.element!r} must be 'mm3-{facts.name}'",
                    facts.file,
                    facts.line,
                )
            if facts.element is not None and not facts.min_size:
                self.add(
                    "MOD-52",
                    "no min_size_px declared for a module with a front-end",
                    facts.file,
                    facts.line,
                )
        for rel, tree in mod.trees.items():
            scan = scan_python(rel, tree, facts)
            self.scans.append(scan)
            for mod_id, finding in scan.findings:
                self.found[mod_id].append(finding)
        if facts is not None:
            for mod_id, finding in config_findings(mod.trees, facts, self.en_keys):
                self.found[mod_id].append(finding)
            for key, rel, line in (k for scan in self.scans for k in scan.keys):
                if self.en_keys is not None and key not in self.en_keys:
                    self.add(
                        "MOD-17", f"message_key {key} is not in the English catalog", rel, line
                    )

    # --- §7 catalogs -----------------------------------------------------------------
    def catalogs(self) -> None:
        mod = self.mod
        facts = self.facts
        locales = facts.locales_dir if facts else "locales"
        if mod.wheel_dir is None or locales is None or not mod.pkg:
            return
        root = mod.wheel_dir / mod.pkg / locales
        shown = f"{mod.pkg}/{locales}"
        pos = po_files(root) if root.is_dir() else {}
        if not pos:
            self.add("MOD-62", f"no catalogs under {shown}/<lang>/LC_MESSAGES/messages.po")
            return
        from mm3_build.catalogs import check_locales

        for problem in check_locales(root, REQUIRED_LOCALES):
            inside = problem.path.is_relative_to(root)
            where = f"{shown}/{problem.path.relative_to(root).as_posix()}" if inside else shown
            self.add("MOD-62", problem.message, where)
        en = pos.get("en")
        if en is None:
            return
        try:
            _, _, entries = compiled(en)
        except CatalogError:
            return
        self.en_keys = set(entries)
        en_rel = f"{shown}/en/LC_MESSAGES/messages.po"
        for key, value in entries.items():
            if not UPPER_SNAKE.match(key):
                self.add(
                    "MOD-60", f"message id {key!r} must be an UPPER_SNAKE key, not text", en_rel
                )
            if isinstance(value, str) and re.search(r"\{(?:count|n|number)\}", value):
                self.add(
                    "MOD-63",
                    f"{key} has a {{count}} placeholder but no plural forms",
                    en_rel,
                    severity="warn",
                )
        if facts is not None and facts.element is not None and "HEADER" not in entries:
            self.add("MOD-67", "no HEADER catalog key for appearance.header", en_rel)

    # --- §5 front-end ----------------------------------------------------------------
    def frontend(self) -> None:
        mod, facts = self.mod, self.facts
        if facts is None or not mod.pkg_dir:
            return
        static = (mod.pkg_dir / "static").resolve()
        defines = 0
        for rel, text in mod.js.items():
            inside = Path(mod.root, rel).resolve().relative_to(static).as_posix()
            found, count = scan_js(rel, text, JsContext(facts.name or "", facts.element, inside))
            defines += count
            for mod_id, finding in found:
                self.found[mod_id].append(finding)
            self.scans.append(PyScan())
            for name, line in notified_names(text):
                self._emitted_js.append((name, rel, line))
        if facts.element is not None and defines != 1:
            self.add(
                "MOD-30",
                f"the scripts must define exactly one element, found {defines}",
                f"{mod.pkg_dir.name}/static",
            )
        for rel, text in mod.css.items():
            for m in re.finditer(r"@font-face|@import\s+url\(\s*['\"]?https?:", text):
                self.add(
                    "MOD-33",
                    f"{m.group().strip()} sets global fonts or loads from outside",
                    rel,
                    text.count("\n", 0, m.start()) + 1,
                )
        if mod.wheel is not None:
            for script in (*facts.scripts, *facts.styles):
                if f"{mod.pkg}/static/{script}" not in mod.wheel_names:
                    self.add(
                        "MOD-05",
                        f"{script} (declared in scripts/styles) is not in the wheel under static/",
                        facts.file,
                        facts.line,
                    )

    _emitted_js: list[tuple[str, str, int]]  # set in __init__ of run()

    # --- §6.1 declarations -----------------------------------------------------------
    def declarations(self) -> None:
        facts = self.facts
        if facts is None:
            return
        for attr in ("emits", "consumes", "answers"):
            for name, line in getattr(facts, attr).items():
                if not UPPER_SNAKE.match(name):
                    self.add("MOD-40", f"{attr} name {name!r} is not UPPER_SNAKE", facts.file, line)
        for attr in facts.non_literal:
            if attr in ("emits", "consumes", "answers"):
                self.add(
                    "MOD-29",
                    f"{attr} is not a literal mapping; static checks skip it",
                    facts.file,
                    facts.line,
                    "warn",
                )
        declared = set(facts.emits)
        literal = "emits" not in facts.non_literal
        for name, rel, line in [*(e for s in self.scans for e in s.emitted), *self._emitted_js]:
            if literal and name not in declared:
                self.add("MOD-29", f"{name} is sent but not declared in emits", rel, line)
        if facts.answers and "on_request" not in facts.hooks:
            self.add(
                "MOD-24",
                "answers declares requests but the class has no on_request()",
                facts.file,
                facts.line,
            )

    # --- §8 capabilities -------------------------------------------------------------
    def capabilities(self) -> None:
        facts = self.facts
        if facts is None or "capabilities" in facts.non_literal:
            return
        used: dict[str, tuple[str, int]] = {}
        for scan in self.scans:
            for cap, place in scan.used_caps.items():
                used.setdefault(cap, place)
        if facts.admin:
            used.setdefault("admin.extension", (facts.file, facts.line))
        for cap, (rel, line) in sorted(used.items()):
            if cap not in facts.capabilities:
                self.add("MOD-70", f"uses {cap} without declaring it in capabilities", rel, line)
        for cap in sorted(facts.capabilities - set(used)):
            self.add(
                "MOD-71",
                f"declares {cap} but the code never uses it; request the narrowest set",
                facts.file,
                facts.line,
                "warn",
            )

    # --- README ----------------------------------------------------------------------
    def readme(self) -> None:
        mod, facts = self.mod, self.facts
        if mod.readme is None:
            self.add("MOD-08", "no README.md (purpose, config example, screenshot)")
        else:
            _, text = mod.readme
            screenshot = mod.meta.get("tool", {}).get("mm3", {}).get("screenshot")
            has_shot = bool(re.search(r"!\[[^\]]*\]\(", text)) or (
                isinstance(screenshot, str) and (mod.root / screenshot).is_file()
            )
            if "```" not in text:
                self.add(
                    "MOD-08", "README has no configuration example (a code block)", mod.readme[0]
                )
            if not has_shot:
                self.add("MOD-08", "README shows no screenshot", mod.readme[0])
        if facts is None:
            return
        names = {"MOD-41": [*facts.emits, *facts.consumes], "MOD-24": list(facts.answers)}
        text = mod.readme[1] if mod.readme else ""
        notifications = _section(text, "Notifications")
        readme_name = mod.readme[0] if mod.readme else "README.md"
        for mod_id, listed in names.items():
            if not listed:
                continue
            if notifications is None:
                self.add(
                    mod_id,
                    "README has no 'Notifications' section listing the declared names",
                    readme_name,
                )
                continue
            for name in listed:
                if not re.search(rf"(?<![A-Z0-9_]){re.escape(name)}(?![A-Z0-9_])", notifications):
                    self.add(
                        mod_id,
                        f"{name} is declared but not listed in README 'Notifications'",
                        readme_name,
                    )
        scoped = sorted(c for c in facts.capabilities if c in SCOPED)
        if scoped:
            grants = _section(text, "Grants")
            if grants is None:
                self.add(
                    "MOD-71",
                    "README has no 'Grants' section for the scoped capabilities",
                    readme_name,
                )
            else:
                for cap in scoped:
                    if SCOPED[cap] not in grants.lower():
                        self.add(
                            "MOD-71",
                            f"README 'Grants' does not mention {SCOPED[cap]} ({cap})",
                            readme_name,
                        )


def run_static(mod: ModuleProject) -> tuple[list[Check], ClassFacts | None]:
    """Run every static check.

    Args:
        mod: The loaded project, with the wheel attached when it could be built.

    Returns:
        The static checks in section 11 order (suppressions applied), and the
        module class facts when the class was found.
    """
    run = _Static(mod)
    run._emitted_js = []
    run.run()
    checks = checks_from_findings("static", run.found)
    apply_suppressions(checks, mod.sources)
    return checks, run.facts
