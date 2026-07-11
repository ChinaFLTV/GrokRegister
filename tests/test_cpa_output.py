"""CPA 输出：目录分配、文件名、JSON 结构（不走真实 Device Flow）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cpa_output import (
    cpa_filename,
    ensure_cpa_dependencies,
    resolve_cpa_output_dir,
    sanitize_email_for_filename,
    sso_to_cpa_file,
    write_cpa_json,
)


def test_sanitize_and_filename():
    assert sanitize_email_for_filename("a@b.com") == "a@b.com"
    assert sanitize_email_for_filename("a/b@x.com") == "a_b@x.com"
    assert cpa_filename("user@example.com") == "grok-user@example.com.json"
    assert cpa_filename("") == "grok-unknown.json"


def test_resolve_cpa_output_dir_configured(tmp_path: Path):
    target = tmp_path / "my-cpa"
    out = resolve_cpa_output_dir(str(target))
    assert out == target.resolve()
    assert out.is_dir()


def test_resolve_cpa_output_dir_auto_seq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    # 固定时间戳：用已存在目录模拟冲突
    stamp = time.strftime("%Y%m%d-%H%M")
    first = tmp_path / f"{stamp}-1"
    first.mkdir()
    out = resolve_cpa_output_dir("")
    assert out.name.startswith(stamp + "-")
    assert out.name != f"{stamp}-1"
    assert out.is_dir()
    # 序号应至少为 2
    seq = int(out.name.rsplit("-", 1)[-1])
    assert seq >= 2


def test_write_cpa_json_structure(tmp_path: Path):
    # 构造最小 token（假 JWT payload：{"sub":"u1","exp":9999999999,"iat":1000}）
    # base64url: eyJzdWIiOiJ1MSIsImV4cCI6OTk5OTk5OTk5OSwiaWF0IjoxMDAwfQ
    fake_payload = (
        "eyJzdWIiOiJ1MSIsImV4cCI6OTk5OTk5OTk5OSwiaWF0IjoxMDAwfQ"
    )
    access = f"aaa.{fake_payload}.sig"
    token = {
        "access_token": access,
        "refresh_token": "rt-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "id_token": "",
        "_email": "acct@example.com",
    }
    path = write_cpa_json(tmp_path, token, email="acct@example.com")
    assert path.name == "grok-acct@example.com.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["type"] == "xai"
    assert data["auth_kind"] == "oauth"
    assert data["access_token"] == access
    assert data["refresh_token"] == "rt-1"
    assert data["email"] == "acct@example.com"
    assert data["sub"] == "u1"
    assert data["base_url"] == "https://cli-chat-proxy.grok.com/v1"
    assert "headers" in data
    assert data["disabled"] is False


def test_sso_to_cpa_file_uses_device_flow(tmp_path: Path):
    fake_payload = (
        "eyJzdWIiOiJ1MSIsImV4cCI6OTk5OTk5OTk5OSwiaWF0IjoxMDAwfQ"
    )
    access = f"aaa.{fake_payload}.sig"
    fake_token = {
        "access_token": access,
        "refresh_token": "rt-x",
        "token_type": "Bearer",
        "expires_in": 7200,
        "_email": "x@y.test",
    }
    with patch("cpa_output.sso_to_token", return_value=fake_token) as mock_sso:
        path = sso_to_cpa_file("sso-jwt-value", "x@y.test", tmp_path)
    mock_sso.assert_called_once()
    assert path.name == "grok-x@y.test.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["access_token"] == access
    assert data["email"] == "x@y.test"


def test_sso_to_cpa_file_raises_on_token_fail(tmp_path: Path):
    with patch("cpa_output.sso_to_token", return_value=None):
        with pytest.raises(RuntimeError, match="Device Flow"):
            sso_to_cpa_file("bad-sso", "a@b.c", tmp_path)


def test_ensure_cpa_dependencies_ok_when_installed():
    # 本项目 venv 已装 curl_cffi 时应直接通过
    ensure_cpa_dependencies()


def test_ensure_cpa_dependencies_raises_when_missing():
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "curl_cffi" or name.startswith("curl_cffi."):
            raise ImportError("mocked missing curl_cffi")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(RuntimeError, match="curl_cffi"):
            ensure_cpa_dependencies()
