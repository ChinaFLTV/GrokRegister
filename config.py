"""从外部 config.toml 加载运行时配置。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_USER_DATA_DIR = "chrome-profile"


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
    headless: bool
    timeout_ms: int
    # 浏览器通道：chrome / msedge / chromium（空字符串表示默认 Chromium）
    browser_channel: str
    # 自动化专用用户目录；非空则使用持久化 Profile（复用 Cloudflare cookie）
    user_data_dir: str
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
        poll_interval_sec=float(duck["poll_interval_sec"]),
        poll_timeout_sec=float(duck["poll_timeout_sec"]),
        signup_url=str(signup["url"]),
        headless=bool(signup.get("headless", False)),
        timeout_ms=int(signup.get("timeout_ms", 60000)),
        browser_channel=str(signup.get("browser_channel", "chrome")),
        user_data_dir=str(signup.get("user_data_dir", DEFAULT_USER_DATA_DIR)),
        csv_path=str(output["csv_path"]),
    )
