#!/usr/bin/env python3
"""xAI/Grok 账号注册入口：支持循环总数与并发；同 worker 内复用浏览器。"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from browser import browser_session, signup_on_page, warm_profile
from config import DEFAULT_CONFIG_PATH, load_config, with_worker_profile
from helpers import (
    append_account_csv,
    build_email,
    random_name,
    random_password,
)
from mail import fetch_token, poll_for_confirmation_code

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _register_one_on_page(
    page,
    cfg,
    token: str,
    *,
    index: int,
    total: int,
    worker_id: int,
) -> tuple[bool, str]:
    """在已打开的 page 上注册一个账号（不开关浏览器）。"""
    tag = f"[{index}/{total}#w{worker_id}]"
    email = build_email(cfg.email_domain, cfg.local_part_length)
    first_name = random_name()
    last_name = random_name()
    password = random_password()

    _log(f"{tag} 注册邮箱={email}")

    def code_provider() -> str:
        _log(f"{tag} 正在轮询邮箱验证码（收件人={email}）…")
        code = poll_for_confirmation_code(cfg, token, target_email=email)
        _log(f"{tag} 已获取验证码：{code}")
        return code

    try:
        signup_on_page(page, cfg, email, code_provider, first_name, last_name, password)
        append_account_csv(cfg.csv_path, email, password, last_name, first_name)
        summary = f"{email} / {password} / 姓={last_name} / 名={first_name}"
        _log(f"{tag} [完成] 已保存 → {cfg.csv_path} | {summary}")
        return True, summary
    except Exception as exc:
        _log(f"{tag} [失败] {exc}")
        return False, str(exc)


def run_worker_batch(
    cfg,
    token: str,
    indices: list[int],
    *,
    total: int,
    worker_id: int,
) -> tuple[int, int]:
    """
    单个 worker：打开一次浏览器，循环 indices 中的序号依次注册。
    每轮仅重新打开注册页，不关闭浏览器。
    返回 (成功数, 失败数)。
    """
    if not indices:
        return 0, 0

    ok_count = 0
    fail_count = 0
    _log(
        f"[信息] worker#{worker_id} 启动，本 worker 任务序号={indices}，复用同一浏览器",
    )

    with browser_session(cfg) as page:
        for index in indices:
            ok, _ = _register_one_on_page(
                page, cfg, token, index=index, total=total, worker_id=worker_id
            )
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                # 失败后仍继续下一轮；signup_on_page 会重新 goto 注册页

    _log(f"[信息] worker#{worker_id} 结束，成功={ok_count} 失败={fail_count}")
    return ok_count, fail_count


def _partition_indices(total_n: int, workers_n: int) -> list[list[int]]:
    """把 1..total 轮询分配到各 worker，便于各 worker 内部串行复用浏览器。"""
    buckets: list[list[int]] = [[] for _ in range(workers_n)]
    for i in range(1, total_n + 1):
        buckets[(i - 1) % workers_n].append(i)
    return buckets


def run_batch(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    total: int | None = None,
    workers: int | None = None,
) -> int:
    cfg = load_config(config_path)
    total_n = int(total if total is not None else cfg.total)
    workers_n = int(workers if workers is not None else cfg.workers)
    if total_n < 1:
        raise ValueError("注册总数 total 必须 >= 1")
    if workers_n < 1:
        raise ValueError("并发数 workers 必须 >= 1")
    workers_n = min(workers_n, total_n)

    cfg = replace(cfg, total=total_n, workers=workers_n)

    _log(f"[信息] 已加载配置：{Path(config_path).resolve()}")
    _log(
        f"[信息] 邮箱域名={cfg.email_domain} duckmail={cfg.duckmail_address} "
        f"轮询间隔={cfg.poll_interval_sec}s 超时={cfg.poll_timeout_sec}s"
    )
    _log(
        f"[信息] 计划注册总数={total_n} 并发={workers_n} "
        f"（每 worker 复用同一浏览器） CSV={cfg.csv_path}"
    )

    _log("[信息] 正在获取邮箱认证 token…")
    token = fetch_token(cfg)
    _log("[信息] 邮箱认证 token 获取成功")

    partitions = _partition_indices(total_n, workers_n)
    ok_count = 0
    fail_count = 0

    if workers_n == 1:
        o, f = run_worker_batch(
            with_worker_profile(cfg, 0, 1),
            token,
            partitions[0],
            total=total_n,
            worker_id=0,
        )
        ok_count, fail_count = o, f
    else:
        def task(worker_id: int) -> tuple[int, int]:
            wcfg = with_worker_profile(cfg, worker_id, workers_n)
            return run_worker_batch(
                wcfg,
                token,
                partitions[worker_id],
                total=total_n,
                worker_id=worker_id,
            )

        with ThreadPoolExecutor(max_workers=workers_n) as pool:
            futures = [
                pool.submit(task, wid)
                for wid in range(workers_n)
                if partitions[wid]
            ]
            for fut in as_completed(futures):
                try:
                    o, f = fut.result()
                except Exception as exc:
                    _log(f"[失败] worker 异常：{exc}")
                    o, f = 0, 1
                ok_count += o
                fail_count += f

    _log(f"[汇总] 成功={ok_count} 失败={fail_count} 计划={total_n}")
    if fail_count == 0:
        return 0
    if ok_count == 0:
        return 1
    return 3  # 部分成功


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量注册 Grok/xAI 账号")
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="config.toml 路径（默认：./config.toml）",
    )
    parser.add_argument(
        "-n",
        "--total",
        type=int,
        default=None,
        help="注册账号总数（默认读配置 [run].total，缺省 5）",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="并发数（默认读配置 [run].workers；>1 时每 worker 独立浏览器与 Profile）",
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
        return run_batch(args.config, total=args.total, workers=args.workers)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
