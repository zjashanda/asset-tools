"""适配器基类与共享契约。

硬性契约（SPEC 5）
------------------
* :meth:`Adapter.probe` **绝不抛异常**；缺凭据/缺 CLI 一律返回
  ``available=False, reason="BLOCKED:ADAPTER_UNAVAILABLE"``。
* :meth:`Adapter.discover` 不可用时返回 ``[]``，绝不崩溃。
* :meth:`Adapter.fetch` 必须校验 sha256，不匹配返回
  ``ok=False, reason="SHA256_MISMATCH", exit_code=EXIT_FAIL``。
* 所有文本写入前都过 :func:`asset_tools.util.redact`。
* 凭据只以环境变量**名字**出现在 ``missing_env`` 里。
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import util

KIND_LOCAL = "local"
KIND_HTTP = "http"
KIND_OSS = "oss"
KIND_NODE_STORAGE = "node-storage"
KIND_EMAIL = "email"
KIND_NAS = "nas"

KIND_NAMES = (KIND_LOCAL, KIND_HTTP, KIND_OSS, KIND_NODE_STORAGE, KIND_EMAIL, KIND_NAS)

BLOCKED = "BLOCKED:ADAPTER_UNAVAILABLE"
OK = "OK"

DEFAULT_PRIORITY = 100
DEFAULT_ADAPTER_TIMEOUT = 120.0
DEFAULT_HTTP_TIMEOUT = 30.0


@dataclass
class AdapterStatus:
    name: str
    available: bool
    reason: str
    detail: str = ""
    missing_env: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "reason": self.reason,
            "detail": util.redact(self.detail),
            "missing_env": list(self.missing_env),
        }


@dataclass
class AssetRef:
    """适配器返回的候选资产引用。

    ``ref`` 的语义随类型变化：本地路径 / URL / object key / 邮件附件 id / NAS 路径。
    """

    type: str
    filename: str
    ref: str
    sha256: str = ""
    bytes: int = 0
    priority: int = DEFAULT_PRIORITY
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "filename": self.filename,
            "ref": util.redact(self.ref),
            "sha256": self.sha256,
            "bytes": self.bytes,
            "priority": self.priority,
            "extra": util.redact_obj(self.extra),
        }


@dataclass
class FetchResult:
    ok: bool
    path: str = ""
    sha256: str = ""
    bytes: int = 0
    reason: str = "OK"
    detail: str = ""
    exit_code: int = util.EXIT_OK

    @classmethod
    def failure(cls, reason: str, exit_code: int, detail: str = "") -> "FetchResult":
        return cls(ok=False, reason=reason, exit_code=exit_code, detail=util.redact(detail))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "path": util.redact(self.path),
            "sha256": self.sha256,
            "bytes": self.bytes,
            "reason": self.reason,
            "detail": util.redact(self.detail),
            "exit_code": self.exit_code,
        }


class Adapter:
    """所有适配器的公共骨架。"""

    name = "base"
    kind = KIND_LOCAL

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = dict(config or {})
        self.status: Optional[AdapterStatus] = None
        self.last_error: str = ""

    # ---------------------------------------------------------------- 生命周期

    def probe(self) -> AdapterStatus:
        raise NotImplementedError

    def discover(self, query: Dict[str, Any]) -> List[AssetRef]:
        return []

    def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult:
        return FetchResult.failure("NOT_IMPLEMENTED", util.EXIT_FAIL, "基类不实现 fetch")

    def _probe(self) -> AdapterStatus:
        """带缓存的 probe；即使子类写错也绝不把异常抛给调用方。"""
        if self.status is None:
            try:
                status = self.probe()
            except Exception as exc:  # noqa: BLE001 - 契约要求 probe 永不抛异常
                status = AdapterStatus(
                    name=self.name,
                    available=False,
                    reason=BLOCKED,
                    detail=util.redact("probe error: {0}".format(exc)),
                )
            self.status = status
        return self.status

    def available(self) -> bool:
        return self._probe().available

    def blocked_result(self, detail: str = "") -> FetchResult:
        status = self._probe()
        message = detail or status.detail or "适配器不可用"
        return FetchResult.failure(BLOCKED, util.EXIT_NO_SOURCE, util.redact(message))

    # ---------------------------------------------------------------- 配置

    def config_value(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key)
        return default if value in (None, "") else value

    def timeout(self) -> float:
        return util.env_float("ASSET_ADAPTER_TIMEOUT", DEFAULT_ADAPTER_TIMEOUT)


# --------------------------------------------------------------------- 共享辅助


def ok_status(name: str, detail: str = "") -> AdapterStatus:
    return AdapterStatus(name=name, available=True, reason=OK, detail=detail)


def blocked_status(
    name: str, missing_env: Optional[List[str]] = None, detail: str = ""
) -> AdapterStatus:
    names = [str(item) for item in (missing_env or [])]
    if not detail:
        detail = "缺少环境变量: " + ", ".join(names) if names else "适配器不可用"
    return AdapterStatus(
        name=name,
        available=False,
        reason=BLOCKED,
        detail=util.redact(detail),
        missing_env=names,
    )


def missing_env(names: List[str]) -> List[str]:
    """只返回**缺少的变量名**，绝不返回值。"""
    return [name for name in names if not str(os.environ.get(name) or "").strip()]


def stage_into(source: Any, dest_dir: Any, ref: AssetRef) -> FetchResult:
    """把已取到的本地文件原子落位到 ``dest_dir`` 并校验 sha256。"""
    src = Path(str(source))
    if not src.is_file():
        return FetchResult.failure("SOURCE_MISSING", util.EXIT_NO_SOURCE, "取件结果不存在")
    try:
        filename = util.safe_filename(ref.filename or src.name)
    except util.PathForbidden as exc:
        return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
    target_root = Path(str(dest_dir))
    target = target_root / filename
    try:
        target_root.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp = tempfile.mkstemp(dir=str(target_root), prefix=".fetch-")
        os.close(handle_fd)
    except OSError as exc:
        return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
    try:
        shutil.copyfile(str(src), tmp)
        digest = util.sha256_file(tmp)
        size = os.path.getsize(tmp)
        if ref.sha256 and digest != ref.sha256:
            os.unlink(tmp)
            return FetchResult(
                ok=False,
                sha256=digest,
                bytes=size,
                reason="SHA256_MISMATCH",
                exit_code=util.EXIT_FAIL,
                detail="sha256 不匹配: {0} != {1}".format(digest, ref.sha256),
            )
        os.replace(tmp, str(target))
        try:
            os.chmod(str(target), 0o644)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return FetchResult(ok=True, path=str(target), sha256=digest, bytes=size)


def read_only_query(query: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(query or {})


def parse_int(value: Any, default: int = DEFAULT_PRIORITY) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
