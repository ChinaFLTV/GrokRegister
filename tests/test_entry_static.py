"""入口路径静态检查：确认目标流程已接线。"""

from __future__ import annotations

from pathlib import Path

import register


ROOT = Path(__file__).resolve().parents[1]


def test_entry_imports_and_main_exists():
    assert callable(register.main)
    assert callable(register.run_once)


def test_shipped_sequence_present_in_source():
    entry = (ROOT / "register.py").read_text(encoding="utf-8")
    browser_src = (ROOT / "browser.py").read_text(encoding="utf-8")
    mail_src = (ROOT / "mail.py").read_text(encoding="utf-8")
    config_src = (ROOT / "config.toml").read_text(encoding="utf-8")

    assert "accounts.x.ai/sign-up" in browser_src or "signup_url" in browser_src
    assert "open_signup_and_submit_email" in browser_src
    assert "fill_verification_code" in browser_src
    assert "complete_registration" in browser_src
    assert "data-input-otp" in browser_src
    assert "_set_input_otp_value" in browser_src
    assert "press_sequentially" in browser_src

    assert "fetch_token" in mail_src
    assert "fetch_messages" in mail_src
    assert "mark_message_read" in mail_src
    assert "poll_for_confirmation_code" in mail_src
    assert "Bearer" in mail_src

    assert "load_config" in entry
    assert "append_account_csv" in entry
    assert "run_browser_signup" in entry
    assert "poll_for_confirmation_code" in entry
    assert "warm_profile" in entry or "--warm-profile" in entry
    assert "launch_persistent_context" in browser_src
    assert "user_data_dir" in config_src

    # 密钥/域名应在配置文件中，而非写死为代码唯一来源
    assert 'domain = "fltv.asia"' in config_src
    assert "codaily@duckmail.sbs" in config_src
    assert "7758521" in config_src

    # 运营密钥/域名来自 config.toml，不出现在工具模块中
    helpers = (ROOT / "helpers.py").read_text(encoding="utf-8")
    config_py = (ROOT / "config.py").read_text(encoding="utf-8")
    assert "fltv.asia" not in helpers
    assert "7758521" not in helpers
    assert "codaily@duckmail.sbs" not in helpers
    assert "7758521" not in mail_src
    assert "codaily@duckmail.sbs" not in mail_src
    assert "fltv.asia" not in config_py
