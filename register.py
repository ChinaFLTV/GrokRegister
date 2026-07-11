#!/usr/bin/env python3
"""单轮 xAI/Grok 账号注册入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from browser import run_browser_signup, warm_profile
from config import DEFAULT_CONFIG_PATH, load_config
from helpers import (
    append_account_csv,
    build_email,
    random_name,
    random_password,
)
from mail import fetch_token, poll_for_confirmation_code


def run_once(config_path: str | Path = DEFAULT_CONFIG_PATH) -> int:
    cfg = load_config(config_path)
    email = build_email(cfg.email_domain, cfg.local_part_length)
    first_name = random_name()
    last_name = random_name()
    password = random_password()

    print(f"[信息] 已加载配置：{Path(config_path).resolve()}", flush=True)
    print(f"[信息] 邮箱域名={cfg.email_domain} duckmail={cfg.duckmail_address}", flush=True)
    print(f"[信息] 注册邮箱={email}", flush=True)

    print("[信息] 正在获取邮箱认证 token…", flush=True)
    token = fetch_token(cfg)
    print("[信息] 邮箱认证 token 获取成功", flush=True)

    def code_provider() -> str:
        print("[信息] 正在轮询邮箱验证码…", flush=True)
        code = poll_for_confirmation_code(cfg, token)
        print(f"[信息] 已获取验证码：{code}", flush=True)
        return code

    run_browser_signup(cfg, email, code_provider, first_name, last_name, password)
    append_account_csv(cfg.csv_path, email, password, last_name, first_name)
    print(f"[完成] 账号已保存 → {cfg.csv_path}", flush=True)
    print(f"[完成] {email} / {password} / 姓={last_name} / 名={first_name}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="注册一个 Grok/xAI 账号")
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="config.toml 路径（默认：./config.toml）",
    )
    parser.add_argument(
        "--warm-profile",
        action="store_true",
        help="仅打开自动化专用浏览器 Profile，供人手通过 Cloudflare 后保存",
    )
    args = parser.parse_args(argv)
    try:
        if args.warm_profile:
            cfg = load_config(args.config)
            print(f"[信息] 已加载配置：{Path(args.config).resolve()}", flush=True)
            warm_profile(cfg)
            return 0
        return run_once(args.config)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
