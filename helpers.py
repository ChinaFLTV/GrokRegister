"""纯工具函数：身份生成、验证码提取、CSV 追加。"""

from __future__ import annotations

import csv
import random
import re
import string
from pathlib import Path

CHARSET = string.ascii_letters + string.digits
CSV_COLUMNS = ("邮箱账号", "密码", "姓", "名")
CODE_RE = re.compile(r"^([A-Za-z0-9-]+)\s+xAI confirmation code", re.I)


def generate_local_part(length: int = 8) -> str:
    if length < 1:
        raise ValueError("邮箱本地部分长度必须 >= 1")
    return "".join(random.choices(CHARSET, k=length))


def build_email(domain: str, length: int = 8, local_part: str | None = None) -> str:
    local = local_part if local_part is not None else generate_local_part(length)
    return f"{local}@{domain}"


def extract_confirmation_code(subject: str) -> str:
    match = CODE_RE.match(subject.strip())
    if not match:
        raise ValueError(f"无法从邮件主题提取验证码：{subject!r}")
    return match.group(1)


def normalize_otp_chars(code: str) -> str:
    """去掉横线等分隔符，得到写入 OTP 分格的纯字符，如 X9G-M86 → X9GM86。"""
    chars = re.sub(r"[^A-Za-z0-9]", "", (code or "").strip())
    if not chars:
        raise ValueError(f"验证码为空或无效：{code!r}")
    return chars


def random_name(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=length)).capitalize()


def random_password(length: int = 12) -> str:
    # 至少包含一个大写、一个小写、一个数字，满足常见密码规则
    parts = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
    ]
    parts.extend(random.choices(CHARSET, k=max(0, length - len(parts))))
    random.shuffle(parts)
    return "".join(parts)


def append_account_csv(
    path: str | Path,
    email: str,
    password: str,
    last_name: str,
    first_name: str,
) -> None:
    csv_path = Path(path)
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_COLUMNS)
        writer.writerow([email, password, last_name, first_name])
