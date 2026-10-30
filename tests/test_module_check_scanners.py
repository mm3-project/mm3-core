"""Static scanners of ``mm3 module check``: JavaScript tokenizer and rules, Python AST rules."""

from __future__ import annotations

import ast

import pytest

from mm3.core.modcheck.jsscan import JsContext, notified_names, scan_js, tokenize
from mm3.core.modcheck.pyscan import config_findings, find_class, scan_python

CTX = JsContext("demo", "mm3-demo", "demo.js")
DEFINE = 'customElements.define("mm3-demo", E);\n'


def js_ids(src: str, ctx: JsContext = CTX) -> list[str]:
    found, _ = scan_js("static/demo.js", src, ctx)
    return [mod_id for mod_id, _ in found]


# --- tokenizer ----------------------------------------------------------------------


def kinds(src: str) -> list[tuple[str, str]]:
    return [(t.kind, t.text) for t in tokenize(src) if t.kind != "comment"]


def test_core_281_tokenizer_skips_comments_and_keeps_lines() -> None:
    toks = tokenize("// fetch(1)\n/* a\nb */ x")
    assert [t.kind for t in toks] == ["comment", "comment", "name"]
    assert toks[2].line == 3


def test_core_281_tokenizer_strings_hide_their_content() -> None:
    assert kinds("a = \"fetch(\" + 'it\\'s'") == [
        ("name", "a"),
        ("punct", "="),
        ("str", "fetch("),
        ("punct", "+"),
        ("str", "it\\'s"),
    ]


def test_core_281_tokenizer_template_substitutions_nest() -> None:
    [tok] = tokenize("`a ${ f(`b ${c}`, {x: 1}) } d`")
    assert tok.kind == "tmpl"
    assert tok.parts == ["a ", " d"]
    names = [t.text for t in tok.exprs[0] if t.kind == "name"]
    assert names == ["f", "x"]
    assert tok.exprs[0][2].kind == "tmpl"


def test_core_281_tokenizer_tells_regex_from_division() -> None:
    assert kinds("a / b / c")[1] == ("punct", "/")
    assert kinds("x = /a\\/b[/]c/gi.test(s)")[2] == ("regex", "/a\\/b[/]c/gi")
    assert kinds("return /x/")[1][0] == "regex"
    assert kinds("(a) / 2")[3] == ("punct", "/")


def test_core_281_tokenizer_accepts_modern_syntax() -> None:
    src = "class A { #p = 1n; static { } async *g() { yield a?.b ?? c; } }\nlet x = 0x1F_FF **= 2"
    assert any(t.text == "#p" for t in tokenize(src))
    assert ("punct", "?.") in kinds(src)


def test_core_281_tokenizer_survives_unterminated_input() -> None:
    assert tokenize("'abc\nx = `open")  # no exception


# --- JavaScript rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ('fetch("/x");', ["MOD-32"]),
        ("const x = new XMLHttpRequest();", ["MOD-32"]),
        ('new WebSocket("ws://x");', ["MOD-32"]),
        ('window.fetch("/x");', ["MOD-32"]),
        ("this.fetch(1);", []),
        ("// fetch(1)\nconst s = 'fetch(1)';", []),
        ("el.innerHTML = data;", ["MOD-31"]),
        ("el.innerHTML += `<b>${x}</b>`;", ["MOD-31"]),
        ('el.innerHTML = "<b>fixed</b>";', []),
        ("el.insertAdjacentHTML('beforeend', x);", ["MOD-31"]),
        ("setInterval(f, 1000);", ["MOD-35"]),
        ("window.setTimeout(f, 1);", ["MOD-35"]),
        ("this.setInterval(f, 1000);", []),
        ("console.log(1);", ["MOD-80"]),
        ("console.debug(1);", []),
        ("window.state = {};", ["MOD-30"]),
        ("document.head.appendChild(s);", ["MOD-33"]),
        ("document.body.style.color = 'red';", ["MOD-33"]),
        ('import x from "lit-html";', ["MOD-37"]),
        ('import x from "https://cdn.example/x.js";', ["MOD-37"]),
        ('import x from "../../other/x.js";', ["MOD-37"]),
        ('import x from "./helper.js";', []),
        ('import { html } from "/core/mm3.js";', []),
        ('import("./lazy.js");', []),
        ("import(name);", ["MOD-37"]),
        ('export * from "lodash";', ["MOD-37"]),
        ("html`<p>Hello world</p>`", ["MOD-61"]),
        ('html`<p class="a b">${this.t("K")}</p>`', []),
        ('this.shadow.textContent = "Not available";', ["MOD-61"]),
        ('this.notify("X", { text: "Some words" });', ["MOD-61"]),
        ('this.notify("X", { code: "abc" });', []),
    ],
)
def test_core_281_js_rules(src: str, expected: list[str]) -> None:
    assert js_ids(src + "\n" + DEFINE) == expected


def test_core_281_js_define_count_and_tag() -> None:
    assert scan_js("s.js", DEFINE, CTX)[1] == 1
    assert scan_js("s.js", "const a = 1;", CTX)[1] == 0
    assert js_ids('customElements.define("mm3-other", E);') == ["MOD-30"]
    assert scan_js("s.js", DEFINE * 2, CTX)[1] == 2


def test_core_281_js_notified_names() -> None:
    src = 'this.notify("A_B", {}); other.notify("X"); this.notify(name);'
    assert notified_names(src) == [("A_B", 1)]


def test_core_281_js_findings_carry_the_line() -> None:
    found, _ = scan_js("static/demo.js", "const a = 1;\n\nfetch(1);\n" + DEFINE, CTX)
    assert [(m, f.file, f.line) for m, f in found] == [("MOD-32", "static/demo.js", 3)]


# --- Python rules ---------------------------------------------------------------------


def py_ids(src: str) -> list[str]:
    tree = ast.parse(src)
    return [m for m, _ in scan_python("m.py", tree, None).findings]


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("import time\nx = time.time()", ["MOD-28"]),
        ("from time import time\nx = time()", ["MOD-28"]),
        ("import datetime as dt\nx = dt.datetime.now()", ["MOD-28"]),
        ("from datetime import datetime as D\nx = D.utcnow()", ["MOD-28"]),
        ("from datetime import date\nx = date.today()", ["MOD-28"]),
        ("import time\nx = time.monotonic()", []),
        ("def f(ctx):\n    return ctx.now()", []),
        ("import asyncio\nasyncio.create_task(f())", ["MOD-12"]),
        ("import asyncio\nasyncio.get_event_loop().create_task(f())", ["MOD-12"]),
        ("def f(ctx):\n    ctx.create_task(g(), name='x')", []),
        ("import httpx", ["MOD-23"]),
        ("from requests import get", ["MOD-23"]),
        ("import urllib.request", ["MOD-23"]),
        ("import json", []),
        ("print('x')", ["MOD-80"]),
        ("import logging\nlogging.getLogger(__name__).info('x')", ["MOD-80"]),
        ("def f(ctx):\n    ctx.log.info('forecast_fetched')", []),
        ("try:\n    pass\nexcept Exception:\n    pass", ["MOD-86"]),
        ("try:\n    pass\nexcept:\n    pass", ["MOD-86"]),
        ("try:\n    pass\nexcept Exception as e:\n    raise TransientError('K') from e", []),
        ("try:\n    pass\nexcept ValueError:\n    pass", []),
        ("import time\nasync def f():\n    time.sleep(1)", ["MOD-11"]),
        ("import time\ndef f():\n    time.sleep(1)", []),
        ("def f(ctx):\n    ctx.set_state({'text': 'Not available'})", ["MOD-61"]),
        ("def f(ctx):\n    ctx.set_state({'text': ctx.t('K'), 'part': 'morning'})", []),
        ("def f(ctx):\n    ctx.patch_state({'k': f'Temperature is {1}'})", ["MOD-61"]),
        ("def f():\n    raise TransientError('Network is down')", ["MOD-61"]),
        ("async def f(ctx):\n    await ctx.send_notification('lower_case', {})", ["MOD-40"]),
        ("async def f(ctx):\n    await ctx.send_notification('WEATHER_UPDATED', {})", []),
        ("async def f(ctx):\n    await ctx.request('lower', {})", ["MOD-40"]),
    ],
)
def test_core_281_python_rules(src: str, expected: list[str]) -> None:
    assert py_ids(src) == expected


def test_core_281_python_scan_collects_what_other_checks_combine() -> None:
    src = (
        "async def f(ctx):\n"
        "    await ctx.http.get('x')\n"
        "    await ctx.mqtt.publish('t', {})\n"
        "    await ctx.send_notification('A_B', {})\n"
        "    raise ConfigurationError('field', 'BAD_KEY')\n"
    )
    scan = scan_python("m.py", ast.parse(src), None)
    assert set(scan.used_caps) == {"http.outbound", "mqtt.publish"}
    assert scan.emitted == [("A_B", "m.py", 4)]
    assert scan.keys == [("BAD_KEY", "m.py", 5)]


CLASS = """
from typing import ClassVar
from mm3 import MirrorModule

class M(MirrorModule):
    name = "m"
    element: ClassVar[str | None] = "mm3-m"
    scripts = ["m.js"]
    capabilities = {"http.outbound"}
    emits = {"M_UPDATED": X, "M_OTHER": Y}
    consumes = dict_from()
    min_size_px = (1, 2)
    config_model = Cfg

    async def start(self): ...
    def stop(self): ...
"""


def test_core_281_class_facts_are_read_without_importing() -> None:
    facts = find_class({"m.py": ast.parse(CLASS)}, "M")
    assert facts is not None
    assert (facts.name, facts.element, facts.scripts) == ("m", "mm3-m", ["m.js"])
    assert facts.capabilities == {"http.outbound"}
    assert set(facts.emits) == {"M_UPDATED", "M_OTHER"}
    assert facts.non_literal == ["consumes"]
    assert facts.min_size
    assert facts.config_model == "Cfg"
    assert facts.hooks["start"][0]
    assert not facts.hooks["stop"][0]
    assert find_class({"m.py": ast.parse(CLASS)}, "Missing") is None


CONFIG = """
from pydantic import Field, SecretStr
from mm3 import ModuleConfig

class Cfg(ModuleConfig):
    api_key: SecretStr = Field(json_schema_extra={"secret": True})
    token: SecretStr
    password: str = ""
    lat: float = Field(0, title="CFG_LAT", description="CFG_LAT_HELP")
    lon: float = Field(0, title="Longitude")
    other: float = Field(0, title="NOT_IN_CATALOG")

class Plain:
    x: int = 1
"""


def test_core_281_config_findings() -> None:
    trees = {"c.py": ast.parse(CONFIG + "\nclass M:\n    config_model = Cfg\n")}
    facts = find_class(trees, "M")
    assert facts is not None
    found = config_findings(trees, facts, {"CFG_LAT", "CFG_LAT_HELP"})
    texts = sorted((m, f.message.split(":")[0].split(" ")[0], f.severity) for m, f in found)
    assert ("MOD-47", "token", "fail") in texts
    assert ("MOD-47", "password", "warn") in texts
    assert ("MOD-46", "lon", "fail") in texts
    assert ("MOD-46", "other", "fail") in texts
    assert not [t for t in texts if t[1] in ("api_key", "lat")]


def test_core_281_config_model_must_subclass_module_config() -> None:
    trees = {"c.py": ast.parse(CONFIG + "\nclass M:\n    config_model = Plain\n")}
    facts = find_class(trees, "M")
    assert facts is not None
    assert [m for m, _ in config_findings(trees, facts, None)] == ["MOD-45"]


@pytest.mark.parametrize(
    "src",
    [
        "import httpx",
        "from httpx import AsyncClient",
        "import requests",
        "from requests.adapters import HTTPAdapter",
        "import aiohttp",
        "import urllib.request",
        "from urllib import request",
        "import http.client",
        "import urllib3",
    ],
)
def test_mod_23_importing_an_http_library_is_flagged(src: str) -> None:
    assert py_ids(src) == ["MOD-23"]


@pytest.mark.parametrize(
    "src",
    [
        "async def f(ctx):\n    return await ctx.http.get('https://x/')",
        "from urllib.parse import urljoin",
        "import json\nimport ssl",
        "from . import httpx_helpers",
    ],
)
def test_mod_23_ctx_http_and_non_client_imports_pass(src: str) -> None:
    assert py_ids(src) == []
