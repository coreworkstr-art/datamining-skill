"""Configuration validation and TOML loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from datamining_skill import InvalidConfigurationException, ProfilerConfig
from datamining_skill.infrastructure import load_config
from tests.conftest import WriteFile

DEFAULT_TOML = Path(__file__).resolve().parent.parent / "configs" / "profiler.default.toml"


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_size_bytes": 0},
        {"head_sample_bytes": -1},
        {"max_line_bytes": 10},
        {"estimation_windows": 1},
        {"min_format_confidence": 0.0},
        {"min_format_confidence": 1.5},
        {"window_bytes": 1_000_000, "exact_count_max_bytes": 1_000},
    ],
)
def test_invalid_settings_are_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(InvalidConfigurationException):
        ProfilerConfig(**overrides)  # type: ignore[arg-type]


def test_shipped_default_file_matches_builtin_defaults() -> None:
    assert load_config(DEFAULT_TOML) == ProfilerConfig()


def test_unknown_keys_are_rejected(write_file: WriteFile) -> None:
    path = write_file("bad.toml", "[profiler]\nwindow_byts = 10\n")
    with pytest.raises(InvalidConfigurationException, match="window_byts"):
        load_config(path)


def test_wrong_types_are_rejected(write_file: WriteFile) -> None:
    path = write_file("bad.toml", '[profiler]\nwindow_bytes = "big"\n')
    with pytest.raises(InvalidConfigurationException):
        load_config(path)


def test_partial_files_fall_back_to_defaults(write_file: WriteFile) -> None:
    path = write_file("partial.toml", "[profiler]\nestimation_windows = 4\n")
    assert load_config(path) == ProfilerConfig(estimation_windows=4)
