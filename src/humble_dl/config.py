"""Application settings, loaded from injected environment and home directory."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_CONCURRENCY = "HUMBLE_DL_CONCURRENCY"
ENV_PROFILE_DIR = "HUMBLE_DL_PROFILE_DIR"

OUTPUT_DIR_NAME = "HumbleBundle"
STATE_DIR_NAME = ".humble-dl"
PROFILE_DIR_NAME = "profile"
DEBUG_DIR_NAME = "debug"
HASH_CACHE_FILE_NAME = "hash-cache.json"
BOOKS_STATE_FILE_NAME = "books-imported.json"

DEFAULT_CONCURRENCY = 4
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 32
DEFAULT_API_RPS = 2.0
DEFAULT_HTTP_TIMEOUT_S = 30.0
DEFAULT_API_TIMEOUT_S = 10.0
DEFAULT_LOGIN_TIMEOUT_S = 600.0


class ConfigError(Exception):
    """Raised when configuration (environment or flags) is invalid."""


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    output_dir: Path
    profile_dir: Path
    debug_dir: Path
    hash_cache_path: Path
    books_state_path: Path
    concurrency: int
    api_rps: float
    http_timeout_s: float
    api_timeout_s: float
    login_timeout_s: float


def _parse_concurrency(env: Mapping[str, str]) -> int:
    raw = env.get(ENV_CONCURRENCY)
    if raw is None:
        return DEFAULT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not MIN_CONCURRENCY <= value <= MAX_CONCURRENCY:
        raise ConfigError(
            f"{ENV_CONCURRENCY}={raw!r} is invalid; "
            f"must be an integer in range {MIN_CONCURRENCY}..{MAX_CONCURRENCY}"
        )
    return value


def load_settings(
    env: Mapping[str, str], home: Path, output_override: Path | None = None
) -> Settings:
    """Build Settings from an environment mapping and home directory.

    Args:
        env: Environment variables (injected; never read from os.environ here).
        home: User home directory (injected).
        output_override: Replaces the default output directory when given.

    Returns:
        The resolved Settings.

    Raises:
        ConfigError: If HUMBLE_DL_CONCURRENCY is not an integer in 1..32.
    """
    state = home / STATE_DIR_NAME
    profile_env = env.get(ENV_PROFILE_DIR)
    return Settings(
        output_dir=output_override if output_override is not None else home / OUTPUT_DIR_NAME,
        profile_dir=Path(profile_env) if profile_env else state / PROFILE_DIR_NAME,
        debug_dir=state / DEBUG_DIR_NAME,
        hash_cache_path=state / HASH_CACHE_FILE_NAME,
        books_state_path=state / BOOKS_STATE_FILE_NAME,
        concurrency=_parse_concurrency(env),
        api_rps=DEFAULT_API_RPS,
        http_timeout_s=DEFAULT_HTTP_TIMEOUT_S,
        api_timeout_s=DEFAULT_API_TIMEOUT_S,
        login_timeout_s=DEFAULT_LOGIN_TIMEOUT_S,
    )
