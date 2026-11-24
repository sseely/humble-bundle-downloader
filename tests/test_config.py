"""Tests for humble_dl.config."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from humble_dl.config import (
    ENV_CONCURRENCY,
    ENV_PROFILE_DIR,
    ConfigError,
    Settings,
    load_settings,
)

HOME = Path("/h")


def test_defaults_with_empty_env() -> None:
    s = load_settings({}, HOME)
    assert s.output_dir == Path("/h/HumbleBundle")
    assert s.profile_dir == Path("/h/.humble-dl/profile")
    assert s.debug_dir == Path("/h/.humble-dl/debug")
    assert s.hash_cache_path == Path("/h/.humble-dl/hash-cache.json")
    assert s.books_state_path == Path("/h/.humble-dl/books-imported.json")
    assert s.titles_path == Path("/h/.humble-dl/titles.json")
    assert s.concurrency == 4
    assert s.api_rps == 2.0
    assert s.http_timeout_s == 30.0
    assert s.api_timeout_s == 10.0
    assert s.login_timeout_s == 600.0


def test_output_override() -> None:
    s = load_settings({}, HOME, output_override=Path("/out/x"))
    assert s.output_dir == Path("/out/x")


def test_profile_dir_env_override() -> None:
    s = load_settings({ENV_PROFILE_DIR: "/custom/profile"}, HOME)
    assert s.profile_dir == Path("/custom/profile")
    assert s.debug_dir == Path("/h/.humble-dl/debug")


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("8", 8), ("32", 32), (" 5 ", 5)])
def test_concurrency_valid(raw: str, expected: int) -> None:
    assert load_settings({ENV_CONCURRENCY: raw}, HOME).concurrency == expected


@pytest.mark.parametrize("raw", ["0", "-1", "33", "abc", "", "2.5"])
def test_concurrency_invalid(raw: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({ENV_CONCURRENCY: raw}, HOME)
    message = str(exc.value)
    assert ENV_CONCURRENCY in message
    assert "1..32" in message
    assert repr(raw) in message


def test_settings_frozen() -> None:
    s = load_settings({}, HOME)
    with pytest.raises(FrozenInstanceError):
        s.concurrency = 9  # pyright: ignore[reportAttributeAccessIssue]


def test_settings_is_dataclass_instance() -> None:
    assert isinstance(load_settings({}, HOME), Settings)
