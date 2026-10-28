"""The env file read by ``!env`` (default ``/etc/mm3/mm3.env``).

The file holds ``KEY=VALUE`` lines, ``#`` comment lines and blank lines.
A value may be wrapped in single or double quotes, which are removed; there
is no interpolation and no escape processing. Values never appear in
records or logs.

Implements:
    CORE-200: Core spec 0.8, §6.2 Rules.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from mm3.core.config.records import ValidationRecord, make_record

__all__ = ["DEFAULT_ENV_FILE", "EnvFile", "read_env_file"]

DEFAULT_ENV_FILE = Path("/etc/mm3/mm3.env")
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


@dataclass
class EnvFile:
    """Variables read from an env file.

    Attributes:
        path: The file.
        values: ``KEY`` to value; empty when the file does not exist.
        exists: Whether the file was found.
        records: Problems found (syntax errors, permission warning).
    """

    path: Path
    values: dict[str, str] = field(default_factory=dict)
    exists: bool = False
    records: list[ValidationRecord] = field(default_factory=list)


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def read_env_file(path: Path) -> EnvFile:
    """Read an env file.

    A missing file is not an error: ``!env`` then uses the process
    environment only.

    Args:
        path: The file.

    Returns:
        Its variables and the records found.

    Implements:
        CORE-200: Core spec 0.8, §6.2 Rules.
    """
    env = EnvFile(path)
    try:
        st = path.stat()
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return env
    except OSError:
        env.records.append(
            make_record("CORE_CFG_FILE_UNREADABLE", "", {"file": str(path)}, file=str(path))
        )
        return env
    env.exists = True
    if st.st_mode & stat.S_IROTH:
        env.records.append(
            make_record(
                "CORE_CFG_ENV_FILE_MODE",
                "",
                {"mode": oct(stat.S_IMODE(st.st_mode))},
                file=str(path),
                line=1,
                col=1,
            )
        )
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            env.records.append(
                make_record("CORE_CFG_ENV_FILE_SYNTAX", "", {}, file=str(path), line=n, col=1)
            )
            continue
        env.values[m.group(1)] = _unquote(m.group(2))
    return env
