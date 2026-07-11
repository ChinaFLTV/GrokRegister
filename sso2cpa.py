#!/usr/bin/env python3
"""
SSO cookie → auth.json（grok 原生格式） 或 cliproxyapi xai oauth 格式（纯 HTTP Device Flow）

用法:
  # 批量 SSO → cliproxyapi 可用文件（xai-{email}.json，扁平 type=xai）
  python3 sso2gropcpa.py --sso sso_list.txt --out-dir ./auth_out
  # 等同于:
  python3 sso2gropcpa.py --sso sso_list.txt --cliproxy ./auth_out

  # 合并到一个 json（key 带 user_id 后缀，避免覆盖）
  python3 sso2gropcpa.py --sso sso_list.txt --out auth_merged.json --merge

  # 单行 sso → ~/.grok/auth.json
  python3 sso2gropcpa.py --sso-cookie 'eyJ...' --out ~/.grok/auth.json

  # 仅 cliproxyapi 扁平 xai oauth 格式（type=xai），不写 grok 格式
  # 输出文件名: xai-{email}.json（无 email 时用 xai-{sub}.json；cliproxyapi 靠此前缀识别）
  python3 sso2gropcpa.py --sso sso_list.txt --cliproxy ~/.cli-proxy-api --delay 15
  python3 sso2gropcpa.py --sso-cookie 'eyJ...' --cliproxy ~/.cli-proxy-api --email user@example.com

  # 从已有 grok/嵌套 auth 文件批量转成 cliproxy 扁平格式（无需重新 SSO）
  python3 sso2gropcpa.py --from-auth ./auth_out --cliproxy ~/.cli-proxy-api
  python3 sso2gropcpa.py --from-auth ./auth_out/uid.json --cliproxy ~/.cli-proxy-api
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
AUTH_KEY = f"{OIDC_ISSUER}::{CLIENT_ID}"
SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)

CLIPROXY_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CLIPROXY_TOKEN_ENDPOINT = f"{OIDC_ISSUER}/oauth2/token"
CLIPROXY_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CLIPROXY_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}


def b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_payload(token: str) -> dict:
    try:
        return json.loads(b64url_decode(token.split(".")[1]))
    except Exception:
        return {}


def rfc3339_ns(ts: float | None = None) -> str:
    """2026-07-10T01:00:00.000000000Z"""
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"


def rfc3339_sec(ts: float | None = None) -> str:
    """RFC3339 到秒精度: 2026-07-10T19:58:39Z"""
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class RateLimitedError(Exception):
    """auth.x.ai 限流（verify/approve 重试耗尽）。"""


def is_rate_limited(url: str, body: str = "") -> bool:
    """识别 accounts.x.ai / auth.x.ai 的 rate_limited 跳转或响应。"""
    blob = f"{url}\n{body}".lower()
    return (
        "rate_limited" in blob
        or "rate-limited" in blob
        or "too_many_requests" in blob
        or "ratelimit" in blob
        or "429" in blob
    )


def backoff_sec(base: float, attempt: int, cap: float = 120.0) -> float:
    if base <= 0:
        base = 10.0
    if attempt < 1:
        attempt = 1
    shift = min(attempt - 1, 4)
    d = base * (2**shift)
    if d > cap:
        d = cap
    return d + secrets.randbelow(5)


class Pacer:
    """自适应账号间隔：限流抬高，成功缓慢回落。"""

    def __init__(self, base: float = 0, max_delay: float = 180):
        if base < 0:
            base = 0
        if max_delay < 30:
            max_delay = 180
        if base == 0:
            base = 20  # 未指定 --delay 时的温和默认
        self.base = float(base)
        self.current = float(base)
        self.max = float(max_delay)
        self.hits = 0

    def on_rate_limit(self) -> None:
        self.hits += 1
        nxt = max(self.current * 1.6, self.current + 20, 30.0)
        self.current = min(nxt, self.max)
        print(f"  🐢 限流退避: 下个账号间隔 → {self.current:.0f}s (连续限流 {self.hits})")

    def on_success(self) -> None:
        if self.hits > 0:
            self.hits -= 1
        if self.current > self.base:
            self.current = max(self.base, self.current * 0.88)
            print(f"  🐇 恢复中: 账号间隔 → {self.current:.0f}s")

    def wait_between(self, remaining: bool) -> None:
        if not remaining or self.current <= 0:
            return
        total = self.current + secrets.randbelow(6)
        print(f"  ⏱ 等待 {total:.0f}s 后继续下一个账号...")
        time.sleep(total)


def request_device_code() -> dict | None:
    data = urllib.parse.urlencode({"client_id": CLIENT_ID, "scope": SCOPES}).encode()
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ❌ device/code HTTP {e.code}: {e.read().decode()[:200]}")
        return None


def poll_token(device_code: str, interval: int, expires_in: int, timeout: int = 60) -> dict | None:
    deadline = time.time() + min(expires_in, timeout)
    while time.time() < deadline:
        time.sleep(interval)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            print(f"  ❌ token: {error}")
            return None
    print("  ❌ 轮询超时")
    return None


def fetch_userinfo(access_token: str) -> dict:
    """用 access_token 调 OIDC userinfo，取 email 等资料。失败返回空 dict。"""
    if not access_token:
        return {}
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/userinfo",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  ⚠ userinfo 失败: {e}")
        return {}


def enrich_token_with_userinfo(token: dict) -> dict:
    """
    Device Flow / refresh 通常不返回 id_token，access JWT 也无 email claim。
    补拉 userinfo，把 email 写进 token['_email']（供 cliproxy/grok entry 使用）。
    """
    if not token:
        return token
    if token.get("_email") or token.get("email"):
        return token
    access = token.get("access_token") or token.get("key") or ""
    info = fetch_userinfo(access)
    if info.get("email"):
        token["_email"] = info["email"]
        token["_email_verified"] = bool(info.get("email_verified"))
        token["_name"] = info.get("name") or ""
        print(f"  📧 userinfo email={info['email']}")
    else:
        print("  ⚠ userinfo 无 email")
    return token


def sso_to_token(
    sso_cookie: str,
    max_retries: int = 8,
    base_delay: float = 15.0,
) -> dict | None:
    """SSO cookie → token dict (access/refresh/expires_in)。

    限流时会退避并重新申请 device code；重试耗尽抛 RateLimitedError。
    """
    try:
        from curl_cffi import requests
    except ImportError:
        import sys as _sys

        py = _sys.executable or "python3"
        print(
            "  ❌ 缺少依赖 curl_cffi。请安装:\n"
            f"     {py} -m pip install 'curl_cffi>=0.6.0'\n"
            "     # 或: pip install -r requirements.txt"
        )
        return None

    s = requests.Session()
    s.cookies.set("sso", sso_cookie, domain=".x.ai")

    try:
        r = s.get("https://accounts.x.ai/", impersonate="chrome", timeout=15)
    except Exception as e:
        print(f"  ❌ 网络错误: {e}")
        return None
    if "sign-in" in r.url or "sign-up" in r.url:
        print("  ❌ sso 无效")
        return None
    print("  ✅ sso 有效")

    dc: dict | None = None
    rate_hits = 0

    def fresh_device() -> bool:
        nonlocal dc
        print("  🔑 Device Flow...")
        dc = request_device_code()
        if not dc:
            return False
        print(f"  📋 user_code: {dc.get('user_code')}")
        try:
            s.get(dc["verification_uri_complete"], impersonate="chrome", timeout=15)
        except Exception as e:
            print(f"  ❌ verification_uri 异常: {e}")
            return False
        return True

    if not fresh_device():
        return None

    # verify + approve —— 限流时退避 + 重新申请 device code
    verify_ok = False
    approve_ok = False
    for attempt in range(1, max_retries + 1):
        try:
            r = s.post(
                f"{OIDC_ISSUER}/oauth2/device/verify",
                data={"user_code": dc["user_code"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=15,
                allow_redirects=True,
            )
            body_snip = (r.text or "")[:300] if hasattr(r, "text") else ""
            if is_rate_limited(r.url, body_snip):
                rate_hits += 1
                delay = backoff_sec(base_delay, attempt, 180)
                print(f"  ⏳ verify 限流, 第 {attempt}/{max_retries} 次重试, 等待 {delay:.0f}s...")
                time.sleep(delay)
                if not fresh_device():
                    return None
                continue
            if "consent" not in r.url:
                print(f"  ❌ verify 失败: {r.url}")
                return None
            verify_ok = True
        except Exception as e:
            delay = backoff_sec(base_delay, attempt, 120)
            print(f"  ⏳ verify 异常 ({e}), 第 {attempt}/{max_retries} 次重试, 等待 {delay:.0f}s...")
            time.sleep(delay)
            continue

        try:
            r = s.post(
                f"{OIDC_ISSUER}/oauth2/device/approve",
                data={
                    "user_code": dc["user_code"],
                    "action": "allow",
                    "principal_type": "User",
                    "principal_id": "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=15,
                allow_redirects=True,
            )
            body_snip = (r.text or "")[:300] if hasattr(r, "text") else ""
            if is_rate_limited(r.url, body_snip):
                rate_hits += 1
                delay = backoff_sec(base_delay, attempt, 180)
                print(f"  ⏳ approve 限流, 第 {attempt}/{max_retries} 次重试, 等待 {delay:.0f}s...")
                time.sleep(delay)
                verify_ok = False
                if not fresh_device():
                    return None
                continue
            if "done" not in r.url:
                print(f"  ❌ approve 失败: {r.url}")
                return None
            approve_ok = True
            break
        except Exception as e:
            delay = backoff_sec(base_delay, attempt, 120)
            print(f"  ⏳ approve 异常 ({e}), 第 {attempt}/{max_retries} 次重试, 等待 {delay:.0f}s...")
            time.sleep(delay)
            continue

    if not verify_ok:
        print("  ❌ verify 重试耗尽")
        if rate_hits > 0:
            raise RateLimitedError("verify 重试耗尽")
        return None
    if not approve_ok:
        print("  ❌ approve 重试耗尽")
        if rate_hits > 0:
            raise RateLimitedError("approve 重试耗尽")
        return None

    print("  ✅ 授权确认")

    token = poll_token(
        dc["device_code"],
        dc.get("interval", 5),
        dc.get("expires_in", 1800),
    )
    if not token:
        return None
    print(
        f"  ✅ access_token (expires_in={token.get('expires_in')}s)"
        + (" + refresh_token" if token.get("refresh_token") else "")
        + (" + id_token" if token.get("id_token") else "")
    )
    return enrich_token_with_userinfo(token)


def token_to_auth_entry(token: dict, email: str = "") -> tuple[str, dict]:
    """
    返回 (top_level_key, entry)
    top_level_key 固定为 issuer::client_id（与 ~/.grok/auth.json 一致）
    """
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    payload = decode_jwt_payload(access)

    user_id = payload.get("sub") or payload.get("principal_id") or ""
    principal_id = payload.get("principal_id") or user_id
    principal_type = payload.get("principal_type") or "User"

    expires_in = int(token.get("expires_in") or 21600)
    # 优先用 JWT exp
    if "exp" in payload:
        expires_at = rfc3339_ns(float(payload["exp"]))
    else:
        expires_at = rfc3339_ns(time.time() + expires_in)

    iat = payload.get("iat")
    create_time = rfc3339_ns(float(iat) if iat else time.time())

    entry = {
        "key": access,
        "auth_mode": "oidc",
        "create_time": create_time,
        "user_id": user_id,
        "email": email or token.get("_email") or token.get("email") or "",
        "principal_type": principal_type,
        "principal_id": principal_id,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_client_id": CLIENT_ID,
    }
    return AUTH_KEY, entry


def cliproxy_filename(email: str = "", sub: str = "") -> str:
    """cliproxyapi 落盘文件名：必须 xai- 前缀，优先 xai-{email}.json，否则 xai-{sub}.json。"""
    email = (email or "").strip()
    sub = (sub or "").strip()
    if email:
        return f"xai-{email}.json"
    if sub:
        return f"xai-{sub}.json"
    return f"xai-anon_{secrets.token_hex(4)}.json"


def token_to_cliproxy_entry(token: dict, email: str = "") -> tuple[str, dict]:
    """
    返回 cliproxyapi 可用的扁平 xai oauth 格式。

    Returns:
        (filename, entry) — filename 形如 xai-user@example.com.json
    """
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    id_token = token.get("id_token") or ""
    token_type = token.get("token_type") or "Bearer"
    expires_in = int(token.get("expires_in") or 21600)

    access_payload = decode_jwt_payload(access)
    id_payload = decode_jwt_payload(id_token) if id_token else {}

    sub = (
        access_payload.get("sub")
        or access_payload.get("principal_id")
        or id_payload.get("sub")
        or ""
    )
    resolved_email = (
        email
        or token.get("_email")
        or token.get("email")
        or id_payload.get("email")
        or access_payload.get("email")
        or ""
    )

    if "exp" in access_payload:
        expired = rfc3339_sec(float(access_payload["exp"]))
    else:
        expired = rfc3339_sec(time.time() + expires_in)

    if "iat" in access_payload:
        last_refresh = rfc3339_sec(float(access_payload["iat"]))
    else:
        last_refresh = rfc3339_sec()

    entry = {
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
        "base_url": CLIPROXY_BASE_URL,
        "token_endpoint": CLIPROXY_TOKEN_ENDPOINT,
        "redirect_uri": CLIPROXY_REDIRECT_URI,
        "disabled": False,
        "headers": dict(CLIPROXY_HEADERS),
        "id_token": id_token,
    }
    filename = cliproxy_filename(resolved_email, sub)
    return filename, entry


def auth_file_to_token(data: dict) -> tuple[dict, str] | None:
    """
    从已落盘的 auth JSON 抽出 token dict + email。

    支持:
      1) grok 嵌套: { "https://auth.x.ai::client": { key, refresh_token, email, ... } }
      2) 扁平 xai:  { type:xai, access_token, refresh_token, ... }
      3) 仅 entry 本体: { key/access_token, refresh_token, ... }
    """
    if not isinstance(data, dict) or not data:
        return None

    # 已是扁平 xai oauth
    if data.get("type") == "xai" or data.get("auth_kind") == "oauth":
        access = data.get("access_token") or data.get("key") or ""
        if not access:
            return None
        token = {
            "access_token": access,
            "refresh_token": data.get("refresh_token") or "",
            "token_type": data.get("token_type") or "Bearer",
            "expires_in": int(data.get("expires_in") or 21600),
            "id_token": data.get("id_token") or "",
        }
        return token, (data.get("email") or "")

    # 扁平 entry（无 type，但有 key）
    if "key" in data and ("refresh_token" in data or "auth_mode" in data):
        access = data.get("key") or ""
        if not access:
            return None
        exp_in = 21600
        payload = decode_jwt_payload(access)
        if "exp" in payload and "iat" in payload:
            exp_in = max(1, int(payload["exp"]) - int(payload["iat"]))
        token = {
            "access_token": access,
            "refresh_token": data.get("refresh_token") or "",
            "token_type": "Bearer",
            "expires_in": exp_in,
            "id_token": data.get("id_token") or "",
        }
        return token, (data.get("email") or "")

    # 嵌套 issuer::client_id
    for k, v in data.items():
        if k == "disabled":
            continue
        if isinstance(v, dict) and (v.get("key") or v.get("access_token")):
            access = v.get("access_token") or v.get("key") or ""
            if not access:
                continue
            exp_in = 21600
            payload = decode_jwt_payload(access)
            if "exp" in payload and "iat" in payload:
                exp_in = max(1, int(payload["exp"]) - int(payload["iat"]))
            token = {
                "access_token": access,
                "refresh_token": v.get("refresh_token") or "",
                "token_type": v.get("token_type") or "Bearer",
                "expires_in": int(v.get("expires_in") or exp_in),
                "id_token": v.get("id_token") or "",
            }
            return token, (v.get("email") or data.get("email") or "")

    return None


def write_auth_json(path: Path, auth_key: str, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {auth_key: entry}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_auth_json(path: Path, auth_key: str, entry: dict, unique: bool = True) -> None:
    """
    合并写入。unique=True 时 key 变成 issuer::client_id::user_id，避免多账号互相覆盖。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    key = auth_key
    if unique and entry.get("user_id"):
        key = f"{auth_key}::{entry['user_id']}"
    existing[key] = entry
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_cliproxy_file(cliproxy_dir: Path, token: dict, email: str = "") -> Path:
    """写出扁平 type=xai 文件：compact JSON、无尾随换行、权限 600。

    文件名必须为 xai-{email}.json（cliproxyapi 识别用）
    """
    cliproxy_dir.mkdir(parents=True, exist_ok=True)
    filename, clip_entry = token_to_cliproxy_entry(token, email=email)
    clip_path = cliproxy_dir / filename
    tmp = clip_path.with_suffix(clip_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(clip_entry, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, clip_path)
    os.chmod(clip_path, 0o600)
    return clip_path


def load_sso_list(path: str | None, single: str | None) -> list[tuple[str, str]]:
    """返回 [(sso_cookie, email), ...]。email 可从 邮箱----密码----sso 行解析。"""
    if single:
        return [(single.strip(), "")]
    if not path:
        return []
    out: list[tuple[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        email = ""
        sso = line
        # 兼容 邮箱----密码----sso 或 邮箱----sso
        if "----" in line:
            parts = [p.strip() for p in line.split("----") if p.strip()]
            if len(parts) >= 2:
                # 最后一段是 sso；若第一段像邮箱则记下
                sso = parts[-1]
                if "@" in parts[0]:
                    email = parts[0]
        out.append((sso, email))
    return out


def iter_from_auth_paths(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    if src.is_dir():
        return sorted(p for p in src.glob("*.json") if p.is_file() and not p.name.startswith("."))
    return []


def convert_from_auth(src: Path, cliproxy_dir: Path, email_override: str = "") -> tuple[int, int]:
    """把已有 auth 文件转成 cliproxy 扁平格式。返回 (ok, fail)。"""
    paths = iter_from_auth_paths(src)
    if not paths:
        print(f"  ❌ --from-auth 无可用 json: {src}")
        return 0, 1

    ok = 0
    fail = 0
    print(f"🔄 from-auth → cliproxy: {len(paths)} 个文件 → {cliproxy_dir}")
    for i, p in enumerate(paths, 1):
        print(f"\n[{i}/{len(paths)}] {p.name}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            parsed = auth_file_to_token(data)
            if not parsed:
                fail += 1
                print("  ❌ 无法识别 auth 结构")
                continue
            token, email = parsed
            email = email_override or email
            if not email:
                token = enrich_token_with_userinfo(token)
                email = token.get("_email") or email
            out = write_cliproxy_file(cliproxy_dir, token, email=email)
            ok += 1
            print(f"  💾 cliproxy → {out}")
        except Exception as e:
            fail += 1
            print(f"  ❌ 异常: {e}")
    print(f"\n📊 转换完成: {ok}/{len(paths)} 成功, {fail} 失败")
    return ok, fail


def main() -> int:
    ap = argparse.ArgumentParser(
        description="SSO cookie → cliproxyapi xai oauth（扁平 type=xai）/ 可选 grok 嵌套 auth.json"
    )
    ap.add_argument("--sso", metavar="FILE", help="sso 列表文件（一行一个 JWT，或 邮箱----密码----sso）")
    ap.add_argument("--sso-cookie", metavar="JWT", help="单个 sso cookie")
    ap.add_argument(
        "--from-auth",
        metavar="PATH",
        help="从已有 grok/嵌套/扁平 auth 文件或目录转换为 cliproxy 格式（需配合 --cliproxy 或 --out-dir）",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="【grok 嵌套格式】输出 auth.json（仅在需要 ~/.grok/auth.json 时使用）",
    )
    ap.add_argument(
        "--out-dir",
        default=None,
        help="【cliproxy 扁平格式】输出目录，每个账号一个 xai-{email}.json "
        "（与 --cliproxy 相同，可直接 cp 到 ~/.cli-proxy-api）",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="【grok】合并到 --out，key 用 issuer::client_id::user_id",
    )
    ap.add_argument(
        "--grok-out-dir",
        default=None,
        help="【grok 嵌套格式】批量写 {user_id}.json（仅 grok CLI 需要时用）",
    )
    ap.add_argument(
        "--delay",
        type=int,
        default=0,
        help="每个账号基础间隔秒数（自适应，限流会自动加大；0=默认20）",
    )
    ap.add_argument(
        "--max-delay",
        type=int,
        default=180,
        help="自适应间隔上限秒数（默认 180）",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=8,
        help="单账号 verify/approve 最大重试次数（默认 8）",
    )
    ap.add_argument(
        "--account-retries",
        type=int,
        default=3,
        help="单账号整轮失败后的重跑次数（限流专用，默认 3）",
    )
    ap.add_argument("--email", default="", help="写入 entry.email（可选；否则自动 userinfo）")
    ap.add_argument(
        "--cliproxy",
        default=None,
        help="【cliproxy 扁平格式】输出目录，文件名 xai-{email}.json，"
        "例如: --cliproxy ~/.cli-proxy-api",
    )
    args = ap.parse_args()

    # 统一 cliproxy 输出目录：--cliproxy 与 --out-dir 等价（扁平 type=xai）
    cliproxy_dirs: list[Path] = []
    if args.cliproxy:
        cliproxy_dirs.append(Path(args.cliproxy).expanduser())
    if args.out_dir:
        p = Path(args.out_dir).expanduser()
        if p not in cliproxy_dirs:
            cliproxy_dirs.append(p)

    # 路径 A：已有 auth → cliproxy（无需 SSO）
    if args.from_auth:
        if not cliproxy_dirs:
            ap.error("--from-auth 需要同时指定 --cliproxy 或 --out-dir")
        # 写到第一个目标；若两个都给且不同则都写
        total_ok = total_fail = 0
        for dest in cliproxy_dirs:
            ok, fail = convert_from_auth(
                Path(args.from_auth).expanduser(),
                dest,
                email_override=args.email,
            )
            total_ok += ok
            total_fail += fail
        return 0 if total_fail == 0 else 1

    cookies = load_sso_list(args.sso, args.sso_cookie)
    if not cookies:
        ap.error("需要 --sso / --sso-cookie，或 --from-auth + --cliproxy/--out-dir")

    want_grok = bool(args.out or args.merge or args.grok_out_dir)
    want_cliproxy = bool(cliproxy_dirs)

    if not want_grok and not want_cliproxy:
        # 默认批量写 cliproxy 扁平格式到 ./auth_out
        if len(cookies) > 1:
            cliproxy_dirs = [Path("./auth_out")]
            want_cliproxy = True
            print("批量模式默认 --out-dir ./auth_out（cliproxy 扁平 xai-{email}.json）")
        else:
            # 单账号默认写 ~/.cli-proxy-api
            cliproxy_dirs = [Path.home() / ".cli-proxy-api"]
            want_cliproxy = True
            print(f"单账号默认 --cliproxy {cliproxy_dirs[0]}")

    targets = []
    if want_cliproxy:
        targets.append("cliproxy(type=xai)")
    if want_grok:
        targets.append("grok(nested)")

    pace = Pacer(base=args.delay, max_delay=args.max_delay)
    print(
        f"🚀 SSO → {'+'.join(targets)}: {len(cookies)} 个, "
        f"delay={pace.current:.0f}s (自适应 max={args.max_delay}), "
        f"retries={args.retries}, account-retries={args.account_retries}"
    )

    ok = 0
    fail = 0
    acct_max = max(1, int(args.account_retries))

    for i, (sso, line_email) in enumerate(cookies, 1):
        print(f"\n{'=' * 60}\n[{i}/{len(cookies)}] {line_email or '...'}\n{'=' * 60}")
        token = None
        last_err: Exception | None = None
        try:
            for ar in range(1, acct_max + 1):
                try:
                    token = sso_to_token(sso, max_retries=args.retries, base_delay=15.0)
                    if token:
                        break
                    # 非限流失败：不再账号级重跑
                    break
                except RateLimitedError as e:
                    last_err = e
                    if ar < acct_max:
                        pace.on_rate_limit()
                        cool = backoff_sec(pace.current, ar, args.max_delay)
                        print(
                            f"  ♻️ 账号级重试 {ar}/{acct_max}（限流）: "
                            f"冷却 {cool:.0f}s 后重跑本账号..."
                        )
                        time.sleep(cool)
                        continue
                    raise

            if not token:
                fail += 1
                if isinstance(last_err, RateLimitedError):
                    pace.on_rate_limit()
                print(f"  ❌ [{i}] 失败{f': {last_err}' if last_err else ''}")
            else:
                # 邮箱优先级: --email > 列表行邮箱 > userinfo/_email > id_token
                email_for_entry = args.email or line_email or ""

                label = ""
                if want_cliproxy:
                    for dest in cliproxy_dirs:
                        clip_path = write_cliproxy_file(dest, token, email=email_for_entry)
                        label = clip_path.name
                        print(f"  💾 cliproxy → {clip_path}")

                if want_grok:
                    key, entry = token_to_auth_entry(token, email=email_for_entry)
                    uid = entry.get("user_id") or secrets.token_hex(4)
                    if not label:
                        label = uid
                    if args.grok_out_dir:
                        p = Path(args.grok_out_dir) / f"{uid}.json"
                        write_auth_json(p, key, entry)
                        print(f"  💾 grok → {p}")
                    if args.out:
                        if args.merge or len(cookies) > 1:
                            merge_auth_json(Path(args.out), key, entry, unique=True)
                            print(f"  💾 grok merge → {args.out}")
                        else:
                            write_auth_json(Path(args.out), key, entry)
                            print(f"  💾 grok → {args.out}")

                ok += 1
                pace.on_success()
                print(f"  ✅ [{i}] 完成 {label}")
        except RateLimitedError as e:
            fail += 1
            pace.on_rate_limit()
            print(f"  ❌ [{i}] 失败: {e}")
        except Exception as e:
            fail += 1
            print(f"  ❌ [{i}] 异常: {e}")

        pace.wait_between(i < len(cookies))

    print(
        f"\n{'=' * 60}\n📊 完成: {ok}/{len(cookies)} 成功, {fail} 失败 "
        f"(最终间隔 {pace.current:.0f}s)"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())