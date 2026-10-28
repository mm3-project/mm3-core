"""Config Manager: load YAML, merge defaults, validate engine and module schemas.

``load_config`` reads the main file, its includes and the env file, and
validates the result against the engine model and every installed
module's ``config_model``. Watching the files and computing diffs for hot
reload are added with the hot reload work of milestone 2.

Implements:
    CORE-02: Core spec 0.8, §3 Engine components.
"""

from mm3.core.config.envfile import DEFAULT_ENV_FILE
from mm3.core.config.records import ValidationRecord
from mm3.core.config.validate import DEFAULT_CONFIG, ConfigResult, ResolvedInstance, load_config

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_ENV_FILE",
    "ConfigResult",
    "ResolvedInstance",
    "ValidationRecord",
    "load_config",
]
