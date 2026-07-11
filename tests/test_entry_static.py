"""入口路径静态检查：确认目标流程已接线。"""

from __future__ import annotations

import re
from pathlib import Path

import register


ROOT = Path(__file__).resolve().parents[1]


def test_entry_imports_and_main_exists():
    assert callable(register.main)
    assert callable(register.run_batch)


def test_shipped_sequence_present_in_source():
    entry = (ROOT / "register.py").read_text(encoding="utf-8")
    browser_src = (ROOT / "browser.py").read_text(encoding="utf-8")
    mail_src = (ROOT / "mail.py").read_text(encoding="utf-8")
    config_src = (ROOT / "config.toml").read_text(encoding="utf-8")
    config_example_src = (ROOT / "config.example.toml").read_text(encoding="utf-8")

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
    assert "sso_to_cpa_file" in entry or "cpa_output" in entry
    assert "resolve_cpa_output_dir" in entry
    assert "signup_on_page" in entry or "signup_on_page" in browser_src
    assert "browser_session" in entry or "browser_session" in browser_src
    assert "run_worker_batch" in entry
    assert "poll_for_confirmation_code" in entry
    assert "warm_profile" in entry or "--warm-profile" in entry
    assert "run_batch" in entry
    assert "--total" in entry or "total" in entry
    assert "ThreadPoolExecutor" in entry
    assert "launch_persistent_context" in browser_src
    assert "safe_goto" in browser_src
    assert "ERR_ABORTED" in browser_src
    assert "sign_out_session" in browser_src
    assert "sign-out" in browser_src or "sign_out_url" in browser_src
    assert "capture_sso_from_grok" in browser_src
    assert "extract_sso_cookie" in browser_src
    assert "grok.com" in browser_src
    assert "_is_grok_host" in browser_src
    assert "_is_accounts_account_page" in browser_src
    assert "registration_success_signal" in browser_src
    assert "ensure_logged_out_for_signup" in browser_src
    # 不得再主动 goto grok 采集 SSO
    capture_body = browser_src.split("def capture_sso_from_grok")[1].split(
        "def _should_drop_auth_cookie"
    )[0]
    assert "safe_goto" not in capture_body
    assert "sign_out=False" in entry or "sign_out=False" in browser_src
    assert "SSO 已保存" in entry or "已拿到 SSO" in entry or "已拿到 SSO" in browser_src
    assert "复用" in entry or "browser_session" in entry
    assert "user_data_dir" in config_src
    assert "[timing]" in config_src
    assert "[run]" in config_src
    assert "total = 5" in config_src
    assert "between_rounds_ms" in config_src
    assert "goto_retries" in config_src
    assert "after_sso_capture_ms" in config_src
    assert "grok_home_url" in config_src
    # 示例 / 本地均可选 csv 或 cpa
    assert re.search(r'type\s*=\s*"(csv|cpa)"', config_example_src)
    assert "path =" in config_src
    assert "path =" in config_example_src
    assert re.search(r'type\s*=\s*"(csv|cpa)"', config_src)

    helpers = (ROOT / "helpers.py").read_text(encoding="utf-8")
    assert '"SSO"' in helpers or "'SSO'" in helpers
    assert "SSO" in helpers

    cpa_src = (ROOT / "cpa_output.py").read_text(encoding="utf-8")
    assert "grok-" in cpa_src
    assert "sso_to_token" in cpa_src
    assert "token_to_cliproxy_entry" in cpa_src

    # 密钥/域名应在配置文件中，而非写死为代码唯一来源
    assert 'domain = "fltv.asia"' in config_src
    assert "codaily@duckmail.sbs" in config_src
    assert "7758521" in config_src

    # 运营密钥/域名来自 config.toml，不出现在工具模块中
    config_py = (ROOT / "config.py").read_text(encoding="utf-8")
    assert "fltv.asia" not in helpers
    assert "7758521" not in helpers
    assert "codaily@duckmail.sbs" not in helpers
    assert "7758521" not in mail_src
    assert "codaily@duckmail.sbs" not in mail_src
    assert "fltv.asia" not in config_py
