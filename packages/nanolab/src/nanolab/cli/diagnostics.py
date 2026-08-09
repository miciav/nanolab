"""Shared host prerequisite diagnostics."""

import shutil


REQUIRED_EXECUTABLES = ("docker", "ssh")


def missing_executables(required: tuple[str, ...] = REQUIRED_EXECUTABLES) -> list[str]:
    return [name for name in required if shutil.which(name) is None]
