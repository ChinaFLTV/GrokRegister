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
    # 输出产物：csv（默认）| cpa
    output_type: str
    # csv 模式：CSV 文件路径
    csv_path: str
    # cpa 模式：输出目录；空则运行时自动分配 yyyyMMdd-HHmm-{序号}
    output_path: str
    # cpa Device Flow 超时 / 重试
    cpa_http_timeout_sec: float
    cpa_poll_timeout_sec: float
    cpa_max_retries: int
    cpa_retry_base_sec: float


OUTPUT_TYPES = frozenset({"csv", "cpa"})


def _cfg_float(*candidates: object, default: float) -> float:
    """从若干候选值中取第一个非 None，再转 float（避免 dict.get 的 Optional 告警）。"""
    for value in candidates:
        if value is None:
            continue
        return float(value)  # type: ignore[arg-type]
    return default


def _cfg_int(*candidates: object, default: int) -> int:
    for value in candidates:
        if value is None:
            continue
        return int(value)  # type: ignore[arg-type]
    return default


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

    output_type = str(output.get("type", "cpa")).strip().lower() or "cpa"
    if output_type not in OUTPUT_TYPES:
        raise ValueError(
            f"[output].type 无效：{output_type!r}，允许值：{', '.join(sorted(OUTPUT_TYPES))}"
        )

    # csv 兼容：历史配置只有 csv_path；缺省 accounts.csv
    csv_path = str(output.get("csv_path", "accounts.csv") or "accounts.csv")
    # cpa 输出目录；可空（运行时再解析默认目录）
    output_path = str(output.get("path", "") or "").strip()

    return Config(
        email_domain=str(email["domain"]),
        local_part_length=_cfg_int(email.get("local_part_length"), default=8),
        duckmail_address=str(duck["address"]),
        duckmail_password=str(duck["password"]),
        duckmail_base_url=str(duck["base_url"]).rstrip("?&"),
        token_endpoint=str(duck["token_endpoint"]),
        messages_endpoint=str(duck["messages_endpoint"]),
        from_address=str(duck["from_address"]),
        subject_marker=str(duck["subject_marker"]),
        poll_interval_sec=_cfg_float(
            timing.get("poll_interval_sec"),
            duck.get("poll_interval_sec"),
            default=2.0,
        ),
        poll_timeout_sec=_cfg_float(
            timing.get("poll_timeout_sec"),
            duck.get("poll_timeout_sec"),
            default=120.0,
        ),
        signup_url=str(signup["url"]),
        grok_home_url=str(signup.get("grok_home_url") or "https://grok.com"),
        sign_out_url=str(signup.get("sign_out_url") or "https://grok.com/sign-out"),
        sign_out_enabled=bool(signup.get("sign_out_enabled", True)),
        clear_auth_cookies=bool(signup.get("clear_auth_cookies", True)),
        headless=bool(signup.get("headless", False)),
        timeout_ms=_cfg_int(
            timing.get("timeout_ms"),
            signup.get("timeout_ms"),
            default=45000,
        ),
        browser_channel=str(signup.get("browser_channel") or "chrome"),
        user_data_dir=str(signup.get("user_data_dir") or DEFAULT_USER_DATA_DIR),
        after_email_submit_ms=_cfg_int(timing.get("after_email_submit_ms"), default=500),
        after_otp_filled_ms=_cfg_int(timing.get("after_otp_filled_ms"), default=150),
        after_otp_submit_ms=_cfg_int(timing.get("after_otp_submit_ms"), default=400),
        after_complete_ms=_cfg_int(timing.get("after_complete_ms"), default=500),
        after_sso_capture_ms=_cfg_int(timing.get("after_sso_capture_ms"), default=800),
        after_sign_out_ms=_cfg_int(timing.get("after_sign_out_ms"), default=300),
        between_rounds_ms=_cfg_int(timing.get("between_rounds_ms"), default=800),
        otp_key_delay_ms=_cfg_int(timing.get("otp_key_delay_ms"), default=30),
        click_timeout_ms=_cfg_int(timing.get("click_timeout_ms"), default=5000),
        fill_timeout_ms=_cfg_int(timing.get("fill_timeout_ms"), default=12000),
        sign_out_timeout_ms=_cfg_int(timing.get("sign_out_timeout_ms"), default=15000),
        goto_retries=_cfg_int(timing.get("goto_retries"), default=3),
        total=_cfg_int(run.get("total"), default=DEFAULT_TOTAL),
        workers=_cfg_int(run.get("workers"), default=DEFAULT_WORKERS),
        output_type=output_type,
        csv_path=csv_path,
        output_path=output_path,
        cpa_http_timeout_sec=_cfg_float(
            timing.get("cpa_http_timeout_sec"),
            output.get("cpa_http_timeout_sec"),
            default=15.0,
        ),
        cpa_poll_timeout_sec=_cfg_float(
            timing.get("cpa_poll_timeout_sec"),
            output.get("cpa_poll_timeout_sec"),
            default=60.0,
        ),
        cpa_max_retries=_cfg_int(
            timing.get("cpa_max_retries"),
            output.get("cpa_max_retries"),
            default=8,
        ),
        cpa_retry_base_sec=_cfg_float(
            timing.get("cpa_retry_base_sec"),
            output.get("cpa_retry_base_sec"),
            default=15.0,
        ),
    )


def with_worker_profile(cfg: Config, worker_id: int, workers: int) -> Config:
    """并发时为每个 worker 分配独立 Profile，避免 Chrome 用户目录锁冲突。"""
    if workers <= 1:
        return cfg
    base = (cfg.user_data_dir or DEFAULT_USER_DATA_DIR).strip() or DEFAULT_USER_DATA_DIR
    root = Path(base).expanduser()
    return replace(cfg, user_data_dir=str(root / f"w{worker_id}"))
