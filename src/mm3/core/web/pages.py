"""Engine pages of the listeners: admin login, pairing, and the admin placeholder.

The pages carry no inline script or style (CORE-63): the pairing page loads
``/core/pair.js``. Their text comes from the ``CORE_WEB_*`` keys of the engine catalog
(core spec 8.3), in English until the admin follows the engine locale.

Implements:
    CORE-11: Core spec 0.15, §4.4 Contract rules.
    CORE-63: Core spec 0.12, §10.1 Security.
    CORE-253: Core spec 0.12, §10.1 Security.
    CORE-254: Core spec 0.12, §10.1 Security.
"""

from __future__ import annotations

from html import escape

from mm3.core.i18n.catalogs import CORE_CATALOG, Table

__all__ = ["TEXT", "admin_home_page", "login_page", "message_page", "pair_page", "t"]

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


def _page(title_key: str, body: str, *, script: str | None = None) -> str:
    tag = f'\n<script src="{escape(script)}" defer></script>' if script else ""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{t(title_key)}</title>{tag}\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def login_page(error_key: str | None = None) -> str:
    """Render ``/admin/login`` (CORE-253).

    Args:
        error_key: Key of an error to show above the form.

    Returns:
        The HTML.
    """
    error = f'<p role="alert">{t(error_key)}</p>\n' if error_key else ""
    body = (
        f"<h1>{t('CORE_WEB_LOGIN_TITLE')}</h1>\n{error}"
        '<form method="post" action="/admin/login">\n'
        f"<label>{t('CORE_WEB_LOGIN_TOKEN')} "
        '<input type="password" name="token" autocomplete="off" required autofocus></label>\n'
        f'<button type="submit">{t("CORE_WEB_LOGIN_SUBMIT")}</button>\n</form>'
    )
    return _page("CORE_WEB_LOGIN_TITLE", body)


def admin_home_page() -> str:
    """Render ``/admin`` until the admin UI of milestone 6 replaces it.

    Returns:
        The HTML.
    """
    body = (
        f"<h1>{t('CORE_WEB_ADMIN_TITLE')}</h1>\n<p>{t('CORE_WEB_ADMIN_PENDING')}</p>\n"
        '<form method="post" action="/admin/logout">'
        f'<button type="submit">{t("CORE_WEB_LOGOUT")}</button></form>'
    )
    return _page("CORE_WEB_ADMIN_TITLE", body)


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
