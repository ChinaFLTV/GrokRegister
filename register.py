#!/usr/bin/env python3
"""xAI/Grok 账号注册入口：支持循环总数与并发；同 worker 内复用浏览器。"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from browser import (
    _UI_ERRORS,
    browser_session,
    sign_out_session,
    signup_on_page,
    warm_profile,
)
from config import DEFAULT_CONFIG_PATH, Config, load_config, with_worker_profile
from cpa_output import ensure_cpa_dependencies, resolve_cpa_output_dir, sso_to_cpa_file
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


def _save_account_product(
        cfg,
        *,
        email: str,
        password: str,
        sso: str,
        last_name: str,
        first_name: str,
        tag: str,
) -> str:
    """
    按 [output].type 落盘产物。返回用于日志的「保存目标」描述。
    cpa 失败会抛异常（本轮计失败，不登出由调用方决定——仍应登出以免污染会话）。
    """
    sso_preview = f"{sso[:12]}…{sso[-6:]}" if len(sso) > 22 else sso
    base_summary = (
        f"{email} / {password} / SSO={sso_preview} / "
        f"姓={last_name} / 名={first_name}"
    )

    if cfg.output_type == "cpa":
        out_dir = cfg.output_path
        if not (out_dir or "").strip():
            raise RuntimeError("cpa 模式缺少 output_path（运行时应已解析默认目录）")
        _log(f"{tag} [信息] SSO 已拿到，开始 Device Flow → CPA JSON…")
        cpa_path = sso_to_cpa_file(cfg, sso, email, out_dir)
        _log(f"{tag} [完成] 已保存 CPA → {cpa_path} | {base_summary}")
        return str(cpa_path)

    # 默认 csv
    append_account_csv(
        cfg.csv_path, email, password, sso, last_name, first_name
    )
    _log(f"{tag} [完成] 已保存 → {cfg.csv_path} | {base_summary}")
    return cfg.csv_path


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

    sso_got = False
    try:
        # 先完成注册并拿到 SSO；sign_out=False，等写盘成功后再正式登出
        sso = signup_on_page(
            page,
            cfg,
            email,
            code_provider,
            first_name,
            last_name,
            password,
            sign_out=False,
        )
        if not (sso or "").strip():
            raise RuntimeError("未获取到有效 SSO，本轮不登出、不写盘")
        sso_got = True

        saved = _save_account_product(
            cfg,
            email=email,
            password=password,
            sso=sso,
            last_name=last_name,
            first_name=first_name,
            tag=tag,
        )
        summary = f"{email} → {saved}"

        # 产物已落盘后才正式登出
        _log(f"{tag} [信息] 产物已保存，开始正式登出…")
        sign_out_session(page, cfg)
        page.wait_for_timeout(max(0, cfg.between_rounds_ms))
        return True, summary
    except _UI_ERRORS as exc:
        _log(f"{tag} [失败] {exc}")
        # 已拿到 SSO 但写盘/CPA 失败：仍登出，避免污染下一轮
        if sso_got:
            try:
                _log(f"{tag} [信息] 写盘失败后尝试登出以清理会话…")
                sign_out_session(page, cfg)
            except _UI_ERRORS as logout_exc:
                _log(f"{tag} [警告] 失败后登出异常：{logout_exc}")
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


def _resolve_run_count(cli_value: int | None, cfg_value: int) -> int:
    """CLI 覆盖优先，否则用配置。"""
    if cli_value is not None:
        return cli_value
    return cfg_value


def run_batch(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    total: int | None = None,
    workers: int | None = None,
) -> int:
    cfg: Config = load_config(config_path)
    total_n = _resolve_run_count(total, cfg.total)
    workers_n = _resolve_run_count(workers, cfg.workers)

    if total_n < 1:
        raise ValueError("注册总数 total 必须 >= 1")
    if workers_n < 1:
        raise ValueError("并发数 workers 必须 >= 1")
    workers_n = min(workers_n, total_n)

    cfg = replace(cfg, total=total_n, workers=workers_n)

    # cpa：先校验依赖，再解析本批统一输出目录（配置 path 或自动 yyyyMMdd-HHmm-{序号}）
    if cfg.output_type == "cpa":
        ensure_cpa_dependencies()
        cpa_dir = resolve_cpa_output_dir(cfg.output_path)
        cfg = replace(cfg, output_path=str(cpa_dir))

    _log(f"[信息] 已加载配置：{Path(config_path).resolve()}")
    _log(
        f"[信息] 邮箱域名={cfg.email_domain} duckmail={cfg.duckmail_address} "
        f"轮询间隔={cfg.poll_interval_sec}s 超时={cfg.poll_timeout_sec}s"
    )
    if cfg.output_type == "cpa":
        out_desc = f"type=cpa dir={cfg.output_path} 文件=grok-{{email}}.json"
    else:
        out_desc = f"type=csv file={cfg.csv_path}"
    _log(
        f"[信息] 计划注册总数={total_n} 并发={workers_n} "
        f"（每 worker 复用同一浏览器） 输出={out_desc}"
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
                except _UI_ERRORS as exc:
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
    except _UI_ERRORS as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
