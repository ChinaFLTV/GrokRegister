"""Duckmail 筛选 / 轮询契约单元测试（基于样例 JSON）。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from config import Config, load_config
from helpers import extract_confirmation_code
from mail import (
    fetch_token,
    pick_latest_confirmation,
    poll_for_confirmation_code,
)


def _cfg(**overrides: Any) -> Config:
    base = Config(
        email_domain="fltv.asia",
        local_part_length=8,
        duckmail_address="codaily@duckmail.sbs",
        duckmail_password="7758521",
        duckmail_base_url="https://www.duckmail.sbs/api/mail",
        token_endpoint="/token",
        messages_endpoint="/messages",
        from_address="noreply@x.ai",
        subject_marker="xAI confirmation code",
        poll_interval_sec=5,
        poll_timeout_sec=120,
        signup_url="https://accounts.x.ai/sign-up",
        headless=True,
        timeout_ms=1000,
        browser_channel="chrome",
        user_data_dir="chrome-profile",
        csv_path="accounts.csv",
    )
    return replace(base, **overrides) if overrides else base


FIXTURE_MESSAGES = [
    {
        "id": "20260710T190215-8190",
        "from": {"name": "xAI", "address": "noreply@x.ai"},
        "subject": "A45-WU6 xAI confirmation code",
        "seen": False,
        "createdAt": "2026-07-10T19:02:15.040264967Z",
    },
    {
        "id": "20260710T185115-7690",
        "from": {"name": "Cloudflare", "address": "noreply@notify.cloudflare.com"},
        "subject": "[Cloudflare]: Verify Email Routing address",
        "seen": True,
        "createdAt": "2026-07-10T18:51:15.665753474Z",
    },
    {
        "id": "20260710T180000-1000",
        "from": {"name": "xAI", "address": "noreply@x.ai"},
        "subject": "OLD1-ABC xAI confirmation code",
        "seen": False,
        "createdAt": "2026-07-10T18:00:00.000000000Z",
    },
]


def test_pick_latest_unseen_xai_confirmation():
    cfg = _cfg()
    picked = pick_latest_confirmation(FIXTURE_MESSAGES, cfg)
    assert picked is not None
    assert picked["id"] == "20260710T190215-8190"
    assert extract_confirmation_code(picked["subject"]) == "A45-WU6"


def test_pick_skips_seen_and_non_xai():
    cfg = _cfg()
    msgs = [
        {
            "id": "1",
            "from": {"address": "noreply@x.ai"},
            "subject": "AA xAI confirmation code",
            "seen": True,
            "createdAt": "2026-07-10T20:00:00Z",
        },
        {
            "id": "2",
            "from": {"address": "other@example.com"},
            "subject": "BB xAI confirmation code",
            "seen": False,
            "createdAt": "2026-07-10T21:00:00Z",
        },
    ]
    assert pick_latest_confirmation(msgs, cfg) is None


def test_token_parse_shape():
    # drive request_json path with a stub transport via monkeypatch on requests
    sample = {
        "id": "08f23b1f9967134149358a707b20e56a",
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.example",
    }
    # unit-level: parse contract of fetch_token via injected request
    import mail as mail_mod

    calls: list[tuple] = []

    def fake_request_json(method, url, *, token=None, json_body=None, timeout=30.0):
        calls.append((method, url, token, json_body))
        return sample

    original = mail_mod.request_json
    mail_mod.request_json = fake_request_json
    try:
        token = fetch_token(_cfg())
    finally:
        mail_mod.request_json = original

    assert token == sample["token"]
    assert calls[0][0] == "POST"
    assert "endpoint=%2Ftoken" in calls[0][1]
    assert calls[0][3] == {
        "address": "codaily@duckmail.sbs",
        "password": "7758521",
    }


def test_poll_finds_code_and_marks_read():
    cfg = _cfg(poll_interval_sec=0.01, poll_timeout_sec=1.0)
    sleeps: list[float] = []
    marked: list[str] = []
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(dt):
        sleeps.append(dt)
        clock["t"] += dt

    def fetch(_cfg, _token):
        return FIXTURE_MESSAGES

    def mark(_cfg, _token, msg_id):
        marked.append(msg_id)
        return {"seen": True}

    code = poll_for_confirmation_code(
        cfg,
        "tok",
        sleep_fn=sleep,
        now_fn=now,
        fetch_fn=fetch,
        mark_fn=mark,
    )
    assert code == "A45-WU6"
    assert marked == ["20260710T190215-8190"]


def test_poll_timeout_without_full_sleep():
    cfg = _cfg(poll_interval_sec=0.05, poll_timeout_sec=0.12)
    clock = {"t": 0.0}
    fetches = {"n": 0}

    def now():
        return clock["t"]

    def sleep(dt):
        clock["t"] += dt

    def fetch(_cfg, _token):
        fetches["n"] += 1
        return []  # 永不匹配

    with pytest.raises(TimeoutError):
        poll_for_confirmation_code(
            cfg,
            "tok",
            sleep_fn=sleep,
            now_fn=now,
            fetch_fn=fetch,
            mark_fn=lambda *a, **k: None,
        )
    assert fetches["n"] >= 2
    assert clock["t"] >= cfg.poll_timeout_sec


def test_load_config_reads_external_file(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(
        """
[email]
domain = "fltv.asia"
local_part_length = 8
[duckmail]
address = "codaily@duckmail.sbs"
password = "secret-pass"
base_url = "https://www.duckmail.sbs/api/mail"
token_endpoint = "/token"
messages_endpoint = "/messages"
from_address = "noreply@x.ai"
subject_marker = "xAI confirmation code"
poll_interval_sec = 5
poll_timeout_sec = 120
[signup]
url = "https://accounts.x.ai/sign-up"
headless = true
timeout_ms = 1000
[output]
csv_path = "out.csv"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.email_domain == "fltv.asia"
    assert cfg.duckmail_password == "secret-pass"
    assert cfg.poll_interval_sec == 5
    assert cfg.poll_timeout_sec == 120
