"""Tests of install/mm3-install.sh.

Implements:
- CORE-293 (Core spec 0.20, §10.2 Packaging and deployment): the installer
  script is idempotent, refuses unsupported systems with exit code 1, fails with
  exit code 2 otherwise, never overwrites the configuration, keeps the previous
  venv and writes nothing it was not asked to.
- CORE-143 (Core spec 0.20, §6.5 Kiosk): the kiosk needs cage 0.1.5 or later.
- CORE-348 (Core spec 0.34, §9.2 Presence and power: module boundary): ddcutil,
  cec-utils and the groups i2c and dialout on kiosk systems.

The full run on clean Debian 12 and 13 images is the CI job ``install``
(install/ci-check.sh); here the functions are sourced and run on temporary paths.
"""

from __future__ import annotations

import getpass
import grp
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install" / "mm3-install.sh"
CI_CHECK = SCRIPT.with_name("ci-check.sh")
BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, **(env or {})}
    return subprocess.run(  # noqa: S603 - fixed arguments
        [BASH, str(SCRIPT), *args], env=full, capture_output=True, text=True, check=False
    )


def call(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Source the script (main does not run) and execute ``snippet``."""
    full = {**os.environ, **env}
    return subprocess.run(  # noqa: S603 - fixed arguments
        [BASH, "-c", f'source "{SCRIPT}"\n{snippet}'],
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )


def os_release(tmp_path: Path, ident: str, version: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(f'ID={ident}\nVERSION_ID="{version}"\n')
    return path


def own_env(tmp_path: Path) -> dict[str, str]:
    return {
        "MM3_USER": getpass.getuser(),
        "MM3_GROUP": grp.getgrgid(os.getgid()).gr_name,
        "MM3_ETC": str(tmp_path / "etc"),
        "MM3_OPT": str(tmp_path / "opt"),
        "MM3_TIMEZONE_FILE": str(tmp_path / "timezone"),
    }


def test_core_293_scripts_are_valid_bash_and_executable() -> None:
    for script in (SCRIPT, CI_CHECK):
        assert script.stat().st_mode & stat.S_IXUSR
        result = subprocess.run(  # noqa: S603 - fixed arguments
            [BASH, "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_core_293_scripts_pass_shellcheck() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(  # noqa: S603 - fixed arguments
        [shellcheck, str(SCRIPT), str(CI_CHECK)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout


def test_core_293_help_exits_zero() -> None:
    result = run_script("--help")
    assert result.returncode == 0
    assert "--find-links" in result.stdout
    assert "--no-kiosk" in result.stdout


def test_core_293_unknown_option_exits_two() -> None:
    result = run_script("--bogus")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


def test_core_293_missing_find_links_directory_exits_two(tmp_path: Path) -> None:
    result = run_script("--find-links", str(tmp_path / "missing"))
    assert result.returncode == 2


@pytest.mark.parametrize(
    ("ident", "version", "arch"),
    [
        ("ubuntu", "24.04", "x86_64"),
        ("debian", "11", "x86_64"),
        ("debian", "14", "x86_64"),
        ("debian", "13", "aarch64"),
    ],
)
def test_core_293_unsupported_system_exits_one(
    tmp_path: Path, ident: str, version: str, arch: str
) -> None:
    result = run_script(
        env={"MM3_OS_RELEASE": str(os_release(tmp_path, ident, version)), "MM3_ARCH": arch}
    )
    assert result.returncode == 1
    assert "unsupported" in result.stderr


def test_core_293_missing_os_release_exits_one(tmp_path: Path) -> None:
    result = run_script(env={"MM3_OS_RELEASE": str(tmp_path / "none"), "MM3_ARCH": "x86_64"})
    assert result.returncode == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="needs a non-root user")
def test_core_293_requires_root(tmp_path: Path) -> None:
    result = run_script(
        env={"MM3_OS_RELEASE": str(os_release(tmp_path, "debian", "13")), "MM3_ARCH": "x86_64"}
    )
    assert result.returncode == 2
    assert "root" in result.stderr


def test_core_293_write_config_creates_files_once(tmp_path: Path) -> None:
    env = own_env(tmp_path)
    first = call("write_config", env)
    assert first.returncode == 0, first.stderr
    etc = tmp_path / "etc"
    assert stat.S_IMODE(etc.stat().st_mode) == 0o750
    for name in ("config.yaml", "mm3.env"):
        assert stat.S_IMODE((etc / name).stat().st_mode) == 0o640
    assert (etc / "mm3.env").read_text() == ""
    assert "module: clock" in (etc / "config.yaml").read_text()

    (etc / "config.yaml").write_text("mine: true\n")
    (etc / "mm3.env").write_text("KEY=1\n")
    second = call("write_config", env)
    assert second.returncode == 0, second.stderr
    assert (etc / "config.yaml").read_text() == "mine: true\n"
    assert (etc / "mm3.env").read_text() == "KEY=1\n"
    assert "keeping" in second.stdout


def test_core_293_default_config_has_no_token_and_a_valid_timezone(tmp_path: Path) -> None:
    env = own_env(tmp_path)
    (tmp_path / "timezone").write_text("Europe/Rome\n")
    result = call("default_config", env)
    assert result.returncode == 0, result.stderr
    assert "token" not in result.stdout.lower()
    if Path("/usr/share/zoneinfo/Europe/Rome").exists():
        assert "timezone: Europe/Rome" in result.stdout


def test_core_293_unknown_timezone_falls_back_to_utc(tmp_path: Path) -> None:
    env = own_env(tmp_path)
    (tmp_path / "timezone").write_text("Nowhere/Land\n")
    result = call("detect_timezone", env)
    assert result.stdout == "UTC"


def fake_cage(tmp_path: Path, output: str, status: int = 0) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    cage = bin_dir / "cage"
    cage.write_text(f"#!/bin/sh\necho '{output}'\nexit {status}\n")
    cage.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


@pytest.mark.parametrize(
    ("output", "status", "ok"),
    [
        ("cage version 0.1.5", 0, True),
        ("cage version 0.2.0", 0, True),
        ("cage version 0.1.10", 0, True),
        ("cage version 0.1.4", 0, False),
        ("cage version 0.1.2", 0, False),
        ("no version here", 0, False),
        ("cage version 0.2.0", 1, False),
    ],
)
def test_core_143_cage_version_gate(tmp_path: Path, output: str, status: int, ok: bool) -> None:
    result = call("cage_version_ok", fake_cage(tmp_path, output, status))
    assert (result.returncode == 0) is ok


def test_core_143_missing_cage_drops_the_kiosk(tmp_path: Path) -> None:
    env = {"PATH": str(tmp_path / "empty"), "MM3_SKIP_PACKAGES": "no"}
    (tmp_path / "empty").mkdir()
    result = call(
        'verify_cage; echo "kiosk=$WANT_KIOSK"',
        {**env, "PATH": f"{tmp_path / 'empty'}:/usr/bin:/bin"},
    )
    if shutil.which("cage"):
        pytest.skip("a real cage is installed")
    assert "kiosk=no" in result.stdout
    assert "CORE-143" in result.stderr


def test_core_293_activate_venv_switches_and_keeps_the_previous(tmp_path: Path) -> None:
    env = own_env(tmp_path)
    venvs = tmp_path / "opt" / "venvs"
    old, new = venvs / "1", venvs / "2"
    old.mkdir(parents=True)
    new.mkdir()
    (tmp_path / "opt" / "venv").symlink_to(old)
    result = call(f'STAGED_VENV="{new}"; activate_venv; echo "prev=$PREVIOUS_VENV"', env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "opt" / "venv").resolve() == new
    assert old.is_dir()
    assert f"prev={old}" in result.stdout


def test_core_293_activate_venv_moves_a_real_directory_aside(tmp_path: Path) -> None:
    env = own_env(tmp_path)
    opt = tmp_path / "opt"
    (opt / "venv").mkdir(parents=True)
    (opt / "venv" / "marker").write_text("x")
    new = opt / "venvs" / "2"
    new.mkdir(parents=True)
    result = call(f'STAGED_VENV="{new}"; activate_venv', env)
    assert result.returncode == 0, result.stderr
    assert (opt / "venv").is_symlink()
    assert "rollback" in result.stderr
    moved = list((opt / "venvs").glob("previous-*"))
    assert len(moved) == 1
    assert (moved[0] / "marker").read_text() == "x"


def test_core_293_cleanup_removes_only_a_never_activated_venv(tmp_path: Path) -> None:
    env = own_env(tmp_path)
    staged = tmp_path / "staged"
    kept = tmp_path / "kept"
    staged.mkdir()
    kept.mkdir()
    call(f'STAGED_VENV="{staged}"; cleanup', env)
    call(f'STAGED_VENV="{kept}"; ACTIVATED=yes; cleanup', env)
    assert not staged.exists()
    assert kept.exists()


def fake_group_tools(tmp_path: Path, *, existing: str, usermod_status: int = 0) -> dict[str, str]:
    """Put fake ``getent``, ``id`` and ``usermod`` first on the PATH; ``usermod`` logs its calls."""
    bin_dir = tmp_path / "gbin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "usermod.log"
    tools = {
        "getent": (
            f'#!/bin/sh\ncase " {existing} " in *" $2 "*) echo "$2:x:1:";; *) exit 2;; esac\n'
        ),
        "id": "#!/bin/sh\necho nogroup\n",
        "usermod": f'#!/bin/sh\necho "$@" >> "{log}"\nexit {usermod_status}\n',
    }
    for name, body in tools.items():
        tool = bin_dir / name
        tool.write_text(body)
        tool.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "USERMOD_LOG": str(log)}


def test_core_348_kiosk_installs_ddcutil_and_cec_utils(tmp_path: Path) -> None:
    result = call("WANT_KIOSK=yes; install_packages", {"MM3_SKIP_PACKAGES": "yes"})
    assert result.returncode == 0
    packages = (result.stdout + result.stderr).split("(")[1].split(")")[0].split()
    assert {"ddcutil", "cec-utils", "wlr-randr"} <= set(packages)


def test_core_348_engine_only_installs_no_display_control_packages(tmp_path: Path) -> None:
    result = call("WANT_KIOSK=no; install_packages", {"MM3_SKIP_PACKAGES": "yes"})
    assert "ddcutil" not in result.stdout + result.stderr
    assert "cec-utils" not in result.stdout + result.stderr


def test_core_348_mm3_joins_the_groups_that_exist(tmp_path: Path) -> None:
    env = fake_group_tools(tmp_path, existing="i2c")
    result = call("WANT_KIOSK=yes; ensure_display_control_groups", env)
    assert result.returncode == 0
    assert (tmp_path / "usermod.log").read_text().splitlines() == ["--append --groups i2c mm3"]


def test_core_348_both_groups_are_added_when_present(tmp_path: Path) -> None:
    env = fake_group_tools(tmp_path, existing="i2c dialout")
    result = call("WANT_KIOSK=yes; ensure_display_control_groups", env)
    assert result.returncode == 0
    lines = (tmp_path / "usermod.log").read_text().splitlines()
    assert lines == ["--append --groups i2c mm3", "--append --groups dialout mm3"]


def test_core_348_no_groups_on_an_engine_only_system(tmp_path: Path) -> None:
    env = fake_group_tools(tmp_path, existing="i2c dialout")
    result = call("WANT_KIOSK=no; ensure_display_control_groups", env)
    assert result.returncode == 0
    assert not (tmp_path / "usermod.log").exists()


def test_core_348_a_failed_group_change_is_logged_not_fatal(tmp_path: Path) -> None:
    env = fake_group_tools(tmp_path, existing="i2c", usermod_status=1)
    result = call("WANT_KIOSK=yes; ensure_display_control_groups; echo done", env)
    assert result.returncode == 0
    assert "done" in result.stdout
    assert "could not add mm3 to the group i2c" in result.stderr
