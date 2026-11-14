"""Engine pages of the listeners: admin login, pairing, and the admin pages.

The pages carry no inline script or style (CORE-63): the pairing page loads
``/core/pair.js``. Their text comes from the ``CORE_WEB_*`` keys of the engine catalog
(core spec 8.3), in English until the admin follows the engine locale.

Implements:
    CORE-11: Core spec 0.15, §4.4 Contract rules.
    CORE-63: Core spec 0.38, §10.1 Security.
    CORE-253: Core spec 0.38, §10.1 Security.
    CORE-378: Core spec 0.40, §10.1 Security.
    CORE-381: Core spec 0.41, §9.1 Remote-control API.
    CORE-254: Core spec 0.12, §10.1 Security.
    -: Core spec 0.24, §10.5 Diagnostics and debugging.
    CORE-322: Core spec 0.25, §6.4 Web configuration editor.
    CORE-310: Core spec 0.40, §6.4 Web configuration editor.
    CORE-309: Core spec 0.26, §9.1 Remote-control API.
    CORE-329: Core spec 0.29, §11.1 Engine additions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path

from mm3.core.i18n.catalogs import CORE_CATALOG, Table

__all__ = [
    "ADMIN_PREFIXES",
    "TEXT",
    "admin_catalog_page",
    "admin_config_page",
    "admin_debug_page",
    "admin_home_page",
    "admin_texts",
    "form_texts",
    "language_names",
    "login_page",
    "message_page",
    "pair_page",
    "t",
]

ADMIN_PREFIXES = ("CORE_ADMIN_", "CORE_API_", "CORE_WEB_", "CORE_CFG_")
"""Keys the admin pages read from the engine catalog (CORE-11, CORE-298).

The ``CORE_CFG_`` codes give validation records their localized text in the editor
(CORE-206; ISS-010).
"""

TEXT: dict[str, str] = {
    key: text
    for key, text in Table(CORE_CATALOG).get("en").items()
    if key.startswith("CORE_WEB_") and isinstance(text, str)
}
"""English text of the engine's page keys, from the engine catalog (CORE-11)."""


def t(key: str) -> str:
    """Return the text of an engine page key, HTML-escaped.

    Args:
        key: A ``CORE_WEB_*`` key.

    Returns:
        The escaped text.
    """
    return escape(TEXT[key])


def _page(
    title_key: str,
    body: str,
    *,
    script: str | None = None,
    module: str | None = None,
    styles: tuple[str, ...] = (),
) -> str:
    tag = f'\n<script src="{escape(script)}" defer></script>' if script else ""
    if module:
        # No favicon request that would 404 (an empty data: icon, allowed by img-src).
        tag += '\n<link rel="icon" href="data:,">'
        tag += f'\n<script type="module" src="{escape(module)}"></script>'
    for href in styles:
        tag += f'\n<link rel="stylesheet" href="{escape(href)}">'
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{t(title_key)}</title>{tag}\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


ADMIN_LANGUAGES = ("en", "it", "fr", "es", "de")
"""The languages the admin can pick for its session (CORE-309)."""


def language_names() -> list[dict[str, str]]:
    """Return each admin language with its own name (``Italiano``, ``Deutsch``)."""
    from babel import Locale

    return [
        {"code": code, "name": str(Locale.parse(code).get_display_name(code)).capitalize()}
        for code in ADMIN_LANGUAGES
    ]


def _chain(locale: str) -> list[str]:
    chain = ["en"]
    base = locale.split("-")[0]
    if base != "en":
        chain.append(base)
    if locale not in chain:
        chain.append(locale)
    return chain


def form_texts(locale: str, roots: Mapping[str, Path | None]) -> dict[str, dict[str, str]]:
    """Return the form label and help texts per module (CORE-310).

    Args:
        locale: The admin language.
        roots: Catalog root per installed module.

    Returns:
        ``{"": core texts, module: texts}`` with only the ``CFG_*`` keys,
        English first, then the language; a module with no texts is left out.

    Implements:
        CORE-310: Core spec 0.40, §6.4 Web configuration editor.
    """
    out: dict[str, dict[str, str]] = {}
    for name, root in [("", CORE_CATALOG), *sorted(roots.items())]:
        if root is None:
            continue
        table = Table(root)
        texts: dict[str, str] = {}
        for loc in _chain(locale):
            for key, text in table.get(loc).items():
                if key.startswith("CFG_") and isinstance(text, str):
                    texts[key] = text
        if texts:
            out[name] = texts
    return out


def admin_texts(locale: str) -> dict[str, str]:
    """Return the admin page texts for a locale, falling back to English.

    Args:
        locale: BCP 47 tag, such as ``it`` or ``pt-BR``.

    Returns:
        Key to text for the keys of :data:`ADMIN_PREFIXES`.
    """
    table = Table(CORE_CATALOG)
    out: dict[str, str] = {}
    for loc in _chain(locale):
        for key, text in table.get(loc).items():
            if key.startswith(ADMIN_PREFIXES) and isinstance(text, str):
                out[key] = text
    return out


def _admin_page(
    element: str,
    script: str,
    current: str,
    extensions: Sequence[tuple[str, str]] = (),
    attrs: str = "",
) -> str:
    """An admin page: the header with navigation, then one element that loads its data.

    Args:
        element: The page's element.
        script: Its script under ``/admin/static/``.
        current: The page's own link, marked as current.
        extensions: ``(href, label)`` of the module pages (CORE-329).
        attrs: Extra attributes of the element (data only, CORE-63).
    """
    here = ' aria-current="page"'
    items = [
        ("/admin/config", t("CORE_WEB_NAV_CONFIG")),
        ("/admin/catalog", t("CORE_WEB_NAV_CATALOG")),
        ("/admin/debug", t("CORE_WEB_NAV_DEBUG")),
        *extensions,
    ]
    links = "".join(
        f'<a href="{escape(href)}"{here if href == current else ""}>{escape(label)}</a>'
        for href, label in items
    )
    space = " " if attrs else ""
    body = (
        '<header class="bar"><a class="home" href="/admin">'
        f"{t('CORE_WEB_ADMIN_TITLE')}</a><nav>{links}</nav>"
        '<form method="post" action="/admin/logout">'
        f'<button type="submit">{t("CORE_WEB_LOGOUT")}</button></form></header>\n'
        f"<{element}{space}{attrs}></{element}>"
    )
    return _page(
        "CORE_WEB_ADMIN_TITLE",
        body,
        module=f"/admin/static/{script}",
        styles=("/admin/static/admin.css",),
    )


def admin_debug_page(extensions: Sequence[tuple[str, str]] = ()) -> str:
    """Render ``/admin/debug``: the debug page element loads its data itself (10.5).

    Args:
        extensions: Links to the module pages (CORE-329).

    Returns:
        The HTML.
    """
    return _admin_page("mm3-admin-debug", "debug.js", "/admin/debug", extensions)


def admin_config_page(extensions: Sequence[tuple[str, str]] = ()) -> str:
    """Render ``/admin/config``, the page of the editor and the history (CORE-322).

    Args:
        extensions: Links to the module pages (CORE-329).

    Returns:
        The HTML.

    Implements:
        CORE-322: Core spec 0.25, §6.4 Web configuration editor.
        CORE-34: Core spec 0.22, §6.4 Web configuration editor.
    """
    return _admin_page("mm3-admin-config", "config.js", "/admin/config", extensions)


def admin_catalog_page(extensions: Sequence[tuple[str, str]] = ()) -> str:
    """Render ``/admin/catalog``, the page of the module catalog (CORE-120, CORE-133).

    ``catalog.js`` reads the catalog and the installer through the REST API and the
    ``installer`` topic of the admin channel; the page itself holds no data.

    Args:
        extensions: Links to the module pages (CORE-329).

    Returns:
        The HTML.

    Implements:
        CORE-120: Core spec 0.44, §10.6 Module catalog.
        CORE-133: Core spec 0.44, §10.7 Installer service.
    """
    return _admin_page("mm3-admin-catalog", "catalog.js", "/admin/catalog", extensions)


def admin_extension_page(href: str, extensions: Sequence[tuple[str, str]], attrs: str) -> str:
    """Render ``/admin/ext/<module>``: ``ext.js`` loads the module's element (CORE-329).

    Args:
        href: The page's own link.
        extensions: Links to every module page.
        attrs: The ``data-ext`` attribute with the page data (CORE-330).

    Returns:
        The HTML.

    Implements:
        CORE-329, CORE-330: Core spec 0.29, §11.1 Engine additions.
    """
    return _admin_page("mm3-admin-ext", "ext.js", href, extensions, attrs)


def login_page(error_key: str | None = None, next_path: str | None = None) -> str:
    """Render ``/admin/login`` (CORE-253, CORE-378).

    Args:
        error_key: Key of an error to show above the form.
        next_path: The admin page to return to after login, already checked
            by the caller; carried in a hidden ``next`` field.

    Returns:
        The HTML.

    Implements:
        CORE-253: Core spec 0.38, §10.1 Security.
        CORE-378: Core spec 0.40, §10.1 Security.
    """
    error = f'<p role="alert">{t(error_key)}</p>\n' if error_key else ""
    landing = (
        f'<input type="hidden" name="next" value="{escape(next_path, quote=True)}">\n'
        if next_path
        else ""
    )
    body = (
        f"<h1>{t('CORE_WEB_LOGIN_TITLE')}</h1>\n{error}"
        '<form method="post" action="/admin/login">\n'
        f"{landing}"
        f"<label>{t('CORE_WEB_LOGIN_TOKEN')} "
        '<input type="password" name="token" autocomplete="off" required autofocus></label>\n'
        f'<button type="submit">{t("CORE_WEB_LOGIN_SUBMIT")}</button>\n</form>'
    )
    return _page("CORE_WEB_LOGIN_TITLE", body)


def admin_home_page(extensions: Sequence[tuple[str, str]] = ()) -> str:
    """Render ``/admin``: the status view of the admin home (CORE-378, CORE-381).

    ``home.js`` fills the page from the ``status`` topic of the admin channel.

    Args:
        extensions: Links to the module pages (CORE-329).

    Returns:
        The HTML.

    Implements:
        CORE-378: Core spec 0.40, §10.1 Security.
        CORE-381: Core spec 0.41, §9.1 Remote-control API.
    """
    return _admin_page("mm3-admin-home", "home.js", "/admin", extensions)


def pair_page() -> str:
    """Render ``GET /pair``: ``/core/pair.js`` posts the token from the fragment (CORE-254).

    Returns:
        The HTML.
    """
    body = (
        f"<h1>{t('CORE_WEB_PAIR_TITLE')}</h1>\n<p>{t('CORE_WEB_PAIR_WORKING')}</p>\n"
        f"<noscript><p>{t('CORE_WEB_PAIR_NOSCRIPT')}</p></noscript>\n"
        '<form id="mm3-pair" method="post" action="/pair" hidden>'
        '<input type="hidden" name="token"></form>'
    )
    return _page("CORE_WEB_PAIR_TITLE", body, script="/core/pair.js")


def message_page(title_key: str, message_key: str) -> str:
    """Render a page with one message.

    Args:
        title_key: Key of the title.
        message_key: Key of the message.

    Returns:
        The HTML.
    """
    return _page(title_key, f"<h1>{t(title_key)}</h1>\n<p>{t(message_key)}</p>")
