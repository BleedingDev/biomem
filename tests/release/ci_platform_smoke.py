"""Cheap cross-platform smoke checks replacing the useful AppVeyor signals."""

from __future__ import annotations

import tempfile
from pathlib import Path

import torch

from memory_module import (
    BDBMContainer,
    SecurityManager,
    SessionCache,
    SettingsManager,
    load_bdbm,
    save_bdbm,
)
from memory_module.cli import create_parser


def main() -> None:
    assert torch.version.cuda is None, torch.__version__
    assert not torch.cuda.is_available(), torch.__version__

    parsed = create_parser().parse_args(["store", "smoke-key", "smoke-value"])
    assert parsed.command == "store"
    assert parsed.key == "smoke-key"
    assert parsed.value == "smoke-value"

    cache = SessionCache(ttl_seconds=60)
    cache.store("smoke-session", "smoke-query")
    assert cache.retrieve("smoke-session") == "smoke-query"

    with tempfile.TemporaryDirectory() as temporary:
        data_dir = Path(temporary)
        settings = SettingsManager(data_dir / "settings")
        assert settings.get_max_associations() >= 3

        security = SecurityManager(data_dir=data_dir / "security")
        assert security.check_command_allowed("retrieve") is None
        assert security.is_allowed_origin("chrome-extension://ci-smoke")
        assert not security.is_allowed_origin("https://chatgpt.com")

        state = {"version": "0.0.2", "stats": {"writes": 0}}
        container = BDBMContainer()
        container_path = container.save(state, str(data_dir / "container.bdbm"))
        assert container.load(container_path) == state

        wrapper_path = save_bdbm(state, str(data_dir / "wrapper.bdbm"))
        assert load_bdbm(wrapper_path) == state

    print("CI PLATFORM SMOKE OK")


if __name__ == "__main__":
    main()
