"""共享工具：时间契约、退出码、哈希、脱敏、原子写、子进程。

时间契约（与 ark-node 完全一致，全仓库唯一口径）
------------------------------------------------
* 对外展示：北京时间，**无时区后缀**，``YYYY-MM-DD HH:MM:SS``
* 机器可读：RFC3339，``YYYY-MM-DDTHH:MM:SS+0800``
* 文件名：``YYYYMMDD-HHMMSS``

三种形态都从同一个北京时间来源推导，避免节点本地时区不同导致台账对不上。

脱敏铁律
--------
本仓库只出现凭据**变量名**，永远不出现值。任何要写进日志、JSON、异常信息的
自由文本都必须先过 :func:`redact`。
"""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------- 时间

BEIJING = timezone(timedelta(hours=8))

FMT_DISPLAY = "%Y-%m-%d %H:%M:%S"
FMT_RFC3339 = "%Y-%m-%dT%H:%M:%S%z"
FMT_STAMP = "%Y%m%d-%H%M%S"

# --------------------------------------------------------------------- 退出码

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE = 3
EXIT_FORBIDDEN = 4
EXIT_NETWORK = 5
EXIT_NEEDS_ELEVATION = 6

EXIT_CODES = {
    0: "OK",
    1: "FAIL",
    2: "USAGE",
    3: "NO_SOURCE",
    4: "FORBIDDEN",
    5: "NETWORK",
    6: "NEEDS_ELEVATION",
}

MIB = 1024 * 1024


def now_beijing() -> datetime:
    """当前时间，固定按北京时间换算，不依赖节点本地时区。"""
    return datetime.now(timezone.utc).astimezone(BEIJING)


def _coerce(value: Optional[datetime]) -> datetime:
    if value is None:
        return now_beijing()
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def format_display(dt: Optional[datetime] = None) -> str:
    """对外展示格式：``YYYY-MM-DD HH:MM:SS``（北京时间，无后缀）。"""
    return _coerce(dt).strftime(FMT_DISPLAY)


def format_rfc3339(dt: Optional[datetime] = None) -> str:
    """机器可读格式：``YYYY-MM-DDTHH:MM:SS+0800``。"""
    return _coerce(dt).strftime(FMT_RFC3339)


def format_stamp(dt: Optional[datetime] = None) -> str:
    """文件名时间戳：``YYYYMMDD-HHMMSS``。"""
    return _coerce(dt).strftime(FMT_STAMP)


def parse_time(text: str) -> datetime:
    """解析时间，接受展示格式与 RFC3339，返回带时区的 datetime。

    展示格式按北京时间解释；RFC3339 允许 ``Z`` 后缀与小数秒。
    无法解析时抛 :class:`ValueError`。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("空时间字符串")
    try:
        return datetime.strptime(raw, FMT_DISPLAY).replace(tzinfo=BEIJING)
    except ValueError:
        pass
    candidate = raw
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+0000"
    try:
        return datetime.strptime(candidate, FMT_RFC3339).astimezone(BEIJING)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError("无法解析时间: {0!r}".format(raw))
    return _coerce(parsed)


# --------------------------------------------------------------------- 哈希 / 体积


def sha256_file(path: Path, chunk: int = MIB) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human_bytes(n: Any) -> str:
    try:
        value = float(n or 0)
    except (TypeError, ValueError):
        value = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return "{0:.0f}{1}".format(value, unit)
            return "{0:.1f}{1}".format(value, unit)
        value /= 1024.0
    return "{0:.1f}TB".format(value)


# --------------------------------------------------------------------- 脱敏

#: 变量名里出现这些片段即视为凭据变量（只用于识别名字，不涉及值）
SECRET_NAME_MARKERS = (
    "PASSWORD",
    "PASSWD",
    "PASS",
    "TOKEN",
    "SECRET",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
)

#: 与凭据无关但容易被误判的变量名（白名单）
SECRET_NAME_EXEMPT = (
    "PASSIVE",
    "BYPASS",
    "COMPASS",
)

#: 常见凭据形态。这里是"形状"而不是真实值；命中即整体替换为 ``***``。
_SECRET_SHAPES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bLTAI[A-Za-z0-9]{12,}\b"),
)

#: 值长度下限：太短的值（如 ``1``、``yes``）替换会误伤正常文本
MIN_SECRET_LEN = 6


def is_secret_env_name(name: str) -> bool:
    upper = str(name or "").upper()
    for exempt in SECRET_NAME_EXEMPT:
        if exempt in upper:
            return False
    return any(marker in upper for marker in SECRET_NAME_MARKERS)


def secret_env_values() -> List[str]:
    """当前进程环境中的凭据值（仅用于替换，调用方不得打印）。"""
    values = []
    for name, value in os.environ.items():
        if value and len(value) >= MIN_SECRET_LEN and is_secret_env_name(name):
            values.append(value)
    values.sort(key=len, reverse=True)
    return values


def redact(value: Any) -> str:
    """把已知凭据值替换为 ``***``，再抹掉常见凭据形态。"""
    text = "" if value is None else str(value)
    for secret in secret_env_values():
        if secret in text:
            text = text.replace(secret, "***")
    for pattern in _SECRET_SHAPES:
        text = pattern.sub("***", text)
    return text


def redact_obj(value: Any) -> Any:
    """递归脱敏：字符串过 :func:`redact`，字节转 ``<bytes>``。"""
    if isinstance(value, dict):
        return {redact(key): redact_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    if isinstance(value, bytes):
        return "<bytes>"
    if isinstance(value, str):
        return redact(value)
    return value


# --------------------------------------------------------------------- 文件


def read_json(path: Any) -> Any:
    with open(str(path), "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_write(path: Any, data: str, mode: int = 0o644) -> Path:
    """原子写：同目录临时文件 + ``os.replace``，避免半写文件。"""
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".asset-tools-")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, str(target))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return target


def atomic_write_json(path: Any, obj: Any, mode: int = 0o644) -> Path:
    payload = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    return atomic_write(path, payload, mode=mode)


def dumps(obj: Any) -> str:
    """稳定的 JSON 文本（UTF-8、中文不转义、两空格缩进、末尾换行）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def ensure_dir(path: Any) -> Path:
    target = Path(str(path)).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def hostname() -> str:
    return str(os.environ.get("ARK_NODE_NAME") or socket.gethostname() or "unknown").lower()


# --------------------------------------------------------------------- 路径安全


class PathForbidden(Exception):
    """路径越界 / 穿越，CLI 映射为 ``EXIT_FORBIDDEN``。"""


def safe_filename(name: Any) -> str:
    """校验"纯文件名"：不允许分隔符、绝对路径、``..`` 或 NUL。"""
    text = str(name or "")
    if not text or text in (".", ".."):
        raise PathForbidden("非法文件名: {0!r}".format(text))
    if "\x00" in text:
        raise PathForbidden("文件名包含 NUL 字节")
    if "/" in text or "\\" in text:
        raise PathForbidden("文件名不得包含路径分隔符: {0!r}".format(text))
    if os.path.isabs(text) or Path(text).is_absolute() or re.match(r"^[A-Za-z]:", text):
        raise PathForbidden("文件名不得是绝对路径: {0!r}".format(text))
    return text


def safe_rel_path(rel: Any) -> str:
    """校验"相对路径"：不得以 ``..`` 逃逸、不得绝对、不得含冒号盘符。

    返回 POSIX 风格的相对路径字符串。
    """
    text = str(rel or "").replace("\\", "/").strip()
    if not text:
        raise PathForbidden("空相对路径")
    if "\x00" in text:
        raise PathForbidden("相对路径包含 NUL 字节")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise PathForbidden("相对路径不得是绝对路径: {0!r}".format(text))
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise PathForbidden("空相对路径")
    for part in parts:
        if part == "..":
            raise PathForbidden("相对路径不得包含 '..': {0!r}".format(text))
    return "/".join(parts)


def is_within(path: Any, root: Any) -> bool:
    """判断 ``path`` 是否在 ``root`` 之下（两者都做 resolve）。"""
    try:
        Path(str(path)).resolve().relative_to(Path(str(root)).resolve())
        return True
    except (ValueError, OSError):
        return False


#: 禁止进入资产链路的凭据文件名（SPEC 7：凭据文件名 → FORBIDDEN）
CREDENTIAL_FILENAMES = (
    ".git-credentials",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.pem",
    "*.pfx",
    "auth.json",
)


def is_credential_filename(name: Any) -> bool:
    base = Path(str(name or "")).name.lower()
    return any(fnmatch.fnmatch(base, pat.lower()) for pat in CREDENTIAL_FILENAMES)


def slugify(text: Any, fallback: str = "asset") -> str:
    raw = str(text or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or fallback


def rel_display(path: Any) -> str:
    """日志里优先给仓库相对路径，避免把本机绝对路径写进公开台账。"""
    target = Path(str(path)).resolve()
    try:
        return str(target.relative_to(repo_root()))
    except ValueError:
        return str(target)


# --------------------------------------------------------------------- 进程


def which(name: Any) -> Optional[str]:
    target = str(name or "").strip()
    if not target:
        return None
    found = shutil.which(target)
    return found or None


def resolve_cli(candidate: Any, default_name: Optional[str] = None) -> Optional[str]:
    """解析委托 CLI：显式路径优先，其次 PATH；找不到返回 ``None``。"""
    raw = str(candidate or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_absolute() or os.sep in raw or (os.altsep and os.altsep in raw):
            if path.is_file() and os.access(str(path), os.X_OK):
                return str(path)
            return None
        return which(raw)
    if default_name:
        return which(default_name)
    return None


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def run_cmd(
    cmd: Iterable[Any],
    timeout: float = 30.0,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """执行命令，返回 ``(rc, stdout, stderr)``。

    绝不使用 ``shell=True``；超时返回 124、缺可执行文件返回 127。
    输出按 UTF-8/GBK 宽松解码（Windows 目标机常见 CP936 不再崩）。
    """
    argv = [str(part) for part in cmd]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=env if env is not None else os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after {0:.0f}s".format(timeout)
    except FileNotFoundError as exc:
        return 127, "", "missing binary: {0}".format(exc)
    except OSError as exc:
        return 126, "", "exec error: {0}".format(exc)
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------- 输出


def emit(obj: Any, stream: Any = None) -> None:
    """输出 JSON（stdout 只留 JSON，错误走 stderr）。"""
    target = stream or sys.stdout
    json.dump(obj, target, ensure_ascii=False, indent=2)
    target.write("\n")


def info(message: str) -> None:
    sys.stderr.write(redact(message) + "\n")


def exit_name(code: int) -> str:
    return EXIT_CODES.get(int(code), "FAIL")
