# ruff: noqa: RUF001
"""Launcher checks with fake interpreters; never install or run real chat/setup."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import setup_share

ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = ROOT / "scripts/share"
BROKEN_VENV_MESSAGE = (
    "项目里的 .venv 环境不可用（可能是从别的电脑拷贝过来的）。"
    "请删除项目文件夹里的 .venv 文件夹，再重新运行“开始设置”。"
    "聊天数据和设置不在 .venv 里，不会丢失。"
)


@pytest.mark.parametrize("name", ["开始设置", "开始聊天"])
@pytest.mark.parametrize("suffix", [".command", ".bat"])
def test_launcher_format(name, suffix):
    path = LAUNCHERS / f"{name}{suffix}"
    raw = path.read_bytes()
    if suffix == ".command":
        assert path.stat().st_mode & 0o111
        assert b"\r" not in raw
        assert raw.startswith(b"#!/bin/bash\n")
    else:
        assert raw.startswith(b"@echo off\r\nchcp 65001 >nul\r\n")
        assert b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize("platform,suffix", [("darwin", ".command"), ("win32", ".bat")])
def test_chat_directories_match_setup(platform, suffix, monkeypatch, tmp_path):
    monkeypatch.setattr(setup_share.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    paths = setup_share.share_paths()
    source = (LAUNCHERS / f"开始聊天{suffix}").read_text(encoding="utf-8")
    if platform == "darwin":
        assert 'share_root="$HOME/Library/Application Support/kiki-share"' in source
        prefix, separator = r"\$share_root", "/"
    else:
        assert 'set "SHARE_ROOT=%LOCALAPPDATA%\\kiki-share"' in source
        prefix, separator = "%SHARE_ROOT%", "\\"
    assert paths.root.name == "kiki-share"
    for variable, directory in (
        ("PRIVATE_DATA", paths.private),
        ("RUNTIME_DATA", paths.runtime),
        ("TEST_RESULTS", paths.test_results),
    ):
        match = re.search(
            rf'COMPANION_{variable}_DIR="?{prefix}{re.escape(separator)}([^"\n]+)', source,
        )
        assert match is not None
        assert match.group(1) == directory.name


@pytest.mark.parametrize("suffix", [".command", ".bat"])
def test_launcher_commands(suffix):
    setup = (LAUNCHERS / f"开始设置{suffix}").read_text(encoding="utf-8")
    chat = (LAUNCHERS / f"开始聊天{suffix}").read_text(encoding="utf-8")
    assert "scripts/setup_share.py" in setup.replace("\\", "/")
    assert '-m pip install -q -e ".[share]"' in setup
    assert "-m companion.runtime.text_chat --project-root" in chat
    assert "pip install" not in chat
    assert "没有找到 Python 3.12，请先按说明安装" in setup
    for source in (setup, chat):
        assert "sys.version_info[:2] == (3, 12)" in source
        if suffix == ".command":
            assert '"$script_dir/../.."' in source
            assert 'cd -- "$repo_root"' in source
            assert 'read -r -p "按回车关闭窗口"' in source
            candidates = [
                "python3.12",
                "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
                "/opt/homebrew/bin/python3.12",
            ]
            assert [setup.index(item) for item in candidates] == sorted(
                setup.index(item) for item in candidates
            )
        else:
            assert "%~dp0..\\.." in source
            assert 'cd /d "%REPO_ROOT%"' in source
            assert "pause" in source
            assert setup.index("py -3.12") < setup.index("python -c")
    if suffix == ".command":
        assert setup.index('"$repo_root/.venv/bin/python" -c') < setup.index("python3.12")
        assert "python3.12" not in chat
    else:
        check = '"%REPO_ROOT%\\.venv\\Scripts\\python.exe" -c'
        assert setup.index(check) < setup.index("py -3.12")
        assert "if not errorlevel 1 goto install" in setup
        assert setup.index(":install") > setup.index("-m venv")
        assert check in chat
        assert "py -3.12" not in chat
        assert "\npython -c" not in chat
        assert chat.index(check) < chat.index('set "COMPANION_')
        assert "if errorlevel 1 (\n    echo 请先运行“开始设置”\n    goto finish\n)" in chat
    assert ('"$@"' if suffix == ".command" else "%*") in setup
    assert "安装依赖失败，请检查网络后重试" in setup
    assert "开始设置" in chat


@pytest.mark.parametrize("name", ["开始设置", "开始聊天"])
@pytest.mark.parametrize("venv_state", ["valid", "wrong-version", "missing"])
def test_mac_launcher_without_system_python(name, venv_state, tmp_path):
    repo = tmp_path / "project with spaces"
    share = repo / "scripts/share"
    share.mkdir(parents=True)
    source = (LAUNCHERS / f"{name}.command").read_text(encoding="utf-8")
    # Redirect absolute system candidates too, regardless of the host installation.
    for candidate in (
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
        "/opt/homebrew/bin/python3.12",
    ):
        source = source.replace(candidate, str(tmp_path / "missing-python"))
    launcher = share / f"{name}.command"
    launcher.write_text(source, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "dirname").symlink_to("/usr/bin/dirname")
    if venv_state != "missing":
        python = repo / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(
            '#!/bin/bash\nif [[ "$1" == "-c" ]]; then\n'
            f'    exit {0 if venv_state == "valid" else 1}\nfi\n'
            'printf "CALL:%s\\n" "$*"\n'
            'printf "PRIVATE:%s\\nRUNTIME:%s\\nRESULTS:%s\\n" '
            '"$COMPANION_PRIVATE_DATA_DIR" "$COMPANION_RUNTIME_DATA_DIR" '
            '"$COMPANION_TEST_RESULTS_DIR"\n',
            encoding="utf-8",
        )
        python.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(launcher)], input="\n", capture_output=True, text=True,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path / "home")}, check=False,
    )
    if venv_state == "valid":
        assert result.returncode == 0, result.stderr
        assert "没有找到 Python 3.12" not in result.stdout
        if name == "开始设置":
            assert 'CALL:-m pip install -q -e .[share]' in result.stdout
            assert f"CALL:{repo}/scripts/setup_share.py" in result.stdout
        else:
            assert f"CALL:-m companion.runtime.text_chat --project-root {repo}" in result.stdout
            root = tmp_path / "home/Library/Application Support/kiki-share"
            for label, directory in (("PRIVATE", "private"), ("RUNTIME", "runtime"),
                                     ("RESULTS", "test-results")):
                assert f"{label}:{root}/{directory}" in result.stdout
    else:
        assert result.returncode != 0
        if name == "开始聊天":
            message = "请先运行“开始设置”"
        elif venv_state == "missing":
            message = "没有找到 Python 3.12"
        else:
            message = BROKEN_VENV_MESSAGE
        assert message in result.stdout
        assert "CALL:" not in result.stdout


def test_mac_setup_existing_invalid_venv_reports_recovery(tmp_path):
    repo = tmp_path / "project"
    share = repo / "scripts/share"
    share.mkdir(parents=True)
    launcher = share / "开始设置.command"
    launcher.write_text((LAUNCHERS / "开始设置.command").read_text(encoding="utf-8"),
                        encoding="utf-8")
    calls = tmp_path / "calls.log"
    fake = '#!/bin/bash\nprintf "%s:%s\\n" "$0" "$*" >> "' + str(calls) + '"\n'
    venv_python = repo / ".venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(fake + 'if [[ "$1" == "-c" ]]; then exit 1; fi\n', encoding="utf-8")
    venv_python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "dirname").symlink_to("/usr/bin/dirname")
    system_python = bin_dir / "python3.12"
    system_python.write_text(fake, encoding="utf-8")
    system_python.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(launcher)], input="\n", capture_output=True, text=True,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path / "home")}, check=False,
    )
    assert result.returncode != 0
    assert BROKEN_VENV_MESSAGE in result.stdout
    assert "没有找到 Python 3.12" not in result.stdout
    logged = calls.read_text(encoding="utf-8").splitlines()
    assert len(logged) == 1
    assert logged[0].startswith(f"{venv_python}:-c ")
    assert venv_python.exists()


def test_windows_setup_existing_invalid_venv_branch():
    setup = (LAUNCHERS / "开始设置.bat").read_text(encoding="utf-8")
    branch = (
        'if exist "%REPO_ROOT%\\.venv\\" (\n'
        f"    echo {BROKEN_VENV_MESSAGE}\n"
        "    goto finish\n)"
    )
    assert branch in setup
    check = '"%REPO_ROOT%\\.venv\\Scripts\\python.exe" -c'
    assert setup.index(check) < setup.index(branch) < setup.index("py -3.12")
    assert "rmdir" not in setup.lower()
    assert "del " not in setup.lower()


def test_setup_document():
    text = (ROOT / "docs/share-text-setup.md").read_text(encoding="utf-8")
    for required in (
        '不要勾选“加密本地备份”', "Windows 版本未在真实 Windows 电脑上验证",
        "人名", "地名", "/exit", "--rebuild",
        "网络共享路径", "@kiki-companion", ".venv",
    ):
        assert required in text
    assert "/Users/" not in text
    assert "C:\\Users\\" not in text


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac bash syntax check")
@pytest.mark.parametrize("name", ["开始设置", "开始聊天"])
def test_mac_shell_syntax(name):
    result = subprocess.run(
        ["bash", "-n", str(LAUNCHERS / f"{name}.command")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
