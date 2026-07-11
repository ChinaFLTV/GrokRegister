"""纯工具函数：身份生成、验证码提取、CSV 追加。"""

from __future__ import annotations

import csv
import random
import re
import string
import threading
from pathlib import Path

CHARSET = string.ascii_letters + string.digits
# 列顺序：邮箱账号, 密码, SSO, 姓, 名
CSV_COLUMNS = ("邮箱账号", "密码", "SSO", "姓", "名")
# 历史表头（无 SSO 列），追加时会自动迁移
_LEGACY_CSV_COLUMNS = ("邮箱账号", "密码", "姓", "名")
CODE_RE = re.compile(r"^([A-Za-z0-9-]+)\s+xAI confirmation code", re.I)

# 进程内 CSV 追加互斥（并发注册）
_csv_lock = threading.Lock()


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


def random_password(length: int = 14) -> str:
    # 至少包含大写、小写、数字、特殊字符，降低偶发密码策略失败
    specials = "!@#$%&*"
    parts = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice(specials),
    ]
    pool = CHARSET + specials
    parts.extend(random.choices(pool, k=max(0, length - len(parts))))
    random.shuffle(parts)
    return "".join(parts)


def _migrate_legacy_csv_if_needed(csv_path: Path) -> None:
    """若已有无 SSO 列的旧 CSV，原地改写为新表头（旧行 SSO 置空）。"""
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    header = tuple(rows[0])
    if header == CSV_COLUMNS:
        return
    if header != _LEGACY_CSV_COLUMNS:
        return
    migrated = [list(CSV_COLUMNS)]
    for row in rows[1:]:
        # 旧: 邮箱, 密码, 姓, 名  →  新: 邮箱, 密码, SSO, 姓, 名
        email = row[0] if len(row) > 0 else ""
        password = row[1] if len(row) > 1 else ""
        last_name = row[2] if len(row) > 2 else ""
        first_name = row[3] if len(row) > 3 else ""
        migrated.append([email, password, "", last_name, first_name])
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(migrated)
        f.flush()


def append_account_csv(
    path: str | Path,
    email: str,
    password: str,
    sso: str,
    last_name: str,
    first_name: str,
) -> None:
    """线程安全追加一行账号（带锁，避免并发写坏 CSV）。列：邮箱,密码,SSO,姓,名。"""
    csv_path = Path(path)
    with _csv_lock:
        _migrate_legacy_csv_if_needed(csv_path)
        write_header = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(CSV_COLUMNS)
            writer.writerow([email, password, sso, last_name, first_name])
            f.flush()
