"""Playwright 页面操作：注册 → 验证邮箱 → 完成注册。"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from config import Config
from helpers import normalize_otp_chars

# 页面定位器（站点文案可能是英文或中文，按钮匹配串需保留双语）
EMAIL_INPUT = 'input[type="email"], input[name="email"], input[autocomplete="email"]'
# xAI 使用 input-otp：可见格是 div[data-input-otp-slot]，真输入是单个透明 input
# 例如：<input data-input-otp name="code" maxlength="6" autocomplete="one-time-code">
INPUT_OTP = (
    'input[data-input-otp="true"], '
    'input[name="code"][data-input-otp], '
    'input[name="code"][maxlength="6"], '
    'input[name="code"][autocomplete="one-time-code"]'
)
# 其它站点单框回退
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

# 降低自动化特征（效果因站点而异，不能保证过盾）
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
    # 挂到 context 上，关闭时一并关掉 browser
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


def _fill_first(page: Page, selector: str, value: str, timeout: float = 15000) -> None:
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=timeout)
    loc.fill(value)


def _click_by_texts(page: Page, texts: list[str], role: str = "button") -> None:
    for text in texts:
        try:
            page.get_by_role(role, name=text, exact=False).first.click(timeout=5000)
            return
        except Exception:
            continue
    # 回退：按可见文本点击
    for text in texts:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=5000)
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


def open_signup_and_submit_email(page: Page, cfg: Config, email: str) -> None:
    page.goto(cfg.signup_url, wait_until="domcontentloaded")
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
    )
    _fill_first(page, EMAIL_INPUT, email)
    _click_by_texts(
        page,
        ["Sign up", "Continue", "Next", "注册", "继续", "下一步", "Submit"],
    )


def _otp_input(page: Page):
    """xAI input-otp 真输入框（透明叠层，pointer-events:all）。"""
    return page.locator(INPUT_OTP).first


def _set_input_otp_value(page: Page, chars: str) -> bool:
    """
    向 input-otp 写入验证码并触发 React 受控更新。
    仅用 fill() 往往改不了库内部 state，提交仍是 undefined。
    """
    loc = _otp_input(page)
    try:
        loc.wait_for(state="attached", timeout=8000)
    except Exception:
        return False

    # 1) 聚焦 + 清空 + 逐字键入（最贴近真实输入，input-otp 会同步 slot）
    try:
        loc.click(force=True, timeout=5000)
        loc.press("ControlOrMeta+a")
        loc.press("Backspace")
        # press_sequentially 会发 keydown/keypress/input，比 fill 可靠
        loc.press_sequentially(chars, delay=60)
        got = (loc.input_value(timeout=2000) or "").strip()
        if got.upper() == chars.upper() and len(got) == len(chars):
            return True
    except Exception:
        pass

    # 2) 原生 value setter + input/change（绕过 React 合成事件缺口）
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
                // input-otp 有时监听 InputEvent
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

    # 3) 点容器区域后用 page.keyboard（容器 pointer-events:none，仍尝试点 input）
    try:
        loc.focus()
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.press("Backspace")
        page.keyboard.type(chars, delay=60)
        got = (loc.input_value(timeout=2000) or "").strip()
        return got.upper() == chars.upper() and len(got) == len(chars)
    except Exception:
        return False


def _fill_otp_fallback(page: Page, chars: str) -> bool:
    """非 input-otp 的普通验证码框。"""
    try:
        loc = page.locator(CODE_INPUT_FALLBACK).first
        loc.wait_for(state="visible", timeout=3000)
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


def fill_verification_code(page: Page, code: str, timeout_ms: int = 30000) -> None:
    """
    填写邮箱验证码并确认。
    xAI：可见 6 格为 div slot，真正可编辑的是 input[data-input-otp][name=code]，
    需写入无分隔符 6 位（如 K1J-PGT → K1JPGT），并触发键盘/React 事件。
    """
    chars = normalize_otp_chars(code)
    print(f"[信息] 写入验证码：{chars}（原始 {code!r}）", flush=True)

    filled = _set_input_otp_value(page, chars) or _fill_otp_fallback(page, chars)
    if not filled:
        raise RuntimeError(
            f"未找到或无法写入邮箱验证码输入框，验证码={code!r}"
        )

    current = _otp_input_value(page)
    print(f"[信息] OTP 输入框当前值：{current!r}", flush=True)
    if current and current.upper() != chars.upper():
        raise RuntimeError(
            f"验证码写入后与预期不一致：期望 {chars!r}，实际 {current!r}"
        )
    if not current:
        # 部分实现 input_value 读不到但 React state 已更新；仍尝试提交
        print("[警告] 无法读取 OTP input value，仍尝试提交", flush=True)

    page.wait_for_timeout(200)
    # 优先点 type=submit 的「确认邮箱」，避免点到错误按钮
    try:
        page.locator('form button[type="submit"]').first.click(timeout=5000)
    except Exception:
        _click_by_texts(page, CONFIRM_EMAIL_TEXTS)

    page.wait_for_timeout(1000)
    _assert_no_otp_validation_error(page)

    # 必须离开「验证您的邮箱」页后再进入完成注册
    deadline_msg = "确认邮箱后未进入完成注册页（仍停在验证邮箱或未出现密码框）"
    try:
        page.locator(PASSWORD_INPUT).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        if _page_still_on_verify(page):
            _assert_no_otp_validation_error(page)
            raise RuntimeError(deadline_msg) from exc
        try:
            for text in VERIFY_PAGE_MARKERS:
                page.get_by_text(text, exact=False).first.wait_for(
                    state="hidden", timeout=timeout_ms
                )
                break
        except Exception as exc2:
            raise RuntimeError(deadline_msg) from exc2


def complete_registration(
    page: Page,
    first_name: str,
    last_name: str,
    password: str,
) -> None:
    if _page_still_on_verify(page):
        raise RuntimeError(
            "仍在「验证您的邮箱」页面，无法填写姓名/密码。请先成功确认邮箱验证码。"
        )

    # 优先填写明确的姓名字段
    try:
        _fill_first(page, FIRST_NAME_INPUT, first_name, timeout=20000)
    except Exception:
        pass
    try:
        _fill_first(page, LAST_NAME_INPUT, last_name, timeout=8000)
    except Exception:
        pass

    # 部分页面 名/姓 标签对调：补填仍为空的文本框
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

    _fill_first(page, PASSWORD_INPUT, password)
    # 若存在确认密码框则一并填写
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
    )

def warm_profile(cfg: Config) -> None:
    """
    用自动化专用 Profile 打开注册页，供人手通过 Cloudflare。
    通过后 cookie 会写回 user_data_dir，后续 register 复用同一目录。
    """
    user_data = (cfg.user_data_dir or "").strip()
    if not user_data:
        raise RuntimeError("config.toml 中 [signup].user_data_dir 不能为空，请配置自动化专用 Profile 目录")

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
            page.goto(cfg.signup_url, wait_until="domcontentloaded")
            try:
                input()
            except EOFError:
                # 非交互环境：多等一会儿再关
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


def run_browser_signup(
    cfg: Config,
    email: str,
    code_provider,
    first_name: str,
    last_name: str,
    password: str,
) -> None:
    """
    驱动一整轮浏览器注册。
    code_provider：无参可调用对象，返回邮箱验证码（内部轮询邮件等）。
    """
    if cfg.user_data_dir:
        print(
            f"[信息] 使用持久化 Profile：{Path(cfg.user_data_dir).expanduser().resolve()}",
            flush=True,
        )

    with sync_playwright() as p:
        context = open_browser_context(p, cfg)
        page = first_page(context)
        page.set_default_timeout(cfg.timeout_ms)
        try:
            open_signup_and_submit_email(page, cfg, email)
            # 稍等验证邮箱页渲染
            page.wait_for_timeout(1500)
            code = code_provider()
            fill_verification_code(page, code, timeout_ms=cfg.timeout_ms)
            complete_registration(page, first_name, last_name, password)
            page.wait_for_timeout(2000)
        finally:
            close_browser_context(context)
