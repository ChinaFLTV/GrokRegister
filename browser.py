"""Playwright 页面操作：注册 → 验证邮箱 → 完成注册。"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from config import Config
from helpers import normalize_otp_chars

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
    browser = getattr(context, "_owned_browser", None)
    context.close()
    if browser is not None:
        browser.close()


def first_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return context.new_page()


def _fill_first(page: Page, selector: str, value: str, timeout: float) -> None:
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=timeout)
    loc.fill(value)


def _click_by_texts(
    page: Page,
    texts: list[str],
    role: str = "button",
    timeout: float = 5000,
) -> None:
    for text in texts:
        try:
            page.get_by_role(role, name=text, exact=False).first.click(timeout=timeout)
            return
        except Exception:
            continue
    for text in texts:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=timeout)
            return
        except Exception:
            continue
    raise RuntimeError(f"未找到匹配控件，候选文案：{texts}")


def is_cloudflare_blocked(page: Page) -> bool:
    title = (page.title() or "").lower()
    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
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
            except Exception:
                pass
            page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
            return
        except Exception as exc:
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
    }
)


def settle_page(page: Page, cfg: Config) -> None:
    """完成注册后等待跳转到账户页（或页面稳定），再进行登出。"""
    # 成功后常会跳到 https://accounts.x.ai/account
    try:
        page.wait_for_url("**/account**", timeout=min(12000, cfg.timeout_ms))
        print(f"[信息] 已进入账户页：{page.url}", flush=True)
    except Exception:
        try:
            page.wait_for_load_state(
                "domcontentloaded", timeout=min(5000, cfg.timeout_ms)
            )
        except Exception:
            pass
    page.wait_for_timeout(max(0, cfg.after_complete_ms))


def _clear_auth_cookies(page: Page) -> int:
    """删除 SSO/会话类 Cookie，保留 cf_clearance 等，避免整站风控全丢。"""
    context = page.context
    try:
        cookies = context.cookies()
    except Exception:
        return 0

    keep: list[dict] = []
    removed = 0
    for c in cookies:
        name = str(c.get("name") or "")
        low = name.lower()
        drop = (
            low in _AUTH_COOKIE_NAMES
            or low.startswith("sso")
            or "session" in low
            or low.endswith("_token")
            or low in {"jwt", "sid"}
        )
        if drop:
            removed += 1
            continue
        keep.append(c)

    if removed == 0:
        return 0
    try:
        context.clear_cookies()
        if keep:
            context.add_cookies(keep)
    except Exception:
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
        except Exception:
            pass
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        navigated = True
        print(f"[信息] 登出导航完成：{page.url}", flush=True)
    except Exception as exc:
        print(f"[警告] 页面导航登出未完全成功：{exc}", flush=True)
        # 短超时再试一次 safe_goto（内部会重试 ERR_ABORTED）
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
            navigated = True
            print(f"[信息] 登出导航（commit）完成：{page.url}", flush=True)
        except Exception as exc2:
            print(f"[警告] 登出导航再次失败：{exc2}", flush=True)

    # 清理 SSO 等登录 Cookie（账户页/会话真正依赖这些）
    if cfg.clear_auth_cookies:
        n = _clear_auth_cookies(page)
        if n:
            print(f"[信息] 已清理登录相关 Cookie 约 {n} 个", flush=True)
        try:
            page.evaluate(
                "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
            )
        except Exception:
            pass

    page.wait_for_timeout(max(0, cfg.after_sign_out_ms))

    # 若仍像在账户页，再提示（不中断主流程，下一轮 safe_goto 注册页）
    try:
        cur = page.url or ""
        if "/account" in cur and navigated is False:
            print(
                f"[警告] 登出后仍可能处于登录态 url={cur!r}，"
                f"下一轮将强制打开注册页；若仍进账户页请检查 sign_out_url",
                flush=True,
            )
    except Exception:
        pass


def open_signup_and_submit_email(page: Page, cfg: Config, email: str) -> None:
    safe_goto(page, cfg.signup_url, cfg)
    _assert_not_blocked(page)
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
    except Exception:
        return False

    try:
        loc.click(force=True, timeout=cfg.click_timeout_ms)
        loc.press("ControlOrMeta+a")
        loc.press("Backspace")
        loc.press_sequentially(chars, delay=delay)
        got = (loc.input_value(timeout=2000) or "").strip()
        if got.upper() == chars.upper() and len(got) == len(chars):
            return True
    except Exception:
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
    except Exception:
        pass

    try:
        loc.focus()
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.press("Backspace")
        page.keyboard.type(chars, delay=delay)
        got = (loc.input_value(timeout=2000) or "").strip()
        return got.upper() == chars.upper() and len(got) == len(chars)
    except Exception:
        return False


def _fill_otp_fallback(page: Page, chars: str, cfg: Config) -> bool:
    try:
        loc = page.locator(CODE_INPUT_FALLBACK).first
        loc.wait_for(state="visible", timeout=min(cfg.fill_timeout_ms, 3000))
        loc.click()
        loc.fill(chars)
        return True
    except Exception:
        return False


def _page_still_on_verify(page: Page) -> bool:
    for text in VERIFY_PAGE_MARKERS:
        try:
            if page.get_by_text(text, exact=False).count() > 0:
                loc = page.get_by_text(text, exact=False).first
                if loc.is_visible():
                    return True
        except Exception:
            continue
    return False


def _assert_no_otp_validation_error(page: Page) -> None:
    try:
        body = page.locator("body").inner_text(timeout=2000) or ""
    except Exception:
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
    except Exception:
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
    except Exception:
        _click_by_texts(page, CONFIRM_EMAIL_TEXTS, timeout=cfg.click_timeout_ms)

    page.wait_for_timeout(cfg.after_otp_submit_ms)
    _assert_no_otp_validation_error(page)

    deadline_msg = "确认邮箱后未进入完成注册页（仍停在验证邮箱或未出现密码框）"
    try:
        page.locator(PASSWORD_INPUT).first.wait_for(
            state="visible", timeout=cfg.timeout_ms
        )
    except Exception as exc:
        if _page_still_on_verify(page):
            _assert_no_otp_validation_error(page)
            raise RuntimeError(deadline_msg) from exc
        try:
            for text in VERIFY_PAGE_MARKERS:
                page.get_by_text(text, exact=False).first.wait_for(
                    state="hidden", timeout=cfg.timeout_ms
                )
                break
        except Exception as exc2:
            raise RuntimeError(deadline_msg) from exc2


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
        _fill_first(page, FIRST_NAME_INPUT, first_name, timeout=cfg.fill_timeout_ms)
    except Exception:
        pass
    try:
        _fill_first(page, LAST_NAME_INPUT, last_name, timeout=min(cfg.fill_timeout_ms, 8000))
    except Exception:
        pass

    name_inputs = page.locator(
        'input:not([type="password"]):not([type="email"]):not([type="hidden"])'
        ':not([maxlength="1"]):not([maxLength="1"])'
    )
    try:
        count = name_inputs.count()
        if count >= 1 and not (name_inputs.nth(0).input_value() or "").strip():
            name_inputs.nth(0).fill(first_name)
        if count >= 2 and not (name_inputs.nth(1).input_value() or "").strip():
            name_inputs.nth(1).fill(last_name)
    except Exception:
        pass

    _fill_first(page, PASSWORD_INPUT, password, timeout=cfg.fill_timeout_ms)
    pw_fields = page.locator(PASSWORD_INPUT)
    if pw_fields.count() >= 2:
        pw_fields.nth(1).fill(password)

    _click_by_texts(
        page,
        [
            "Complete registration",
            "Create account",
            "Sign up",
            "Finish",
            "Continue",
            "完成注册",
            "创建账号",
            "完成",
            "继续",
        ],
        timeout=cfg.click_timeout_ms,
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
) -> None:
    """
    在已打开的页面上完成一整轮注册。
    每轮会重新打开注册页（可复用同一浏览器，无需关闭）。
    """
    open_signup_and_submit_email(page, cfg, email)
    page.wait_for_timeout(cfg.after_email_submit_ms)
    code = code_provider()
    fill_verification_code(page, code, cfg)
    complete_registration(page, cfg, first_name, last_name, password)
    # 等完成注册后的跳转收尾
    settle_page(page, cfg)
    # 登出本轮账号，避免复用浏览器时下一轮已登录
    sign_out_session(page, cfg)
    page.wait_for_timeout(max(0, cfg.between_rounds_ms))


def run_browser_signup(
    cfg: Config,
    email: str,
    code_provider,
    first_name: str,
    last_name: str,
    password: str,
) -> None:
    """单轮便捷入口：启动浏览器 → 注册 → 关闭。"""
    with browser_session(cfg) as page:
        signup_on_page(page, cfg, email, code_provider, first_name, last_name, password)


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
        self._context = open_browser_context(self._pw, self.cfg)
        self._page = first_page(self._context)
        self._page.set_default_timeout(self.cfg.timeout_ms)
        return self._page

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            close_browser_context(self._context)
            self._context = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None
