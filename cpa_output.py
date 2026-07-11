"""CPA（cliproxyapi 扁平 xai oauth）产物：SSO → Device Flow → grok-{email}.json。"""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from sso2cpa import sso_to_token, token_to_cliproxy_entry

# 并发写同一目录时互斥（原子写本身按文件，锁主要保护目录创建与序号分配）
_cpa_lock = threading.Lock()

_SAFE_EMAIL_RE = re.compile(r"[^\w.@+-]+", re.UNICODE)


def ensure_cpa_dependencies() -> None:
    """
    cpa 模式启动前检查依赖。缺 curl_cffi 时立刻失败，避免白跑注册。
    """
    try:
        from curl_cffi import requests as _requests  # noqa: F401
    except ImportError as exc:
        py = sys.executable or "python3"
        raise RuntimeError(
            "cpa 输出需要 curl_cffi，当前环境未安装。\n"
            f"  请执行: {py} -m pip install 'curl_cffi>=0.6.0'\n"
            "  或:     pip install -r requirements.txt"
        ) from exc


def sanitize_email_for_filename(email: str) -> str:
    """邮箱用作文件名：去掉路径危险字符，保留常见邮箱字符。"""
    email = (email or "").strip()
    if not email:
        return "unknown"
    # 去掉路径分隔与空白等
    cleaned = email.replace("/", "_").replace("\\", "_").replace("\0", "")
    cleaned = _SAFE_EMAIL_RE.sub("_", cleaned)
    return cleaned or "unknown"


def resolve_cpa_output_dir(configured_path: str = "") -> Path:
    """
    解析 CPA 输出目录。

    - 配置了 path：使用该目录（不存在则创建）
    - 未配置：当前工作目录下 `{yyyyMMdd-HHmm}-{序号}`，序号从 1 起跳过已存在目录
    """
    configured = (configured_path or "").strip()
    if configured:
        out = Path(configured).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        return out

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    root = Path.cwd()
    seq = 1
    with _cpa_lock:
        while True:
            candidate = root / f"{stamp}-{seq}"
            try:
                candidate.mkdir(parents=False, exist_ok=False)
                return candidate.resolve()
            except FileExistsError:
                seq += 1
            if seq > 10_000:
                raise RuntimeError(f"无法分配 CPA 输出目录：{root}/{stamp}-*")


def cpa_filename(email: str) -> str:
    """CPA 落盘文件名：grok-{email}.json。"""
    return f"grok-{sanitize_email_for_filename(email)}.json"


def write_cpa_json(out_dir: Path, token: dict, email: str = "") -> Path:
    """
    写出扁平 type=xai JSON（与 cliproxyapi 结构一致），文件名 grok-{email}.json。
    compact JSON、权限 600。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _, entry = token_to_cliproxy_entry(token, email=email)
    resolved_email = (entry.get("email") or email or "").strip()
    path = out_dir / cpa_filename(resolved_email)

    import json

    payload = json.dumps(entry, separators=(",", ":"), ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _cpa_lock:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def sso_to_cpa_file(
    sso_cookie: str,
    email: str,
    out_dir: Path | str,
    *,
    max_retries: int = 8,
) -> Path:
    """
    SSO cookie → Device Flow 凭证 → 写入 out_dir/grok-{email}.json。
    失败抛 RuntimeError。
    """
    token = sso_to_token(sso_cookie, max_retries=max_retries)
    if not token:
        raise RuntimeError("SSO → OAuth token 失败（Device Flow 未拿到 access_token）")
    return write_cpa_json(Path(out_dir), token, email=email)
