"""从外部 config.toml 加载运行时配置。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_USER_DATA_DIR = "chrome-profile"
DEFAULT_TOTAL = 5
DEFAULT_WORKERS = 1


@dataclass(frozen=True)
class Config:
    email_domain: str
    local_part_length: int
    duckmail_address: str
    duckmail_password: str
    duckmail_base_url: str
    token_endpoint: str
    messages_endpoint: str
    from_address: str
    subject_marker: str
    poll_interval_sec: float
    poll_timeout_sec: float
    signup_url: str
    # 注册成功后打开以采集 SSO Cookie 的页面
    grok_home_url: str
    # 每轮注册成功后登出（GET），避免复用浏览器时下一轮已登录
    sign_out_url: str
    sign_out_enabled: bool
    # 登出后是否清理 SSO/会话 Cookie（保留 cf_clearance 等风控 cookie）
    clear_auth_cookies: bool
    headless: bool
    timeout_ms: int
    browser_channel: str
    user_data_dir: str
    # 页面短等待（毫秒）
    after_email_submit_ms: int
    after_otp_filled_ms: int
    after_otp_submit_ms: int
    after_complete_ms: int
    after_sso_capture_ms: int
    after_sign_out_ms: int
    between_rounds_ms: int
    otp_key_delay_ms: int
    click_timeout_ms: int
    fill_timeout_ms: int
    # 登出导航专用超时（毫秒）；勿用过长的 timeout_ms，避免卡 45s
    sign_out_timeout_ms: int
    # 导航重试（复用浏览器时 goto 可能被上一跳转打断）
    goto_retries: int
    # 批量 / 并发
    total: int
    workers: int
    csv_path: str


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"未找到配置文件：{config_path.resolve()}")

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    email = raw["email"]
    duck = raw["duckmail"]
    signup = raw["signup"]
    output = raw["output"]
    run = raw.get("run") or {}
    timing = raw.get("timing") or {}

    return Config(
        email_domain=str(email["domain"]),
        local_part_length=int(email.get("local_part_length", 8)),
        duckmail_address=str(duck["address"]),
        duckmail_password=str(duck["password"]),
        duckmail_base_url=str(duck["base_url"]).rstrip("?&"),
        token_endpoint=str(duck["token_endpoint"]),
        messages_endpoint=str(duck["messages_endpoint"]),
        from_address=str(duck["from_address"]),
        subject_marker=str(duck["subject_marker"]),
        poll_interval_sec=float(
            timing.get("poll_interval_sec", duck.get("poll_interval_sec", 2))
        ),
        poll_timeout_sec=float(
            timing.get("poll_timeout_sec", duck.get("poll_timeout_sec", 120))
        ),
        signup_url=str(signup["url"]),
        grok_home_url=str(signup.get("grok_home_url", "https://grok.com")),
        sign_out_url=str(
            signup.get("sign_out_url", "https://grok.com/sign-out")
        ),
        sign_out_enabled=bool(signup.get("sign_out_enabled", True)),
        clear_auth_cookies=bool(signup.get("clear_auth_cookies", True)),
        headless=bool(signup.get("headless", False)),
        timeout_ms=int(timing.get("timeout_ms", signup.get("timeout_ms", 45000))),
        browser_channel=str(signup.get("browser_channel", "chrome")),
        user_data_dir=str(signup.get("user_data_dir", DEFAULT_USER_DATA_DIR)),
        after_email_submit_ms=int(timing.get("after_email_submit_ms", 500)),
        after_otp_filled_ms=int(timing.get("after_otp_filled_ms", 150)),
        after_otp_submit_ms=int(timing.get("after_otp_submit_ms", 400)),
        after_complete_ms=int(timing.get("after_complete_ms", 500)),
        after_sso_capture_ms=int(timing.get("after_sso_capture_ms", 800)),
        after_sign_out_ms=int(timing.get("after_sign_out_ms", 300)),
        between_rounds_ms=int(timing.get("between_rounds_ms", 800)),
        otp_key_delay_ms=int(timing.get("otp_key_delay_ms", 30)),
        click_timeout_ms=int(timing.get("click_timeout_ms", 5000)),
        fill_timeout_ms=int(timing.get("fill_timeout_ms", 12000)),
        sign_out_timeout_ms=int(timing.get("sign_out_timeout_ms", 15000)),
        goto_retries=int(timing.get("goto_retries", 3)),
        total=int(run.get("total", DEFAULT_TOTAL)),
        workers=int(run.get("workers", DEFAULT_WORKERS)),
        csv_path=str(output["csv_path"]),
    )


def with_worker_profile(cfg: Config, worker_id: int, workers: int) -> Config:
    """并发时为每个 worker 分配独立 Profile，避免 Chrome 用户目录锁冲突。"""
    if workers <= 1:
        return cfg
    base = (cfg.user_data_dir or DEFAULT_USER_DATA_DIR).strip() or DEFAULT_USER_DATA_DIR
    root = Path(base).expanduser()
    return replace(cfg, user_data_dir=str(root / f"w{worker_id}"))
