"""SSO → OIDC Device Flow → CPA（cliproxyapi 扁平 xai oauth）JSON 落盘。

分层方式对齐 mail.py：
  底层 HTTP 助手 → 单接口函数 → 编排（换票 / 组装 / 写盘）。
公共 OIDC 端点用 requests；需携带 sso Cookie 的步骤用 curl_cffi 模拟 Chrome。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlencode

import requests

from config import Config

# ---------------------------------------------------------------------------
# 常量（Grok CLI 公开 OIDC client）
# ---------------------------------------------------------------------------

OIDC_ISSUER = "https://auth.x.ai"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)
ACCOUNTS_HOME = "https://accounts.x.ai/"
DEVICE_CODE_URL = f"{OIDC_ISSUER}/oauth2/device/code"
DEVICE_VERIFY_URL = f"{OIDC_ISSUER}/oauth2/device/verify"
DEVICE_APPROVE_URL = f"{OIDC_ISSUER}/oauth2/device/approve"
TOKEN_URL = f"{OIDC_ISSUER}/oauth2/token"
USERINFO_URL = f"{OIDC_ISSUER}/oauth2/userinfo"

CPA_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CPA_CLIENT_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}

FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}
JSON_ACCEPT = {"Accept": "application/json"}

_write_lock = threading.Lock()
_SAFE_EMAIL_RE = re.compile(r"[^\w.@+-]+", re.UNICODE)


class CpaRateLimitedError(RuntimeError):
    """auth.x.ai / accounts 限流。"""


# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------


def ensure_cpa_dependencies() -> None:
    """cpa 模式启动前检查 curl_cffi；缺则立刻失败，避免白跑注册。"""
    try:
        # 依赖声明见 requirements.txt / pyproject.toml（PyPI 名 curl-cffi）
        import curl_cffi as _curl_cffi  # noqa: F401
    except ImportError as exc:
        py = sys.executable or "python3"
        raise RuntimeError(
            "cpa 输出需要 curl-cffi，当前环境未安装。\n"
            f"  请执行: {py} -m pip install 'curl-cffi>=0.6.0'\n"
            "  或:     pip install -r requirements.txt"
        ) from exc


# ---------------------------------------------------------------------------
# 纯工具（无 IO）
# ---------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    segment += "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        return json.loads(_b64url_decode(token.split(".")[1]))
    except (ValueError, TypeError, IndexError, json.JSONDecodeError, binascii.Error):
        return {}


def rfc3339_sec(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_email_for_filename(email: str) -> str:
    email = (email or "").strip()
    if not email:
        return "unknown"
    cleaned = email.replace("/", "_").replace("\\", "_").replace("\0", "")
    cleaned = _SAFE_EMAIL_RE.sub("_", cleaned)
    return cleaned or "unknown"


def cpa_filename(email: str) -> str:
    return f"grok-{sanitize_email_for_filename(email)}.json"


def _looks_rate_limited(url: str, body: str = "") -> bool:
    blob = f"{url}\n{body}".lower()
    return any(
        marker in blob
        for marker in (
            "rate_limited",
            "rate-limited",
            "too_many_requests",
            "ratelimit",
            "429",
        )
    )


def _retry_delay_sec(cfg: Config, attempt: int) -> float:
    """线性/指数混合退避，带少量抖动。"""
    base = max(1.0, float(cfg.cpa_retry_base_sec))
    shift = min(max(attempt, 1) - 1, 4)
    delay = min(base * (2 ** shift), 180.0)
    return delay + (attempt % 5)


# ---------------------------------------------------------------------------
# 底层 HTTP：公共 OIDC（requests，对齐 mail.request_json）
# ---------------------------------------------------------------------------


def _form_request(
        method: str,
        url: str,
        data: dict[str, str],
        *,
        timeout: float,
) -> requests.Response:
    resp = requests.request(
        method,
        url,
        data=urlencode(data),
        headers=FORM_HEADERS,
        timeout=timeout,
    )
    return resp


def _json_get(url: str, *, token: str, timeout: float) -> dict[str, Any]:
    headers = dict(JSON_ACCEPT)
    headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON 响应非对象：{data!r}")
    return data


# ---------------------------------------------------------------------------
# 底层 HTTP：带 SSO 的浏览器会话（curl_cffi）
# ---------------------------------------------------------------------------


def _new_sso_session(sso_cookie: str) -> Any:
    """创建携带 sso Cookie 的 curl_cffi Session（类型用 Any 规避桩噪声）。"""
    ensure_cpa_dependencies()
    # 延迟导入：依赖包 PyPI 名为 curl-cffi
    import curl_cffi.requests as curl_requests  # type: ignore[import-untyped]

    # Session 签名含 TypedDict Unpack；经 object 再当无参工厂，避免 IDE 误报
    session_type = cast(object, curl_requests.Session)
    factory = cast(Callable[[], Any], session_type)
    session: Any = factory()
    session.cookies.set("sso", sso_cookie, domain=".x.ai")
    return session


def _session_get(session: Any, url: str, *, timeout: float) -> Any:
    return session.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True)


def _session_post_form(
        session: Any,
        url: str,
        data: dict[str, str],
        *,
        timeout: float,
) -> Any:
    return session.post(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=timeout,
        allow_redirects=True,
    )


# ---------------------------------------------------------------------------
# 单接口：OIDC Device Flow
# ---------------------------------------------------------------------------


def request_device_code(cfg: Config) -> dict[str, Any]:
    """POST /oauth2/device/code → device_code / user_code / verification_uri_complete。"""
    resp = _form_request(
        "POST",
        DEVICE_CODE_URL,
        {"client_id": OIDC_CLIENT_ID, "scope": OIDC_SCOPES},
        timeout=cfg.cpa_http_timeout_sec,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"device/code HTTP {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("device_code") or not data.get("user_code"):
        raise RuntimeError(f"device/code 响应缺少字段：{data!r}")
    return data


def assert_sso_valid(session: Any, cfg: Config) -> None:
    """GET accounts.x.ai/：仍在登录态则通过，否则抛错。"""
    try:
        resp = _session_get(session, ACCOUNTS_HOME, timeout=cfg.cpa_http_timeout_sec)
    except (OSError, TimeoutError, RuntimeError, ValueError, TypeError) as exc:
        raise RuntimeError(f"校验 SSO 网络失败：{exc}") from exc
    final_url = str(getattr(resp, "url", "") or "")
    if "sign-in" in final_url or "sign-up" in final_url:
        raise RuntimeError(f"SSO 无效或已失效，跳转到：{final_url}")


def open_device_verification(session: Any, verification_uri: str, cfg: Config) -> None:
    """打开 verification_uri_complete，使设备码与当前 SSO 会话关联。"""
    try:
        _session_get(session, verification_uri, timeout=cfg.cpa_http_timeout_sec)
    except (OSError, TimeoutError, RuntimeError, ValueError, TypeError) as exc:
        raise RuntimeError(f"打开 verification_uri 失败：{exc}") from exc


def verify_device_user_code(session: Any, user_code: str, cfg: Config) -> str:
    """POST /oauth2/device/verify；成功后 URL 应含 consent。返回最终 URL。"""
    resp = _session_post_form(
        session,
        DEVICE_VERIFY_URL,
        {"user_code": user_code},
        timeout=cfg.cpa_http_timeout_sec,
    )
    final_url = str(getattr(resp, "url", "") or "")
    body = (getattr(resp, "text", None) or "")[:300]
    if _looks_rate_limited(final_url, body):
        raise CpaRateLimitedError(f"verify 限流：{final_url}")
    if "consent" not in final_url:
        raise RuntimeError(f"verify 未进入 consent：{final_url}")
    return final_url


def approve_device_user_code(session: Any, user_code: str, cfg: Config) -> str:
    """POST /oauth2/device/approve action=allow；成功后 URL 应含 done。"""
    resp = _session_post_form(
        session,
        DEVICE_APPROVE_URL,
        {
            "user_code": user_code,
            "action": "allow",
            "principal_type": "User",
            "principal_id": "",
        },
        timeout=cfg.cpa_http_timeout_sec,
    )
    final_url = str(getattr(resp, "url", "") or "")
    body = (getattr(resp, "text", None) or "")[:300]
    if _looks_rate_limited(final_url, body):
        raise CpaRateLimitedError(f"approve 限流：{final_url}")
    if "done" not in final_url:
        raise RuntimeError(f"approve 未完成：{final_url}")
    return final_url


def poll_for_access_token(
        cfg: Config,
        device_code: str,
        *,
        interval_sec: float,
        expires_in_sec: float,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """轮询 POST /oauth2/token（device_code grant），对齐 mail.poll 的截止时间写法。"""
    interval = max(1.0, float(interval_sec))
    poll_cap = min(float(expires_in_sec), float(cfg.cpa_poll_timeout_sec))
    deadline = now_fn() + poll_cap
    last_error = ""

    while now_fn() < deadline:
        sleep_fn(interval)
        resp = _form_request(
            "POST",
            TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": OIDC_CLIENT_ID,
                "device_code": device_code,
            },
            timeout=cfg.cpa_http_timeout_sec,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict) or not data.get("access_token"):
                raise RuntimeError(f"token 响应缺少 access_token：{data!r}")
            return data

        try:
            err_body = resp.json()
        except (ValueError, json.JSONDecodeError, requests.JSONDecodeError):
            last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
            continue

        error = str(err_body.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        last_error = error or str(err_body)
        raise RuntimeError(f"token 失败：{last_error}")

    raise TimeoutError(
        f"{poll_cap:.0f}s 内未拿到 access_token"
        + (f"（最后错误：{last_error}）" if last_error else "")
    )


def fetch_oidc_userinfo(cfg: Config, access_token: str) -> dict[str, Any]:
    """GET /oauth2/userinfo；失败返回空 dict（不阻断主流程）。"""
    if not access_token:
        return {}
    try:
        return _json_get(
            USERINFO_URL,
            token=access_token,
            timeout=cfg.cpa_http_timeout_sec,
        )
    except (requests.RequestException, ValueError, TypeError, RuntimeError):
        return {}


# ---------------------------------------------------------------------------
# 编排：SSO → token dict
# ---------------------------------------------------------------------------


def exchange_sso_for_oauth_token(
        cfg: Config,
        sso_cookie: str,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """
    用有效 SSO Cookie 走完 Device Flow，返回 OAuth token 字典。
    限流 / 瞬时失败时按 cfg.cpa_max_retries 退避重试。
    """
    sso = (sso_cookie or "").strip()
    if not sso:
        raise RuntimeError("SSO cookie 为空")

    session = _new_sso_session(sso)
    assert_sso_valid(session, cfg)
    print("[信息] SSO 有效，开始 Device Flow 换票", flush=True)

    max_attempts = max(1, int(cfg.cpa_max_retries))
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            device = request_device_code(cfg)
            user_code = str(device["user_code"])
            device_code = str(device["device_code"])
            verification_uri = str(
                device.get("verification_uri_complete")
                or device.get("verification_uri")
                or ""
            )
            if not verification_uri:
                raise RuntimeError(f"device/code 无 verification_uri：{device!r}")

            print(
                f"[信息] 申请 device_code 成功 user_code={user_code} "
                f"（第 {attempt}/{max_attempts} 次）",
                flush=True,
            )
            open_device_verification(session, verification_uri, cfg)
            verify_device_user_code(session, user_code, cfg)
            approve_device_user_code(session, user_code, cfg)
            print("[信息] 设备授权已确认，轮询 token…", flush=True)

            token = poll_for_access_token(
                cfg,
                device_code,
                interval_sec=float(device.get("interval") or 5),
                expires_in_sec=float(device.get("expires_in") or 1800),
                sleep_fn=sleep_fn,
            )

            # 补 email（access JWT 通常无 email claim）
            if not token.get("email"):
                info = fetch_oidc_userinfo(cfg, str(token.get("access_token") or ""))
                if info.get("email"):
                    token = dict(token)
                    token["email"] = info["email"]

            print(
                f"[信息] 已拿到 access_token expires_in={token.get('expires_in')}s"
                + (" + refresh_token" if token.get("refresh_token") else ""),
                flush=True,
            )
            return token

        except CpaRateLimitedError as exc:
            last_exc = exc
            delay = _retry_delay_sec(cfg, attempt)
            print(
                f"[警告] Device Flow 限流（{attempt}/{max_attempts}）：{exc}；"
                f"{delay:.0f}s 后重试",
                flush=True,
            )
            if attempt >= max_attempts:
                break
            sleep_fn(delay)
        except (TimeoutError, RuntimeError, requests.RequestException) as exc:
            last_exc = exc
            # 非限流：少量重试后放弃
            if attempt >= max_attempts:
                break
            delay = min(_retry_delay_sec(cfg, attempt), 30.0)
            print(
                f"[警告] Device Flow 失败（{attempt}/{max_attempts}）：{exc}；"
                f"{delay:.0f}s 后重试",
                flush=True,
            )
            sleep_fn(delay)

    raise RuntimeError(
        f"SSO → OAuth token 失败（已重试 {max_attempts} 次）"
        + (f"：{last_exc}" if last_exc else "")
    )


# ---------------------------------------------------------------------------
# 组装 / 落盘
# ---------------------------------------------------------------------------


def build_cpa_entry(token: dict[str, Any], email: str = "") -> dict[str, Any]:
    """OAuth token dict → cliproxyapi 扁平 type=xai 结构。"""
    access = str(token.get("access_token") or token.get("key") or "")
    refresh = str(token.get("refresh_token") or "")
    id_token = str(token.get("id_token") or "")
    token_type = str(token.get("token_type") or "Bearer")
    expires_in = int(token.get("expires_in") or 21600)

    access_payload = decode_jwt_payload(access)
    id_payload = decode_jwt_payload(id_token) if id_token else {}

    sub = str(
        access_payload.get("sub")
        or access_payload.get("principal_id")
        or id_payload.get("sub")
        or ""
    )
    resolved_email = (
            (email or "").strip()
            or str(token.get("email") or "")
            or str(id_payload.get("email") or "")
            or str(access_payload.get("email") or "")
    )

    if "exp" in access_payload:
        expired = rfc3339_sec(float(access_payload["exp"]))
    else:
        expired = rfc3339_sec(time.time() + expires_in)

    if "iat" in access_payload:
        last_refresh = rfc3339_sec(float(access_payload["iat"]))
    else:
        last_refresh = rfc3339_sec()

    return {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": access,
        "refresh_token": refresh,
        "token_type": token_type,
        "expires_in": expires_in,
        "expired": expired,
        "last_refresh": last_refresh,
        "email": resolved_email,
        "sub": sub,
        "base_url": CPA_BASE_URL,
        "token_endpoint": TOKEN_URL,
        "redirect_uri": CPA_REDIRECT_URI,
        "disabled": False,
        "headers": dict(CPA_CLIENT_HEADERS),
        "id_token": id_token,
    }


def resolve_cpa_output_dir(configured_path: str = "") -> Path:
    """
    解析 CPA 输出目录。
    - 配置了 path：使用该目录（不存在则创建）
    - 未配置：当前工作目录 `{yyyyMMdd-HHmm}-{序号}`
    """
    configured = (configured_path or "").strip()
    if configured:
        out = Path(configured).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        return out

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    root = Path.cwd()
    seq = 1
    with _write_lock:
        while True:
            candidate = root / f"{stamp}-{seq}"
            try:
                candidate.mkdir(parents=False, exist_ok=False)
                return candidate.resolve()
            except FileExistsError:
                seq += 1
            if seq > 10_000:
                raise RuntimeError(f"无法分配 CPA 输出目录：{root}/{stamp}-*")


def write_cpa_account_file(
        out_dir: str | Path,
        token: dict[str, Any],
        email: str = "",
) -> Path:
    """线程安全写出 grok-{email}.json（compact、权限 600）。"""
    entry = build_cpa_entry(token, email=email)
    resolved_email = str(entry.get("email") or email or "").strip()
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / cpa_filename(resolved_email)

    payload = json.dumps(entry, separators=(",", ":"), ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _write_lock:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def sso_to_cpa_file(
        cfg: Config,
        sso_cookie: str,
        email: str,
        out_dir: str | Path,
) -> Path:
    """
    主入口：SSO → Device Flow 换票 → 写入 out_dir/grok-{email}.json。
    失败抛 RuntimeError / TimeoutError（由 register 捕获记失败）。
    """
    token = exchange_sso_for_oauth_token(cfg, sso_cookie)
    return write_cpa_account_file(out_dir, token, email=email)
