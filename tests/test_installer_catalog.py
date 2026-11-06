"""Tests of the catalog index, downloads, verification, the index store and auth.

Implements:
- CORE-350 (Core spec 0.33, §10.6 Module catalog): index format and signer.
- CORE-351 (Core spec 0.35, §10.6 Module catalog): verified only by the installer,
  serial guard, engine copy, staleness.
- CORE-352 (Core spec 0.33, §10.6 Module catalog): PEP 740 provenance, real
  attestation from PyPI verified offline.
- CORE-370 (Core spec 0.35, §10.7 Installer service): HTTPS, hosts, redirects, sizes.
- CORE-132, CORE-369 (Core spec 0.37, §10.7 Installer service): passphrase, TOTP, lock.
- CORE-373 (Core spec 0.37, §10.7 Installer service): no symlinks followed in the
  engine's directories.
"""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
import urllib.request
from datetime import timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pyotp
import pytest

from installer_fakes import (
    NOW,
    Clock,
    FakeFetcher,
    FakeVerification,
    entry,
    index_bytes,
    make_wheel,
    release,
)
from mm3.core import catalog
from mm3.core.catalog import CatalogError, Publisher, parse_index, read_cached
from mm3.installer.auth import Auth, AuthError, LockedError
from mm3.installer.fetch import LIMIT_INDEX, FetchError, UrllibFetcher, check_url
from mm3.installer.paths import Layout
from mm3.installer.store import CatalogStore
from mm3.installer.verify import SigstoreVerification, VerifyError

FIXTURES = Path(__file__).parent / "fixtures" / "installer"
REAL_WHEEL = FIXTURES / "pypi_attestations-0.0.31-py3-none-any.whl"
REAL_PROVENANCE = FIXTURES / "pypi_attestations-0.0.31-py3-none-any.whl.provenance.json"
REAL_PUBLISHER = Publisher(repository="pypi/pypi-attestations", workflow="release.yml")


def sample_index(**kw: Any) -> bytes:
    data = make_wheel("mm3-news", "1.2.0")
    old = make_wheel("mm3-news", "0.9.0")
    return index_bytes(
        [
            entry(
                "mm3-news",
                [
                    release("mm3-news", "1.2.0", data),
                    release("mm3-news", "0.9.0", old, api=">=0.5,<1"),
                ],
            ),
            entry("mm3-clockwork", [release("mm3-clockwork", "2.0.0", b"x", api=">=2,<3")]),
        ],
        **kw,
    )


# --- index format and compatibility ---------------------------------------------


def test_core_350_index_parses_and_rejects_bad_fields() -> None:
    index = parse_index(sample_index())
    assert index.serial == 10
    assert index.modules[0].publisher.workflow == "release.yml"
    bad = json.loads(sample_index())
    bad["modules"][0]["releases"][0]["files"][0]["sha256"] = "nothex"
    with pytest.raises(CatalogError, match="sha256"):
        parse_index(json.dumps(bad).encode())
    with pytest.raises(CatalogError):
        parse_index(b'{"schema": 2, "serial": 1}')


def test_core_350_signer_identity_and_urls_are_fixed() -> None:
    assert catalog.INDEX_URL == "https://mm3-project.github.io/module-index/index.json"
    assert catalog.BUNDLE_URL.endswith("index.json.sigstore.json")
    assert catalog.SIGNER_IDENTITY.endswith(
        "mm3-project/module-index/.github/workflows/publish.yml@refs/heads/main"
    )
    assert catalog.SIGNER_ISSUER == "https://token.actions.githubusercontent.com"


def test_core_350_lifetime_at_most_14_days_and_not_expired() -> None:
    catalog.validate_lifetime(parse_index(sample_index()), NOW)
    with pytest.raises(CatalogError, match="14 days"):
        catalog.validate_lifetime(parse_index(sample_index(lifetime=timedelta(days=15))), NOW)
    with pytest.raises(CatalogError, match="expired"):
        catalog.validate_lifetime(parse_index(sample_index()), NOW + timedelta(days=8))


def test_core_124_only_compatible_releases_are_offered() -> None:
    index = parse_index(sample_index())
    news = catalog.find_entry(index, "MM3_News")
    assert news is not None
    newest = catalog.newest_compatible(news, "1.0")
    assert newest is not None
    assert newest.version == "1.2.0"
    clockwork = catalog.find_entry(index, "mm3-clockwork")
    assert clockwork is not None
    assert catalog.newest_compatible(clockwork, "1.0") is None


def test_core_125_search_matches_name_category_and_description() -> None:
    index = parse_index(sample_index())
    assert [e.pypi for e in catalog.search(index, "modulo news")] == ["mm3-news"]
    assert len(catalog.search(index, "", "info")) == 2
    assert catalog.search(index, "nothing-like-this") == []
    news = catalog.find_entry(index, "mm3-news")
    assert news is not None
    assert news.text("it-IT") == "Il modulo mm3-news"


def test_core_351_engine_copy_reports_stale(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="no cached"):
        read_cached(tmp_path)
    root = tmp_path / "catalog"
    root.mkdir()
    (root / "index.json").write_bytes(sample_index())
    meta = catalog.CatalogMeta(10, NOW, NOW + timedelta(days=6))
    (root / "meta.json").write_text(json.dumps(meta.to_json()))
    assert not read_cached(tmp_path, NOW + timedelta(hours=2)).stale
    assert read_cached(tmp_path, NOW + timedelta(days=2)).stale


# --- downloads -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://pypi.org/simple/",
        "https://evil.example.com/x",
        "https://pypi.org:8443/x",
        "https://user:pw@pypi.org/x",
        "file:///etc/passwd",
    ],
)
def test_core_370_refuses_urls_outside_the_hosts(url: str) -> None:
    with pytest.raises(FetchError, match="refused"):
        check_url(url)


def test_core_370_allowed_hosts() -> None:
    for url in (
        catalog.INDEX_URL,
        "https://pypi.org/integrity/x/1/x.whl/provenance",
        "https://files.pythonhosted.org/packages/a/b.whl",
    ):
        check_url(url)


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, length: str | None = None) -> None:
        super().__init__(body)
        self.headers = Message()
        if length is not None:
            self.headers["Content-Length"] = length


class _Opener:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_core_370_size_limits_and_errors(tmp_path: Path) -> None:
    ok = _Opener(_Resp(b"x" * 10))
    assert (
        UrllibFetcher(lambda: ok).get(catalog.INDEX_URL, 10, accept="application/json") == b"x" * 10
    )
    assert ok.requests[0].get_header("Accept") == "application/json"
    big = _Opener(_Resp(b"x" * 11))
    with pytest.raises(FetchError, match="larger"):
        UrllibFetcher(lambda: big).get(catalog.INDEX_URL, 10)
    declared = _Opener(_Resp(b"", length=str(LIMIT_INDEX + 1)))
    with pytest.raises(FetchError, match="larger"):
        UrllibFetcher(lambda: declared).get(catalog.INDEX_URL, LIMIT_INDEX)
    http_error = urllib.error.HTTPError(catalog.INDEX_URL, 404, "no", Message(), None)
    with pytest.raises(FetchError, match="404"):
        UrllibFetcher(lambda: _Opener(http_error)).get(catalog.INDEX_URL, 10)
    dest = tmp_path / "w.whl"
    with pytest.raises(FetchError, match="larger"):
        UrllibFetcher(lambda: _Opener(_Resp(b"y" * 100))).download(
            "https://files.pythonhosted.org/a.whl", dest, 50
        )
    assert not dest.exists()


def test_core_370_redirects_only_to_allowed_hosts() -> None:
    from mm3.installer.fetch import _Redirects

    handler = _Redirects()
    req = urllib.request.Request("https://pypi.org/x")
    with pytest.raises(FetchError, match="refused"):
        handler.redirect_request(
            req, io.BytesIO(), 302, "Found", Message(), "https://evil.example/"
        )  # type: ignore[arg-type]
    moved = handler.redirect_request(
        req,
        io.BytesIO(),
        302,
        "Found",
        Message(),
        "https://files.pythonhosted.org/y",  # type: ignore[arg-type]
    )
    assert moved is not None
    assert moved.full_url == "https://files.pythonhosted.org/y"


# --- verification (real Sigstore, offline trust root) ------------------------------


def test_core_352_real_provenance_verifies(tmp_path: Path) -> None:
    SigstoreVerification(offline=True).verify_wheel(
        REAL_WHEEL, REAL_PROVENANCE.read_bytes(), REAL_PUBLISHER
    )


def test_core_352_wrong_workflow_or_repository_fails() -> None:
    v = SigstoreVerification(offline=True)
    raw = REAL_PROVENANCE.read_bytes()
    with pytest.raises(VerifyError):
        v.verify_wheel(
            REAL_WHEEL, raw, Publisher(repository="pypi/pypi-attestations", workflow="x.yml")
        )
    with pytest.raises(VerifyError, match="another publisher"):
        v.verify_wheel(REAL_WHEEL, raw, Publisher(repository="evil/fork", workflow="release.yml"))


def test_core_352_tampered_wheel_fails(tmp_path: Path) -> None:
    wheel = tmp_path / REAL_WHEEL.name
    shutil.copyfile(REAL_WHEEL, wheel)
    with wheel.open("ab") as fh:
        fh.write(b"\0")
    with pytest.raises(VerifyError):
        SigstoreVerification(offline=True).verify_wheel(
            wheel, REAL_PROVENANCE.read_bytes(), REAL_PUBLISHER
        )


def test_core_352_garbage_provenance_fails() -> None:
    with pytest.raises(VerifyError, match="invalid provenance"):
        SigstoreVerification(offline=True).verify_wheel(REAL_WHEEL, b"{}", REAL_PUBLISHER)


def test_core_350_bad_index_bundles_fail() -> None:
    v = SigstoreVerification(offline=True)
    with pytest.raises(VerifyError, match="not a Sigstore bundle"):
        v.verify_index(b"{}", b"not json")
    # A real bundle for other content (the DSSE attestation of a wheel) never
    # verifies as the index signature.
    att = json.loads(REAL_PROVENANCE.read_bytes())["attestation_bundles"][0]["attestations"][0]
    from pypi_attestations import Attestation

    bundle = Attestation.model_validate(att).to_bundle().to_json()
    with pytest.raises(VerifyError):
        v.verify_index(sample_index(), bundle.encode())


# --- the installer's index store ----------------------------------------------------


def store(
    tmp_path: Path, clock: Clock | None = None
) -> tuple[CatalogStore, FakeFetcher, FakeVerification, Layout]:
    layout = Layout.under(tmp_path)
    fetcher, verification = FakeFetcher(), FakeVerification()
    return (
        CatalogStore(layout, fetcher, verification, now=clock or Clock()),
        fetcher,
        verification,
        layout,
    )


def test_core_351_refresh_keeps_index_and_writes_engine_copy(tmp_path: Path) -> None:
    s, fetcher, _, layout = store(tmp_path)
    assert s.due()
    assert s.meta() is None
    fetcher.serve_index(sample_index(serial=12))
    meta = s.refresh()
    assert meta.serial == 12
    assert not s.due()
    assert (layout.catalog / "index.json").stat().st_mode & 0o777 == 0o600
    cached = read_cached(layout.engine_state, NOW)
    assert cached.meta.serial == 12
    assert not cached.stale
    assert (layout.engine_catalog / "index.json").stat().st_mode & 0o777 == 0o644
    assert s.verified().serial == 12


def test_core_351_bad_signature_keeps_the_old_index(tmp_path: Path) -> None:
    s, fetcher, verification, _ = store(tmp_path)
    fetcher.serve_index(sample_index(serial=5))
    s.refresh()
    fetcher.serve_index(sample_index(serial=6))
    verification.index_ok = False
    with pytest.raises(CatalogError, match="signature"):
        s.refresh()
    verification.index_ok = True
    assert s.verified().serial == 5


def test_core_351_lower_serial_is_refused(tmp_path: Path) -> None:
    s, fetcher, _, _ = store(tmp_path)
    fetcher.serve_index(sample_index(serial=9))
    s.refresh()
    fetcher.serve_index(sample_index(serial=8))
    with pytest.raises(CatalogError, match="lower than 9"):
        s.refresh()
    fetcher.serve_index(sample_index(serial=9))
    assert s.refresh().serial == 9  # the same serial again is accepted


def test_core_351_rolled_back_copy_is_refused_for_installs(tmp_path: Path) -> None:
    s, fetcher, _, layout = store(tmp_path)
    fetcher.serve_index(sample_index(serial=3))
    s.refresh()
    old = (layout.catalog / "index.json").read_bytes()
    fetcher.serve_index(sample_index(serial=4))
    s.refresh()
    (layout.catalog / "index.json").write_bytes(old)  # someone puts serial 3 back
    with pytest.raises(CatalogError, match="older"):
        s.verified()


def test_core_351_expired_index_is_refused(tmp_path: Path) -> None:
    clock = Clock()
    s, fetcher, _, _ = store(tmp_path, clock)
    fetcher.serve_index(sample_index())
    s.refresh()
    clock.now = NOW + timedelta(days=8)
    with pytest.raises(CatalogError, match="expired"):
        s.verified()
    with pytest.raises(CatalogError, match="expired"):
        s.refresh()


def test_core_351_verified_checks_the_signature_again(tmp_path: Path) -> None:
    s, fetcher, verification, _ = store(tmp_path)
    with pytest.raises(CatalogError, match="no verified catalog"):
        s.verified()
    fetcher.serve_index(sample_index())
    s.refresh()
    verification.index_ok = False
    with pytest.raises(CatalogError, match="signature"):
        s.verified()


def test_core_370_unreachable_index_is_a_catalog_error(tmp_path: Path) -> None:
    s, _, _, _ = store(tmp_path)
    with pytest.raises(CatalogError, match="404"):
        s.refresh()


# --- re-authentication ----------------------------------------------------------------


def test_core_369_passphrase_is_stored_as_argon2_hash(tmp_path: Path) -> None:
    auth = Auth(Layout.under(tmp_path))
    (tmp_path / "state").mkdir()
    with pytest.raises(ValueError, match="12"):
        auth.set_passphrase("short")
    auth.set_passphrase("correct horse battery")
    stored = json.loads((tmp_path / "state" / "auth.json").read_text())
    assert stored["passphrase"].startswith("$argon2")
    assert (tmp_path / "state" / "auth.json").stat().st_mode & 0o777 == 0o600
    auth.check("correct horse battery", None)


def test_core_132_no_passphrase_means_every_operation_is_refused(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    with pytest.raises(AuthError, match="sudo /opt/mm3/installer/bin/mm3 installer passphrase"):
        Auth(Layout.under(tmp_path)).check("anything", None)


def test_core_132_five_failures_lock_for_15_minutes(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    clock = Clock()
    auth = Auth(Layout.under(tmp_path), now=clock)
    auth.set_passphrase("correct horse battery")
    for _ in range(4):
        with pytest.raises(AuthError):
            auth.check("wrong", None)
    with pytest.raises(LockedError):
        auth.check("wrong", None)
    # Locked even for the right passphrase, and across a restart.
    with pytest.raises(LockedError):
        Auth(Layout.under(tmp_path), now=clock).check("correct horse battery", None)
    clock.now = NOW + timedelta(minutes=16)
    auth.check("correct horse battery", None)
    assert auth.locked_until() is None


def test_core_369_totp_needs_a_code_to_enable_and_then_for_every_check(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    clock = Clock()
    auth = Auth(Layout.under(tmp_path), now=clock)
    auth.set_passphrase("correct horse battery")
    setup = auth.begin_totp()
    totp = pyotp.TOTP(setup.secret)
    assert setup.uri.startswith("otpauth://totp/")
    assert not auth.totp_enabled
    right = totp.at(clock.now)
    with pytest.raises(AuthError, match="wrong"):
        auth.confirm_totp("000000" if right != "000000" else "111111")
    auth.confirm_totp(right)
    assert auth.totp_enabled
    clock.now = NOW + timedelta(seconds=30)
    with pytest.raises(AuthError):
        auth.check("correct horse battery", None)
    auth.check("correct horse battery", totp.at(clock.now))
    auth.disable_totp()
    auth.check("correct horse battery", None)


def test_core_373_engine_copy_never_follows_a_symlink(tmp_path: Path) -> None:
    s, fetcher, _, layout = store(tmp_path)
    secret = tmp_path / "auth.json"
    secret.write_text("secret")
    secret.chmod(0o600)
    layout.engine_state.mkdir(parents=True)
    (layout.engine_state / "catalog").symlink_to(tmp_path)  # the engine plants a link
    fetcher.serve_index(sample_index(serial=12))
    assert s.refresh().serial == 12  # the installer's own copy is kept
    assert secret.read_text() == "secret"
    assert secret.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "index.json").exists()


def test_core_373_atomic_write_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    from mm3.installer.files import atomic_write

    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(OSError, match=r"Too many levels|symbolic|Not a directory"):
        atomic_write(tmp_path / "link" / "f", b"x")
    atomic_write(tmp_path / "real" / "f", b"x", mode=0o644)
    assert (tmp_path / "real" / "f").stat().st_mode & 0o777 == 0o644
    (tmp_path / "real" / "g").symlink_to(tmp_path / "victim")
    atomic_write(tmp_path / "real" / "g", b"y")  # replaces the link, never writes through it
    assert not (tmp_path / "victim").exists()
    assert (tmp_path / "real" / "g").read_bytes() == b"y"


def test_core_369_a_totp_code_is_accepted_once(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    clock = Clock()
    auth = Auth(Layout.under(tmp_path), now=clock)
    auth.set_passphrase("correct horse battery")
    setup = auth.begin_totp()
    totp = pyotp.TOTP(setup.secret)
    auth.confirm_totp(totp.at(clock.now))
    clock.now = NOW + timedelta(seconds=30)
    code = totp.at(clock.now)
    auth.check("correct horse battery", code)
    with pytest.raises(AuthError):
        auth.check("correct horse battery", code)  # replayed
    with pytest.raises(AuthError):
        auth.check("correct horse battery", totp.at(NOW))  # older step
    clock.now = NOW + timedelta(seconds=60)
    auth.check("correct horse battery", totp.at(clock.now))
