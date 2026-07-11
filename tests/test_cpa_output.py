"""CPA 输出：目录、文件名、JSON 结构、编排入口（不走真实 Device Flow）。"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from config import Config
from cpa_output import (
    build_cpa_entry,
    cpa_filename,
    ensure_cpa_dependencies,
    resolve_cpa_output_dir,
    sanitize_email_for_filename,
    sso_to_cpa_file,
    write_cpa_account_file,
)


def _cfg(**overrides: Any) -> Config:
    base = Config(
        email_domain="example.test",
        local_part_length=8,
        duckmail_address="a@example.test",
        duckmail_password="p",
        duckmail_base_url="https://example.test/api/mail",
        token_endpoint="/token",
        messages_endpoint="/messages",
        from_address="noreply@x.ai",
        subject_marker="xAI confirmation code",
        poll_interval_sec=2,
        poll_timeout_sec=120,
        signup_url="https://accounts.x.ai/sign-up",
        grok_home_url="https://grok.com",
        sign_out_url="https://grok.com/sign-out",
        sign_out_enabled=True,
        clear_auth_cookies=True,
        headless=True,
        timeout_ms=1000,
        browser_channel="chrome",
        user_data_dir="chrome-profile",
        after_email_submit_ms=500,
        after_otp_filled_ms=150,
        after_otp_submit_ms=400,
        after_complete_ms=500,
        after_sso_capture_ms=500,
        after_sign_out_ms=300,
        between_rounds_ms=800,
        otp_key_delay_ms=30,
        click_timeout_ms=5000,
        fill_timeout_ms=12000,
        sign_out_timeout_ms=15000,
        goto_retries=3,
        total=5,
        workers=1,
        output_type="cpa",
        csv_path="accounts.csv",
        output_path="",
        cpa_http_timeout_sec=15,
        cpa_poll_timeout_sec=60,
        cpa_max_retries=3,
        cpa_retry_base_sec=1,
    )
    return replace(base, **overrides) if overrides else base


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
    stamp = time.strftime("%Y%m%d-%H%M")
    first = tmp_path / f"{stamp}-1"
    first.mkdir()
    out = resolve_cpa_output_dir("")
    assert out.name.startswith(stamp + "-")
    assert out.name != f"{stamp}-1"
    assert out.is_dir()
    seq = int(out.name.rsplit("-", 1)[-1])
    assert seq >= 2


def test_build_and_write_cpa_entry(tmp_path: Path):
    fake_payload = "eyJzdWIiOiJ1MSIsImV4cCI6OTk5OTk5OTk5OSwiaWF0IjoxMDAwfQ"
    access = f"aaa.{fake_payload}.sig"
    token = {
        "access_token": access,
        "refresh_token": "rt-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "id_token": "",
        "email": "acct@example.com",
    }
    entry = build_cpa_entry(token, email="acct@example.com")
    assert entry["type"] == "xai"
    assert entry["auth_kind"] == "oauth"
    assert entry["sub"] == "u1"
    assert entry["base_url"] == "https://cli-chat-proxy.grok.com/v1"

    path = write_cpa_account_file(tmp_path, token, email="acct@example.com")
    assert path.name == "grok-acct@example.com.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["access_token"] == access
    assert data["refresh_token"] == "rt-1"
    assert data["email"] == "acct@example.com"
    assert data["disabled"] is False
    assert "headers" in data


def test_sso_to_cpa_file_uses_exchange(tmp_path: Path):
    fake_payload = "eyJzdWIiOiJ1MSIsImV4cCI6OTk5OTk5OTk5OSwiaWF0IjoxMDAwfQ"
    access = f"aaa.{fake_payload}.sig"
    fake_token = {
        "access_token": access,
        "refresh_token": "rt-x",
        "token_type": "Bearer",
        "expires_in": 7200,
        "email": "x@y.test",
    }
    cfg = _cfg()
    with patch(
        "cpa_output.exchange_sso_for_oauth_token", return_value=fake_token
    ) as mock_ex:
        path = sso_to_cpa_file(cfg, "sso-jwt-value", "x@y.test", tmp_path)
    mock_ex.assert_called_once()
    assert mock_ex.call_args.args[0] is cfg
    assert mock_ex.call_args.args[1] == "sso-jwt-value"
    assert path.name == "grok-x@y.test.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["access_token"] == access
    assert data["email"] == "x@y.test"


def test_sso_to_cpa_file_raises_on_exchange_fail(tmp_path: Path):
    cfg = _cfg()
    with patch(
        "cpa_output.exchange_sso_for_oauth_token",
        side_effect=RuntimeError("SSO → OAuth token 失败"),
    ):
        with pytest.raises(RuntimeError, match="SSO → OAuth token 失败"):
            sso_to_cpa_file(cfg, "bad-sso", "a@b.c", tmp_path)


def test_ensure_cpa_dependencies_ok_when_installed():
    ensure_cpa_dependencies()


def test_ensure_cpa_dependencies_raises_when_missing():
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "curl_cffi" or name.startswith("curl_cffi."):
            raise ImportError("mocked missing curl_cffi")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(RuntimeError, match="curl-cffi"):
            ensure_cpa_dependencies()
