"""Loads ``ProfilerConfig`` from a local TOML file."""

from __future__ import annotations

import dataclasses
import os
import tomllib
from typing import Any

from datamining_skill.application.config import ProfilerConfig
from datamining_skill.domain.exceptions import InvalidConfigurationException


def load_config(path: str | os.PathLike[str]) -> ProfilerConfig:
    """Read the ``[profiler]`` table into a validated config.

    Unknown keys and wrongly typed values are rejected, so a typo cannot silently fall back
    to a default.
    """
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise InvalidConfigurationException(f"cannot read configuration file: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise InvalidConfigurationException("configuration file is not valid UTF-8") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InvalidConfigurationException(f"invalid TOML: {exc}") from exc

    section = document.get("profiler", {})
    if not isinstance(section, dict):
        raise InvalidConfigurationException("'profiler' must be a table")

    fields = {field.name: field for field in dataclasses.fields(ProfilerConfig)}
    unknown = sorted(set(section) - set(fields))
    if unknown:
        raise InvalidConfigurationException(f"unknown configuration keys: {', '.join(unknown)}")

    settings: dict[str, Any] = {}
    for key, raw_value in section.items():
        value_type = float if key == "min_format_confidence" else int
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise InvalidConfigurationException(f"'{key}' must be a number")
        if value_type is int and not isinstance(raw_value, int):
            raise InvalidConfigurationException(f"'{key}' must be an integer")
        settings[key] = value_type(raw_value)
    return ProfilerConfig(**settings)
