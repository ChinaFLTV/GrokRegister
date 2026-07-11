"""Playwright 页面操作：注册 → 验证邮箱 → 完成注册。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from config import Config
from helpers import normalize_otp_chars

# get_by_role 常用 role（避免 str 与 Playwright Literal 不兼容）
AriaRole = Literal["button", "link", "textbox", "checkbox", "radio", "heading"]

# UI 自动化常见失败类型（替代 bare except Exception，消除 IDE “Too broad exception”）
_UI_ERRORS = (
    PlaywrightError,
    PlaywrightTimeout,
    TimeoutError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    StopIteration,
)

# 页面定位器（站点文案可能是英文或中文，按钮匹配串需保留双语）
EMAIL_INPUT = 'input[type="email"], input[name="email"], input[autocomplete="email"]'
# xAI 使用 input-otp：可见格是 div[data-input-otp-slot]，真输入是单个透明 input
INPUT_OTP = (
    'input[data-input-otp="true"], '
    'input[name="code"][data-input-otp], '
    'input[name="code"][maxlength="6"], '
    'input[name="code"][autocomplete="one-time-code"]'
)
CODE_INPUT_FALLBACK = (
    'input[name="code"], input[name="otp"], input[autocomplete="one-time-code"], '
    'input[inputmode="numeric"], input[placeholder*="code" i], '
    'input[placeholder*="验证" i]'
)
FIRST_NAME_INPUT = (
    'input[name="firstName"], input[name="first_name"], '
    'input[autocomplete="given-name"], input[placeholder*="First" i], '
    'input[placeholder*="名" i]'
)
LAST_NAME_INPUT = (
    'input[name="lastName"], input[name="last_name"], '
    'input[autocomplete="family-name"], input[placeholder*="Last" i], '
    'input[placeholder*="姓" i]'
)
PASSWORD_INPUT = 'input[type="password"], input[name="password"], input[autocomplete="new-password"]'
VERIFY_PAGE_MARKERS = ("验证您的邮箱", "Verify your email", "Verify your Email")
CONFIRM_EMAIL_TEXTS = [
    "Confirm email",
    "Verify",
    "Confirm",
    "Continue",
    "确认邮箱",
    "验证",
    "确认",
    "继续",
]

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def _launch_kwargs(cfg: Config) -> dict:
    kwargs: dict = {
        "headless": cfg.headless,
        "args": list(LAUNCH_ARGS),
    }
    if cfg.browser_channel:
        kwargs["channel"] = cfg.browser_channel
    return kwargs


def open_browser_context(playwright: Playwright, cfg: Config) -> BrowserContext:
    """
    打开浏览器上下文。
    配置了 user_data_dir 时使用持久化 Profile（自动化专用目录，可复用 Cloudflare 通行状态）。
    """
    kwargs = _launch_kwargs(cfg)
    user_data = (cfg.user_data_dir or "").strip()
    if user_data:
        profile_dir = Path(user_data).expanduser().resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            **kwargs,
        )

    browser = playwright.chromium.launch(**kwargs)
    context = browser.new_context()
    context._owned_browser = browser  # type: ignore[attr-defined]
    return context


def close_browser_context(context: BrowserContext) -> None:
    owned = getattr(context, "_owned_browser", None)
    context.close()
    # getattr 类型为 Any | None；用 isinstance 收窄到 Browser 再 close
    if isinstance(owned, Browser):
        owned.close()


def first_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return context.new_page()


def _fill_first(page: Page, selector: str, value: str, timeout: float) -> None:
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=timeout)
    _react_fill(loc, value)


def _react_fill(loc, value: str) -> None:
    """
    填写受控输入框（React）。普通 fill 偶发写不进，辅以原生 setter + input 事件。
    """
    loc.click(force=True)
    try:
        loc.fill("")
    except _UI_ERRORS:
        pass
    loc.fill(value)
    try:
        got = (loc.input_value(timeout=1000) or "").strip()
    except _UI_ERRORS:
        got = ""
    if got == value:
        return
    try:
        ok = loc.evaluate(
            """(el, v) => {
                const proto = window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(el, v);
                else el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                try {
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true, data: v, inputType: 'insertText'
                    }));
                } catch (e) {}
                return (el.value || '') === v;
            }""",
            value,
        )
        if ok:
            return
    except _UI_ERRORS:
        pass
    # 最后退回逐字输入
    try:
        loc.click(force=True)
        loc.press("ControlOrMeta+a")
        loc.press("Backspace")
        loc.press_sequentially(value, delay=20)
    except _UI_ERRORS:
        pass


def _click_by_texts(
        page: Page,
        texts: list[str],
        role: AriaRole = "button",
        timeout: float = 5000,
) -> None:
    for text in texts:
        try:
            page.get_by_role(role, name=text, exact=False).first.click(timeout=timeout)
            return
        except _UI_ERRORS:
            continue
    for text in texts:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=timeout)
            return
        except _UI_ERRORS:
            continue
    raise RuntimeError(f"未找到匹配控件，候选文案：{texts}")


def _page_form_error_snippet(page: Page, limit: int = 240) -> str:
    """抓取页面上可能的校验错误文案，便于诊断完成注册失败。"""
    try:
        body = page.locator("body").inner_text(timeout=2000) or ""
    except _UI_ERRORS:
        return ""
    markers = (
        "required",
        "invalid",
        "error",
        "must",
        "at least",
        "password",
        "必填",
        "无效",
        "错误",
        "至少",
        "密码",
        "请填写",
        "不能为空",
    )
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if not s or len(s) > 120:
            continue
        low = s.lower()
        if any(m in low for m in markers) or any(m in s for m in markers):
            lines.append(s)
        if len(lines) >= 6:
            break
    text = " | ".join(lines)
    return text[:limit]


def is_cloudflare_blocked(page: Page) -> bool:
    title = (page.title() or "").lower()
    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except _UI_ERRORS:
        pass
    return (
            "attention required" in title
            or "cloudflare" in title
            or "you have been blocked" in body
            or "sorry, you have been blocked" in body
    )


def _assert_not_blocked(page: Page) -> None:
    if is_cloudflare_blocked(page):
        raise RuntimeError(
            f"被站点风控拦截（Cloudflare/人机验证）：url={page.url!r} title={page.title()!r}。"
            f"请先运行：python register.py --warm-profile，在自动化专用 Profile 中手动通过验证后再注册。"
        )


def _is_retriable_navigation_error(exc: BaseException) -> bool:
    text = str(exc)
    markers = (
        "ERR_ABORTED",
        "NS_BINDING_ABORTED",
        "frame was detached",
        "Frame was detached",
        "Navigation interrupted",
        "navigating to",
    )
    # 仅对中断类错误重试；纯超时不盲目重试
    return any(m in text for m in markers) and (
            "ERR_ABORTED" in text
            or "NS_BINDING_ABORTED" in text
            or "detached" in text.lower()
            or "interrupted" in text.lower()
    )


def safe_goto(page: Page, url: str, cfg: Config) -> None:
    """
    打开页面；若因上一导航未结束导致 net::ERR_ABORTED，则短暂等待后重试。
    复用浏览器循环注册时很常见。
    """
    retries = max(1, int(cfg.goto_retries))
    last_err: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            try:
                page.evaluate("() => { try { window.stop(); } catch (e) {} }")
            except _UI_ERRORS:
                pass
            page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
            return
        except _UI_ERRORS as exc:
            last_err = exc
            if attempt >= retries or not _is_retriable_navigation_error(exc):
                raise
            print(
                f"[警告] 打开 {url!r} 被中断（{attempt}/{retries}）：{exc!s}；等待后重试…",
                flush=True,
            )
            page.wait_for_timeout(max(200, cfg.between_rounds_ms))
    if last_err:
        raise last_err


# 需清理的登录态 Cookie 名（小写匹配）；保留 cf_clearance 等风控 cookie
_AUTH_COOKIE_NAMES = frozenset(
    {
        "sso",
        "sso-rw",
        "session",
        "sessionid",
        "auth",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "jwt",
        "sid",
        "last-logged-in-with",
        "x-userid",
        "x-anonuserid",
        "x-challenge",
        "x-signature",
    }
)


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except _UI_ERRORS:
        return ""


def _url_path(url: str) -> str:
    try:
        return (urlparse(url).path or "").rstrip("/") or "/"
    except _UI_ERRORS:
        return ""


def _is_grok_host(url: str) -> bool:
    """判断是否已落到 grok.com（含子域）。"""
    host = _url_host(url)
    return host == "grok.com" or host.endswith(".grok.com")


def _is_accounts_host(url: str) -> bool:
    host = _url_host(url)
    return host == "accounts.x.ai" or host.endswith(".accounts.x.ai")


def _is_accounts_account_page(url: str) -> bool:
    """
    真正的账户页：hosts=accounts.x.ai 且 path=/account。
    注意：不能用子串 account 匹配，否则会误伤 accounts.x.ai/sign-up。
    """
    if not _is_accounts_host(url):
        return False
    path = _url_path(url).lower()
    return path == "/account" or path.startswith("/account/")


def _is_signup_or_incomplete_url(url: str) -> bool:
    """仍停在注册/验证流程的页面。"""
    if not _is_accounts_host(url):
        return False
    path = _url_path(url).lower()
    markers = ("/sign-up", "/signup", "/sign_up", "/verify", "/login", "/sign-in")
    return any(m in path for m in markers) or path in {"/", ""}


def _collect_context_cookies(page: Page) -> list[dict[str, Any]]:
    """汇总上下文 Cookie（含按常见域名再取一次，避免遗漏）。"""
    context = page.context
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    urls: list[str | None] = [
        None,
        "https://grok.com",
        "https://www.grok.com",
        "https://accounts.x.ai",
        "https://auth.x.ai",
        "https://x.ai",
    ]
    for u in urls:
        try:
            batch = context.cookies(u) if u else context.cookies()
        except _UI_ERRORS:
            continue
        for c in batch:
            # Playwright Cookie 是 TypedDict/协议对象，统一成普通 dict 再存
            item = dict(c)
            key = (
                str(item.get("name") or ""),
                str(item.get("domain") or ""),
                str(item.get("path") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def extract_sso_cookie(page: Page) -> str | None:
    """
    从当前浏览器上下文 Cookie 中取出 name 为 sso 的值。
    优先精确名 sso；若无则回退 sso-rw（部分环境仅有读写变体）。
    实测：sso 常在 accounts.x.ai/account 阶段就已写入，不必等 grok.com。
    """
    cookies = _collect_context_cookies(page)
    if not cookies:
        return None

    by_name: dict[str, str] = {}
    for c in cookies:
        name = str(c.get("name") or "")
        value = str(c.get("value") or "").strip()
        if not name or not value:
            continue
        by_name[name.lower()] = value

    if by_name.get("sso"):
        return by_name["sso"]
    if by_name.get("sso-rw"):
        return by_name["sso-rw"]
    return None


def _cookie_name_domain_summary(page: Page) -> list[str]:
    """调试用：cookie 名@域名（不包含 value）。"""
    items: list[str] = []
    for c in _collect_context_cookies(page):
        name = str(c.get("name") or "")
        domain = str(c.get("domain") or "")
        if name:
            items.append(f"{name}@{domain}" if domain else name)
    return sorted(set(items))


def registration_success_signal(page: Page) -> str | None:
    """
    注册成功信号（任一即可）：
    - Cookie 已有 sso
    - 已在 accounts.x.ai/account
    - 已在 grok.com
    返回简短原因字符串；未成功返回 None。
    """
    url = page.url or ""
    if extract_sso_cookie(page):
        return "sso_cookie"
    if _is_accounts_account_page(url):
        return "accounts_account"
    if _is_grok_host(url):
        return "grok_host"
    return None


def settle_page(page: Page, cfg: Config) -> None:
    """
    完成注册后等待成功落点（account / grok / sso 任一），不要死等 grok.com。
    """
    poll_ms = max(150, min(int(cfg.after_sso_capture_ms) or 500, 1000))
    total_ms = max(12000, min(int(cfg.timeout_ms), 45000))
    elapsed = 0
    last_url = page.url or ""
    print(f"[信息] 等待注册成功落点（account / grok / sso），当前 url={last_url!r}", flush=True)

    while elapsed <= total_ms:
        signal = registration_success_signal(page)
        if signal:
            print(
                f"[信息] 注册成功信号={signal} url={page.url!r}",
                flush=True,
            )
            page.wait_for_timeout(max(0, cfg.after_complete_ms))
            return
        cur = page.url or ""
        if cur != last_url:
            print(f"[信息] 页面跳转：{cur}", flush=True)
            last_url = cur
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms

    print(
        f"[警告] 等待注册成功落点超时，当前 url={page.url!r}；"
        f"继续尝试采集 sso",
        flush=True,
    )
    page.wait_for_timeout(max(0, cfg.after_complete_ms))


def capture_sso_from_grok(page: Page, cfg: Config) -> str:
    """
    轮询 Cookie 采集 sso。
    不主动 goto grok.com；accounts.x.ai/account 阶段通常已有 sso。
    必须在 sign_out_session / clear_auth_cookies 之前调用。
    """
    print(
        f"[信息] 轮询采集 SSO Cookie（当前 url={page.url!r}）…",
        flush=True,
    )

    poll_ms = max(150, int(cfg.after_sso_capture_ms) or 500)
    # 成功案例往往立刻有 sso；失败案例给足时间但不要叠两层 45s
    total_ms = max(10000, min(int(cfg.timeout_ms), 30000))
    elapsed = 0
    sso: str | None = None
    last_url = page.url or ""

    while elapsed <= total_ms:
        sso = extract_sso_cookie(page)
        if sso:
            break
        cur = page.url or ""
        if cur != last_url:
            print(f"[信息] 采集中页面跳转：{cur}", flush=True)
            last_url = cur
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms

    if not sso:
        names = _cookie_name_domain_summary(page)
        url = page.url or ""
        hint = ""
        if _is_signup_or_incomplete_url(url):
            hint = "（仍停在注册/登录页，可能本轮姓名密码未提交成功）"
        elif _is_accounts_account_page(url):
            hint = "（已在账户页但仍无 sso，可能会话未完整建立）"
        raise RuntimeError(
            "注册后未在 Cookie 中找到 sso"
            + hint
            + (f"。现有 cookie：{names}" if names else "（上下文无任何 cookie）")
            + f"。当前 url={url!r}"
        )

    preview = f"{sso[:12]}…{sso[-8:]}" if len(sso) > 24 else sso
    where = "accounts/account" if _is_accounts_account_page(page.url or "") else (
        "grok.com" if _is_grok_host(page.url or "") else "other"
    )
    print(
        f"[信息] 已采集 SSO Cookie（len={len(sso)} preview={preview} "
        f"where={where} url={page.url!r}）",
        flush=True,
    )
    return sso


def _should_drop_auth_cookie(name: str) -> bool:
    low = (name or "").lower()
    if not low:
        return False
    if low in _AUTH_COOKIE_NAMES:
        return True
    if low.startswith("sso"):
        return True
    if "session" in low:
        return True
    if low.endswith("_token"):
        return True
    # grok / xAI 前端会话相关
    if low.startswith("x-") and low in {
        "x-userid",
        "x-anonuserid",
        "x-challenge",
        "x-signature",
    }:
        return True
    if low.startswith("x-user"):
        return True
    return False


def _clear_auth_cookies(page: Page) -> int:
    """删除 SSO/会话类 Cookie，保留 cf_clearance 等，避免整站风控全丢。"""
    context = page.context
    try:
        # 用汇总列表，覆盖多域名
        cookies = _collect_context_cookies(page)
    except _UI_ERRORS:
        return 0

    keep: list[dict] = []
    removed = 0
    for c in cookies:
        name = str(c.get("name") or "")
        if _should_drop_auth_cookie(name):
            removed += 1
            continue
        keep.append(c)

    if removed == 0:
        return 0
    try:
        context.clear_cookies()
        if keep:
            # Playwright add_cookies 需要 url 或 domain；过滤缺字段的
            valid = []
            for c in keep:
                if c.get("name") is None:
                    continue
                if not c.get("domain") and not c.get("url"):
                    continue
                valid.append(c)
            if valid:
                context.add_cookies(valid)
    except _UI_ERRORS:
        return 0
    return removed


def sign_out_session(page: Page, cfg: Config) -> None:
    """
    对本轮新注册账号执行登出，避免复用浏览器时停在 /account 已登录态。
    优先页面 GET 导航 sign_out_url（grok.com/sign-out）；
    再清理 SSO Cookie；不使用易卡死的 APIRequest 长超时。
    """
    if not cfg.sign_out_enabled:
        return
    url = (cfg.sign_out_url or "").strip()
    if not url:
        return

    timeout = max(3000, int(cfg.sign_out_timeout_ms))
    print(f"[信息] 正在登出：页面 GET {url}（超时 {timeout}ms）", flush=True)

    navigated = False
    try:
        # 先停掉账户页上可能未结束的导航，再 GET 登出
        try:
            page.evaluate("() => { try { window.stop(); } catch (e) {} }")
        except _UI_ERRORS:
            pass
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        navigated = True
        print(f"[信息] 登出导航完成：{page.url}", flush=True)
    except _UI_ERRORS as exc:
        print(f"[警告] 页面导航登出未完全成功：{exc}", flush=True)
        # 短超时再试一次 safe_goto（内部会重试 ERR_ABORTED）
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
            navigated = True
            print(f"[信息] 登出导航（commit）完成：{page.url}", flush=True)
        except _UI_ERRORS as exc2:
            print(f"[警告] 登出导航再次失败：{exc2}", flush=True)

    cur = page.url or ""
    if "__cf_chl" in cur or is_cloudflare_blocked(page):
        print(
            f"[警告] 登出页疑似 Cloudflare 挑战 url={cur!r}；"
            f"仍继续清理本地登录 Cookie",
            flush=True,
        )
        page.wait_for_timeout(max(800, cfg.after_sign_out_ms))

    # 清理 SSO 等登录 Cookie（账户页/会话真正依赖这些）
    if cfg.clear_auth_cookies:
        n = _clear_auth_cookies(page)
        if n:
            print(f"[信息] 已清理登录相关 Cookie 约 {n} 个", flush=True)
        try:
            page.evaluate(
                "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
            )
        except _UI_ERRORS:
            pass
        leftover = extract_sso_cookie(page)
        if leftover:
            n2 = _clear_auth_cookies(page)
            print(
                f"[警告] 登出后仍检测到 sso，已二次清理 Cookie（约 {n2} 个）",
                flush=True,
            )

    page.wait_for_timeout(max(0, cfg.after_sign_out_ms))

    try:
        cur = page.url or ""
        if _is_accounts_account_page(cur) and not navigated:
            print(
                f"[警告] 登出后仍可能处于登录态 url={cur!r}，"
                f"下一轮将强制打开注册页；若仍进账户页请检查 sign_out_url",
                flush=True,
            )
        if extract_sso_cookie(page):
            print(
                "[警告] 登出清理后 Cookie 中仍有 sso，下一轮开始前会再次尝试清理",
                flush=True,
            )
    except _UI_ERRORS:
        pass


def ensure_logged_out_for_signup(page: Page, cfg: Config) -> None:
    """新一轮注册前：若残留 sso/账户页登录态，先清干净。"""
    url = page.url or ""
    has_sso = bool(extract_sso_cookie(page))
    on_account = _is_accounts_account_page(url)
    if not has_sso and not on_account:
        return
    print(
        f"[信息] 检测到残留登录态（sso={has_sso} account_page={on_account}），"
        f"注册前先清理…",
        flush=True,
    )
    if cfg.sign_out_enabled and (cfg.sign_out_url or "").strip():
        try:
            sign_out_session(page, cfg)
        except _UI_ERRORS as exc:
            print(f"[警告] 预清理登出失败：{exc}", flush=True)
    elif cfg.clear_auth_cookies:
        n = _clear_auth_cookies(page)
        if n:
            print(f"[信息] 预清理登录 Cookie 约 {n} 个", flush=True)


def open_signup_and_submit_email(page: Page, cfg: Config, email: str) -> None:
    ensure_logged_out_for_signup(page, cfg)
    safe_goto(page, cfg.signup_url, cfg)
    # 若打开注册页却被重定向到账户页，再清一次
    if _is_accounts_account_page(page.url or "") or extract_sso_cookie(page):
        print(
            f"[警告] 打开注册页后仍像已登录 url={page.url!r}，再次清理并重试…",
            flush=True,
        )
        ensure_logged_out_for_signup(page, cfg)
        safe_goto(page, cfg.signup_url, cfg)
    _assert_not_blocked(page)
    # 多轮复用浏览器时，等注册入口渲染完成再点
    try:
        page.wait_for_load_state(
            "domcontentloaded", timeout=min(8000, cfg.timeout_ms)
        )
    except _UI_ERRORS:
        pass
    page.wait_for_timeout(max(0, min(cfg.between_rounds_ms, 1200)))
    _click_by_texts(
        page,
        [
            "Sign up with email",
            "Continue with email",
            "Use email",
            "Email",
            "使用邮箱注册",
            "使用邮箱",
            "邮箱注册",
        ],
        timeout=cfg.click_timeout_ms,
    )
    _fill_first(page, EMAIL_INPUT, email, timeout=cfg.fill_timeout_ms)
    _click_by_texts(
        page,
        ["Sign up", "Continue", "Next", "注册", "继续", "下一步", "Submit"],
        timeout=cfg.click_timeout_ms,
    )


def _otp_input(page: Page):
    return page.locator(INPUT_OTP).first


def _set_input_otp_value(page: Page, chars: str, cfg: Config) -> bool:
    """向 input-otp 写入验证码并触发 React 受控更新。"""
    loc = _otp_input(page)
    delay = max(0, int(cfg.otp_key_delay_ms))
    try:
        loc.wait_for(state="attached", timeout=min(cfg.fill_timeout_ms, 8000))
    except _UI_ERRORS:
        return False

    try:
        loc.click(force=True, timeout=cfg.click_timeout_ms)
        loc.press("ControlOrMeta+a")
        loc.press("Backspace")
        loc.press_sequentially(chars, delay=delay)
        got = (loc.input_value(timeout=2000) or "").strip()
        if got.upper() == chars.upper() and len(got) == len(chars):
            return True
    except _UI_ERRORS:
        pass

    try:
        ok = loc.evaluate(
            """(el, value) => {
                const proto = window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) {
                    desc.set.call(el, value);
                } else {
                    el.value = value;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                try {
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true, data: value, inputType: 'insertText'
                    }));
                } catch (e) {}
                return (el.value || '') === value;
            }""",
            chars,
        )
        if ok:
            return True
        got = (loc.input_value(timeout=1000) or "").strip()
        if got.upper() == chars.upper():
            return True
    except _UI_ERRORS:
        pass

    try:
        loc.focus()
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.press("Backspace")
        page.keyboard.type(chars, delay=delay)
        got = (loc.input_value(timeout=2000) or "").strip()
        return got.upper() == chars.upper() and len(got) == len(chars)
    except _UI_ERRORS:
        return False


def _fill_otp_fallback(page: Page, chars: str, cfg: Config) -> bool:
    try:
        loc = page.locator(CODE_INPUT_FALLBACK).first
        loc.wait_for(state="visible", timeout=min(cfg.fill_timeout_ms, 3000))
        loc.click()
        loc.fill(chars)
        return True
    except _UI_ERRORS:
        return False


def _page_still_on_verify(page: Page) -> bool:
    for text in VERIFY_PAGE_MARKERS:
        try:
            if page.get_by_text(text, exact=False).count() > 0:
                loc = page.get_by_text(text, exact=False).first
                if loc.is_visible():
                    return True
        except _UI_ERRORS:
            continue
    return False


def _assert_no_otp_validation_error(page: Page) -> None:
    try:
        body = page.locator("body").inner_text(timeout=2000) or ""
    except _UI_ERRORS:
        return
    markers = (
        "Invalid input: expected string, received undefined",
        "expected string, received undefined",
        "Invalid input",
    )
    for m in markers:
        if m in body and _page_still_on_verify(page):
            raise RuntimeError(
                f"邮箱验证码提交失败（页面仍提示：{m}）。"
                f"请确认验证码已正确写入 OTP 输入框（input[data-input-otp]）。"
            )


def _otp_input_value(page: Page) -> str:
    try:
        return (_otp_input(page).input_value(timeout=1000) or "").strip()
    except _UI_ERRORS:
        return ""


def fill_verification_code(page: Page, code: str, cfg: Config) -> None:
    """填写邮箱验证码并确认。"""
    chars = normalize_otp_chars(code)
    print(f"[信息] 写入验证码：{chars}（原始 {code!r}）", flush=True)

    filled = _set_input_otp_value(page, chars, cfg) or _fill_otp_fallback(page, chars, cfg)
    if not filled:
        raise RuntimeError(f"未找到或无法写入邮箱验证码输入框，验证码={code!r}")

    current = _otp_input_value(page)
    print(f"[信息] OTP 输入框当前值：{current!r}", flush=True)
    if current and current.upper() != chars.upper():
        raise RuntimeError(
            f"验证码写入后与预期不一致：期望 {chars!r}，实际 {current!r}"
        )
    if not current:
        print("[警告] 无法读取 OTP input value，仍尝试提交", flush=True)

    page.wait_for_timeout(cfg.after_otp_filled_ms)
    try:
        page.locator('form button[type="submit"]').first.click(timeout=cfg.click_timeout_ms)
    except _UI_ERRORS:
        _click_by_texts(page, CONFIRM_EMAIL_TEXTS, timeout=cfg.click_timeout_ms)

    page.wait_for_timeout(cfg.after_otp_submit_ms)
    _assert_no_otp_validation_error(page)

    deadline_msg = "确认邮箱后未进入完成注册页（仍停在验证邮箱或未出现密码框）"
    try:
        page.locator(PASSWORD_INPUT).first.wait_for(
            state="visible", timeout=cfg.timeout_ms
        )
    except _UI_ERRORS as exc:
        if _page_still_on_verify(page):
            _assert_no_otp_validation_error(page)
            raise RuntimeError(deadline_msg) from exc
        try:
            for text in VERIFY_PAGE_MARKERS:
                page.get_by_text(text, exact=False).first.wait_for(
                    state="hidden", timeout=cfg.timeout_ms
                )
                break
        except _UI_ERRORS as exc2:
            raise RuntimeError(deadline_msg) from exc2


def _password_form_visible(page: Page) -> bool:
    try:
        loc = page.locator(PASSWORD_INPUT).first
        return loc.is_visible()
    except _UI_ERRORS:
        return False


def _wait_complete_form_ready(page: Page, cfg: Config) -> None:
    """等待完成注册表单稳定出现（密码框持续可见）。"""
    page.locator(PASSWORD_INPUT).first.wait_for(
        state="visible", timeout=cfg.fill_timeout_ms
    )
    # 短暂稳定：避免 OTP 后 DOM 重绘导致填完又被清空
    for _ in range(4):
        page.wait_for_timeout(200)
        if not _password_form_visible(page):
            page.locator(PASSWORD_INPUT).first.wait_for(
                state="visible", timeout=cfg.fill_timeout_ms
            )


def _tick_optional_checkboxes(page: Page) -> None:
    """勾选完成注册页可能出现的条款/同意类 checkbox（忽略失败）。"""
    try:
        boxes = page.locator(
            'form input[type="checkbox"], '
            'input[type="checkbox"][name*="agree" i], '
            'input[type="checkbox"][name*="terms" i], '
            'input[type="checkbox"][name*="accept" i]'
        )
        n = min(boxes.count(), 4)
        for i in range(n):
            box = boxes.nth(i)
            try:
                if box.is_visible() and not box.is_checked():
                    box.check(force=True)
            except _UI_ERRORS:
                try:
                    box.click(force=True)
                except _UI_ERRORS:
                    pass
    except _UI_ERRORS:
        pass


def _fill_complete_form_fields(
        page: Page,
        cfg: Config,
        first_name: str,
        last_name: str,
        password: str,
) -> None:
    """填写姓名/密码，并校验写入结果。"""
    # 姓名：优先语义选择器，再回退可见文本输入
    try:
        _fill_first(page, FIRST_NAME_INPUT, first_name, timeout=cfg.fill_timeout_ms)
    except _UI_ERRORS:
        pass
    try:
        _fill_first(
            page, LAST_NAME_INPUT, last_name, timeout=min(cfg.fill_timeout_ms, 8000)
        )
    except _UI_ERRORS:
        pass

    name_inputs = page.locator(
        'input:not([type="password"]):not([type="email"]):not([type="hidden"])'
        ':not([type="checkbox"]):not([type="radio"])'
        ':not([maxlength="1"]):not([maxLength="1"])'
    )
    try:
        count = name_inputs.count()
        filled = []
        for i in range(min(count, 4)):
            try:
                loc = name_inputs.nth(i)
                if not loc.is_visible():
                    continue
                cur = (loc.input_value(timeout=500) or "").strip()
                filled.append(cur)
            except _UI_ERRORS:
                filled.append("")
        # 第 1 个空文本框 -> first，第 2 个 -> last
        empties = [i for i, v in enumerate(filled) if not v]
        if empties:
            try:
                _react_fill(name_inputs.nth(empties[0]), first_name)
            except _UI_ERRORS:
                pass
        if len(empties) >= 2:
            try:
                _react_fill(name_inputs.nth(empties[1]), last_name)
            except _UI_ERRORS:
                pass
        elif len(filled) >= 2 and not filled[1]:
            try:
                _react_fill(name_inputs.nth(1), last_name)
            except _UI_ERRORS:
                pass
    except _UI_ERRORS:
        pass

    # 密码（含确认密码）
    pw_fields = page.locator(PASSWORD_INPUT)
    pw_fields.first.wait_for(state="visible", timeout=cfg.fill_timeout_ms)
    n_pw = pw_fields.count()
    for i in range(min(n_pw, 3)):
        try:
            loc = pw_fields.nth(i)
            if loc.is_visible():
                _react_fill(loc, password)
        except _UI_ERRORS:
            pass

    _tick_optional_checkboxes(page)

    # 校验
    try:
        pw_val = (pw_fields.first.input_value(timeout=1000) or "").strip()
    except _UI_ERRORS:
        pw_val = ""
    if not pw_val:
        raise RuntimeError("密码框写入失败（值为空）")

    # 至少一个姓名有值（有的布局 first/last 选择器对不上）
    name_ok = False
    try:
        for i in range(min(name_inputs.count(), 4)):
            loc = name_inputs.nth(i)
            if not loc.is_visible():
                continue
            if (loc.input_value(timeout=500) or "").strip():
                name_ok = True
                break
    except _UI_ERRORS:
        pass
    if not name_ok:
        # 最后再强写两个常见框
        try:
            _react_fill(name_inputs.nth(0), first_name)
            _react_fill(name_inputs.nth(1), last_name)
            name_ok = True
        except _UI_ERRORS:
            pass
    if not name_ok:
        print("[警告] 未能确认姓名字段已写入，仍尝试提交", flush=True)

    print(
        f"[信息] 完成注册表单已填写：pw_len={len(password)} pw_fields={n_pw} "
        f"url={page.url!r}",
        flush=True,
    )


def _submit_complete_registration(page: Page, cfg: Config) -> None:
    """
    提交完成注册。优先 form[type=submit]，避免点到页面上其它 Sign up/Continue。
    """
    # 1) 主表单 submit
    try:
        btn = page.locator('form button[type="submit"]').first
        if btn.is_visible():
            btn.click(timeout=cfg.click_timeout_ms)
            return
    except _UI_ERRORS:
        pass

    # 2) 明确文案（不含笼统的 Sign up / Continue）
    primary = [
        "Complete registration",
        "Create account",
        "Finish",
        "完成注册",
        "创建账号",
        "完成",
    ]
    try:
        _click_by_texts(page, primary, timeout=cfg.click_timeout_ms)
        return
    except _UI_ERRORS:
        pass

    # 3) 次级文案
    secondary = ["Continue", "继续", "Sign up", "注册"]
    try:
        _click_by_texts(page, secondary, timeout=cfg.click_timeout_ms)
        return
    except _UI_ERRORS:
        pass

    # 4) Enter
    try:
        page.locator(PASSWORD_INPUT).first.press("Enter")
        return
    except _UI_ERRORS:
        pass
    raise RuntimeError("无法点击完成注册提交按钮")


def complete_registration(
        page: Page,
        cfg: Config,
        first_name: str,
        last_name: str,
        password: str,
) -> None:
    if _page_still_on_verify(page):
        raise RuntimeError(
            "仍在「验证您的邮箱」页面，无法填写姓名/密码。请先成功确认邮箱验证码。"
        )

    try:
        _wait_complete_form_ready(page, cfg)
    except _UI_ERRORS as exc:
        raise RuntimeError(
            f"未出现密码输入框，无法完成注册。当前 url={page.url!r}"
        ) from exc

    def _fill_and_submit(*, label: str) -> None:
        print(f"[信息] {label}…", flush=True)
        _fill_complete_form_fields(page, cfg, first_name, last_name, password)
        page.wait_for_timeout(max(100, cfg.after_otp_filled_ms))
        _submit_complete_registration(page, cfg)

    _fill_and_submit(label="填写并提交完成注册表单")

    # 等待成功；必要时整表重填再提交一次
    poll_ms = 300
    total_ms = max(10000, min(int(cfg.timeout_ms), 28000))
    elapsed = 0
    retried = False
    while elapsed <= total_ms:
        if registration_success_signal(page):
            print(f"[信息] 完成注册已生效 url={page.url!r}", flush=True)
            return
        if not _password_form_visible(page) and not _is_signup_or_incomplete_url(
                page.url or ""
        ):
            print(f"[信息] 完成注册表单已离开 url={page.url!r}", flush=True)
            return

        if not retried and elapsed >= 3000 and _password_form_visible(page):
            err = _page_form_error_snippet(page)
            if err:
                print(f"[警告] 页面可能存在校验提示：{err}", flush=True)
            print("[信息] 仍在完成注册表单，重新填写并再次提交…", flush=True)
            try:
                _fill_and_submit(label="重试完成注册")
            except _UI_ERRORS as exc:
                print(f"[警告] 重试提交失败：{exc}", flush=True)
            retried = True

        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms

    err = _page_form_error_snippet(page)
    # 硬失败：避免再空等 settle/sso 几十秒
    raise RuntimeError(
        f"完成注册提交后未进入账户页/未产生 sso，当前 url={page.url!r}"
        + (f"；页面提示：{err}" if err else "")
    )


def warm_profile(cfg: Config) -> None:
    """用自动化专用 Profile 打开注册页，供人手通过 Cloudflare。"""
    user_data = (cfg.user_data_dir or "").strip()
    if not user_data:
        raise RuntimeError(
            "config.toml 中 [signup].user_data_dir 不能为空，请配置自动化专用 Profile 目录"
        )

    profile_dir = Path(user_data).expanduser().resolve()
    print(f"[信息] 自动化专用 Profile：{profile_dir}", flush=True)
    print(f"[信息] 浏览器通道：{cfg.browser_channel or 'chromium'}", flush=True)
    print(f"[信息] 打开：{cfg.signup_url}", flush=True)
    print(
        "[信息] 请在弹出窗口中手动完成 Cloudflare 验证，"
        "直到能看到「创建您的账户」或「使用邮箱注册」。",
        flush=True,
    )
    print("[信息] 完成后回到本终端按回车关闭浏览器（Profile 会自动保存）。", flush=True)

    with sync_playwright() as p:
        context = open_browser_context(p, cfg)
        page = first_page(context)
        page.set_default_timeout(cfg.timeout_ms)
        try:
            safe_goto(page, cfg.signup_url, cfg)
            try:
                input()
            except EOFError:
                page.wait_for_timeout(120_000)
            if is_cloudflare_blocked(page):
                print(
                    "[警告] 关闭时页面仍像是 Cloudflare 拦截页，"
                    "建议重新执行 --warm-profile 直到注册页正常显示。",
                    flush=True,
                )
            else:
                print(f"[完成] 当前页面 title={page.title()!r} url={page.url!r}", flush=True)
        finally:
            close_browser_context(context)


def signup_on_page(
        page: Page,
        cfg: Config,
        email: str,
        code_provider,
        first_name: str,
        last_name: str,
        password: str,
        *,
        sign_out: bool = True,
) -> str:
    """
    在已打开的页面上完成一整轮注册并采集 SSO。
    每轮会重新打开注册页（可复用同一浏览器，无需关闭）。

    时序保证：只有在成功拿到非空 SSO 之后，才执行正式登出。
    返回采集到的 SSO Cookie 值。
    """
    open_signup_and_submit_email(page, cfg, email)
    page.wait_for_timeout(cfg.after_email_submit_ms)
    code = code_provider()
    fill_verification_code(page, code, cfg)
    complete_registration(page, cfg, first_name, last_name, password)
    # 完成注册后站点会自动跳到 grok.com；等待跳转并采集 sso（不要主动 goto）
    settle_page(page, cfg)
    sso = capture_sso_from_grok(page, cfg)
    if not (sso or "").strip():
        raise RuntimeError("未获取到有效 SSO，中止登出与写盘")

    # 拿到 SSO 之后才允许正式登出（清 cookie / sign-out 导航）
    if sign_out:
        print("[信息] 已拿到 SSO，开始正式登出…", flush=True)
        sign_out_session(page, cfg)
        page.wait_for_timeout(max(0, cfg.between_rounds_ms))
    return sso


def run_browser_signup(
        cfg: Config,
        email: str,
        code_provider,
        first_name: str,
        last_name: str,
        password: str,
) -> str:
    """单轮便捷入口：启动浏览器 → 注册取 SSO → 登出 → 关闭。返回 SSO Cookie。"""
    with browser_session(cfg) as page:
        return signup_on_page(
            page, cfg, email, code_provider, first_name, last_name, password
        )


class browser_session:
    """
    浏览器会话上下文管理器：打开一次，yield page，结束时关闭。
    批量循环中复用同一 page，每轮只刷新/打开注册页。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> Page:
        if self.cfg.user_data_dir:
            print(
                f"[信息] 使用持久化 Profile：{Path(self.cfg.user_data_dir).expanduser().resolve()}",
                flush=True,
            )
        self._pw = sync_playwright().start()
        context = open_browser_context(self._pw, self.cfg)
        page = first_page(context)
        page.set_default_timeout(self.cfg.timeout_ms)
        self._context = context
        self._page = page
        return page

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._context is not None:
            close_browser_context(self._context)
            self._context = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None
