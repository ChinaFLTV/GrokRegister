"""纯工具函数单元测试（走已交付代码路径）。"""

from __future__ import annotations

import csv
import re
import string
from pathlib import Path

import pytest

from config import load_config
from helpers import (
    CSV_COLUMNS,
    append_account_csv,
    build_email,
    extract_confirmation_code,
    generate_local_part,
    normalize_otp_chars,
)


def test_local_part_length_and_charset():
    for _ in range(20):
        part = generate_local_part(8)
        assert len(part) == 8
        assert re.fullmatch(r"[A-Za-z0-9]{8}", part)


def test_build_email_uses_configured_domain(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[email]
domain = "example.test"
local_part_length = 8

[duckmail]
address = "a@example.test"
password = "p"
base_url = "https://example.test/api/mail"
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
csv_path = "accounts.csv"
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    email = build_email(cfg.email_domain, cfg.local_part_length)
    local, domain = email.split("@", 1)
    assert domain == "example.test"
    assert len(local) == 8
    assert set(local) <= set(string.ascii_letters + string.digits)


def test_extract_confirmation_code():
    assert extract_confirmation_code("A45-WU6 xAI confirmation code") == "A45-WU6"
    assert extract_confirmation_code("Z9K-1AB xAI confirmation code") == "Z9K-1AB"
    with pytest.raises(ValueError):
        extract_confirmation_code("Hello world")


def test_normalize_otp_chars():
    assert normalize_otp_chars("X9G-M86") == "X9GM86"
    assert normalize_otp_chars("A45-WU6") == "A45WU6"
    assert normalize_otp_chars("  ab-cd  ") == "abcd"
    with pytest.raises(ValueError):
        normalize_otp_chars("---")


def test_csv_append_columns_and_no_clobber(tmp_path: Path):
    path = tmp_path / "accounts.csv"
    append_account_csv(path, "a@x.test", "pw1", "Li", "Ming")
    append_account_csv(path, "b@x.test", "pw2", "Wang", "Hua")

    with path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert rows[0] == list(CSV_COLUMNS)
    assert rows[1] == ["a@x.test", "pw1", "Li", "Ming"]
    assert rows[2] == ["b@x.test", "pw2", "Wang", "Hua"]
    assert len(rows) == 3
