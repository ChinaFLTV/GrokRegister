"""Duckmail HTTP 客户端：取 token、列邮件、筛选验证码、标已读、轮询。"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import quote

import requests

from config import Config
from helpers import extract_confirmation_code

AUTH_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
HYDRA_MEMBER = "hydra:member"


def _endpoint_url(base_url: str, endpoint: str) -> str:
    # base_url 形如 https://www.duckmail.sbs/api/mail
    # 接口形态：?endpoint=%2Ftoken 或 ?endpoint=%2Fmessages%2F{id}
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    return f"{base_url}?endpoint={quote(endpoint, safe='')}"


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    headers = dict(JSON_HEADERS)
    if token:
        headers[AUTH_HEADER] = f"{BEARER_PREFIX}{token}"
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_token(cfg: Config) -> str:
    url = _endpoint_url(cfg.duckmail_base_url, cfg.token_endpoint)
    data = request_json(
        "POST",
        url,
        json_body={"address": cfg.duckmail_address, "password": cfg.duckmail_password},
    )
    token = data.get("token")
    if not token:
        raise RuntimeError(f"响应中缺少 token：{data!r}")
    return str(token)


def fetch_messages(cfg: Config, token: str) -> list[dict[str, Any]]:
    url = _endpoint_url(cfg.duckmail_base_url, cfg.messages_endpoint)
    data = request_json("GET", url, token=token)
    members = data.get(HYDRA_MEMBER, [])
    if not isinstance(members, list):
        raise RuntimeError(f"邮件列表响应格式异常：{data!r}")
    return members


def mark_message_read(cfg: Config, token: str, message_id: str) -> Any:
    endpoint = f"{cfg.messages_endpoint.rstrip('/')}/{message_id}"
    url = _endpoint_url(cfg.duckmail_base_url, endpoint)
    return request_json("GET", url, token=token)


def _from_address(msg: dict[str, Any]) -> str:
    from_field = msg.get("from") or {}
    if isinstance(from_field, dict):
        return str(from_field.get("address") or "").lower()
    return str(from_field).lower()


def _to_addresses(msg: dict[str, Any]) -> list[str]:
    to_field = msg.get("to") or []
    if isinstance(to_field, dict):
        to_field = [to_field]
    addrs: list[str] = []
    for item in to_field:
        if isinstance(item, dict):
            addrs.append(str(item.get("address") or "").lower())
        else:
            addrs.append(str(item).lower())
    return addrs


def is_unseen_xai_confirmation(
    msg: dict[str, Any],
    cfg: Config,
    target_email: str | None = None,
) -> bool:
    if msg.get("seen") is True:
        return False
    if _from_address(msg) != cfg.from_address.lower():
        return False
    subject = str(msg.get("subject") or "")
    if cfg.subject_marker.lower() not in subject.lower():
        return False
    if target_email:
        want = target_email.strip().lower()
        if want not in _to_addresses(msg):
            return False
    return True


def pick_latest_confirmation(
    messages: list[dict[str, Any]],
    cfg: Config,
    target_email: str | None = None,
) -> dict[str, Any] | None:
    matches = [
        m for m in messages if is_unseen_xai_confirmation(m, cfg, target_email)
    ]
    if not matches:
        return None

    def sort_key(m: dict[str, Any]) -> str:
        return str(m.get("createdAt") or m.get("id") or "")

    return max(matches, key=sort_key)


def poll_for_confirmation_code(
    cfg: Config,
    token: str,
    *,
    target_email: str | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
    fetch_fn: Callable[[Config, str], list[dict[str, Any]]] | None = None,
    mark_fn: Callable[[Config, str, str], Any] | None = None,
) -> str:
    """轮询直到出现未读 xAI 验证码邮件（可按收件人过滤），标为已读并返回验证码。"""
    fetch = fetch_fn or fetch_messages
    mark = mark_fn or mark_message_read
    deadline = now_fn() + cfg.poll_timeout_sec
    last_error: Exception | None = None

    while True:
        try:
            messages = fetch(cfg, token)
            selected = pick_latest_confirmation(messages, cfg, target_email)
            if selected is not None:
                msg_id = str(selected.get("id") or selected.get("msgid") or "")
                if not msg_id:
                    raise RuntimeError(f"邮件缺少 id：{selected!r}")
                code = extract_confirmation_code(str(selected.get("subject") or ""))
                mark(cfg, token, msg_id)
                return code
        except Exception as exc:  # 轮询期间网络抖动：超时前继续重试
            last_error = exc

        if now_fn() >= deadline:
            detail = f" 最后错误：{last_error}" if last_error else ""
            who = f" 收件人={target_email}" if target_email else ""
            raise TimeoutError(
                f"{cfg.poll_timeout_sec} 秒内未收到 xAI 验证码邮件{who}{detail}"
            )
        sleep_fn(cfg.poll_interval_sec)
