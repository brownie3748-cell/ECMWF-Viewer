"""Load the single TOML configuration file."""

from __future__ import annotations

import pathlib
import tomllib

CONFIG_PATH = pathlib.Path(__file__).with_name("config.toml")


def load(path: str | pathlib.Path | None = None) -> dict:
    with open(path or CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)
