from pathlib import Path

import pytest

OFFLINE_ENV_KEYS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY")


@pytest.fixture
def offline_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in OFFLINE_ENV_KEYS:
        monkeypatch.setenv(name, "0")


@pytest.fixture
def soak_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home
