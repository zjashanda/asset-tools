"""供应（supply）：缓存 → 目标目录落位 + 供应记录。

本仓**只供应，不归档**：记录里 ``archive_owner`` 固定 ``"ark-archive"``、
``is_receipt`` 固定 ``False``，明确声明本仓不产出归档 receipt。
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import util
from .adapters import get_adapter
from .adapters.base import AssetRef
from .cache import VERIFY_OK, ContentCache
from .manifest import Asset, AssetManifest

RECORD_SCHEMA = "ark.supply-record.v1"
ARCHIVE_OWNER = "ark-archive"


@dataclass
class SupplyResult:
    ok: bool
    asset_id: str
    reason: str = "OK"
    detail: str = ""
    exit_code: int = util.EXIT_OK
    applied: bool = False
    source_type: str = ""
    source_ref: str = ""
    cache_path: str = ""
    cache_state: str = ""
    dest_path: str = ""
    dest_sha256: str = ""
    dest_sha256_verified: bool = False
    sha256: str = ""
    bytes: int = 0
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    record_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "asset_id": self.asset_id,
            "reason": self.reason,
            "detail": util.redact(self.detail),
            "exit_code": self.exit_code,
            "applied": self.applied,
            "source_type": self.source_type,
            "source_ref": util.redact(self.source_ref),
            "cache_path": util.redact(self.cache_path),
            "cache_state": self.cache_state,
            "dest_path": util.redact(self.dest_path),
            "dest_sha256": self.dest_sha256,
            "dest_sha256_verified": self.dest_sha256_verified,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "attempts": util.redact_obj(self.attempts),
            "record_path": util.redact(self.record_path),
        }


# --------------------------------------------------------------------- 来源映射


def source_ref_of(source: Dict[str, Any]) -> str:
    stype = str(source.get("type") or "")
    if stype == "node-storage":
        return "{0}:{1}".format(source.get("node") or "?", source.get("path") or "")
    if stype == "oss":
        return str(source.get("key") or "")
    if stype == "http":
        return str(source.get("url") or source.get("path") or "")
    return str(source.get("path") or source.get("ref") or source.get("id") or "")


def ref_for(asset: Asset, source: Dict[str, Any]) -> AssetRef:
    stype = str(source.get("type") or "")
    ref = (
        source.get("url")
        or source.get("path")
        or source.get("key")
        or source.get("ref")
        or source.get("id")
        or ""
    )
    try:
        priority = int(source.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    return AssetRef(
        type=stype,
        filename=asset.filename,
        ref=str(ref),
        sha256=asset.sha256,
        bytes=asset.bytes,
        priority=priority,
        extra=dict(source),
    )


# --------------------------------------------------------------------- 计划


def plan_asset(
    manifest: AssetManifest,
    asset: Asset,
    cache: ContentCache,
    configs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """只读计划：缓存状态 + 来源顺序 + 每个来源当前可用性。"""
    try:
        cache_state = cache.verify(asset)
    except util.PathForbidden:
        cache_state = "UNKNOWN"
    sources: List[Dict[str, Any]] = []
    any_available = False
    for source in asset.ordered_sources():
        stype = str(source.get("type") or "")
        try:
            adapter = get_adapter(stype, (configs or {}).get(stype))
            status = adapter._probe()
            available = status.available
            reason = status.reason
            detail = util.redact(status.detail)
        except KeyError:
            available = False
            reason = "BLOCKED:ADAPTER_UNAVAILABLE"
            detail = "适配器未注册"
        any_available = any_available or available
        sources.append(
            {
                "type": stype,
                "priority": source.get("priority", 100),
                "ref": util.redact(source_ref_of(source)),
                "available": available,
                "reason": reason,
                "detail": detail,
            }
        )
    if cache_state == VERIFY_OK:
        action = "use-cache"
    elif any_available:
        action = "fetch"
    else:
        action = "no-source"
    return {
        "id": asset.id,
        "kind": asset.kind,
        "filename": asset.filename,
        "bytes": asset.bytes,
        "sha256": asset.sha256,
        "cache_state": cache_state,
        "cache_path": util.redact(str(_safe_cache_path(cache, asset))),
        "sources": sources,
        "action": action,
        "material_rel": asset.material_rel,
    }


def _safe_cache_path(cache: ContentCache, asset: Asset) -> str:
    try:
        return str(cache.path_for(asset))
    except (util.PathForbidden, ValueError):
        return ""


# --------------------------------------------------------------------- 供应


def supply_asset(
    manifest: AssetManifest,
    asset_id: str,
    dest_dir: Any,
    cache: ContentCache,
    apply: bool = False,
    record_dir: Any = None,
) -> SupplyResult:
    """把单个资产从缓存（或来源）落到 ``dest_dir``。"""
    asset = manifest.by_id(asset_id)
    if asset is None:
        return SupplyResult(
            ok=False,
            asset_id=str(asset_id),
            reason="ASSET_NOT_FOUND",
            detail="清单中没有资产 {0!r}".format(asset_id),
            exit_code=util.EXIT_USAGE,
        )
    result = SupplyResult(
        ok=False,
        asset_id=asset.id,
        sha256=asset.sha256,
        bytes=asset.bytes,
        applied=bool(apply),
    )
    try:
        material_rel = util.safe_rel_path(asset.material_rel)
    except util.PathForbidden as exc:
        result.reason = "FORBIDDEN"
        result.exit_code = util.EXIT_FORBIDDEN
        result.detail = str(exc)
        return result
    dest_root = Path(str(dest_dir)).expanduser().resolve()
    dest_path = (dest_root / material_rel).resolve()
    if not str(dest_path).startswith(str(dest_root) + os.sep):
        result.reason = "FORBIDDEN"
        result.exit_code = util.EXIT_FORBIDDEN
        result.detail = "目标路径越界: {0}".format(util.redact(str(dest_path)))
        return result
    result.dest_path = str(dest_path)
    result.cache_state = _verify(cache, asset)

    if not apply:
        ordered = asset.ordered_sources()
        result.ok = result.cache_state == VERIFY_OK or bool(ordered)
        result.exit_code = util.EXIT_OK if result.ok else util.EXIT_NO_SOURCE
        result.reason = "DRY_RUN"
        result.source_type = "cache" if result.cache_state == VERIFY_OK else ""
        result.detail = "DRY-RUN: 缓存={0}；将落位到 {1}".format(
            result.cache_state, util.redact(str(dest_path))
        )
        for source in ordered:
            result.attempts.append(
                {
                    "type": source.get("type"),
                    "priority": source.get("priority", 100),
                    "ref": util.redact(source_ref_of(source)),
                    "ok": None,
                }
            )
        return result

    source_used: Dict[str, Any] = {}
    cache_file = ""
    if result.cache_state == VERIFY_OK:
        cache_file = str(cache.path_for(asset))
        result.source_type = "cache"
        result.source_ref = "cache:{0}".format(asset.sha256)
        result.cache_path = cache_file
    else:
        cache_file, source_used = _fetch_into_cache(asset, cache, result)
        if not cache_file:
            result.reason = result.attempts[-1]["reason"] if result.attempts else "NO_SOURCE"
            result.exit_code = result.attempts[-1]["exit_code"] if result.attempts else util.EXIT_NO_SOURCE
            if result.exit_code not in (
                util.EXIT_NO_SOURCE,
                util.EXIT_FORBIDDEN,
                util.EXIT_NETWORK,
                util.EXIT_FAIL,
            ):
                result.exit_code = util.EXIT_NO_SOURCE
            result.detail = "所有来源均失败"
            return result
        result.cache_path = cache_file
        result.source_type = str(source_used.get("type") or "")
        result.source_ref = source_ref_of(source_used)
        result.cache_state = VERIFY_OK

    digest = _copy_atomic(Path(cache_file), dest_path)
    if digest is None:
        result.reason = "COPY_FAILED"
        result.exit_code = util.EXIT_FAIL
        result.detail = "落位失败: {0}".format(util.redact(str(dest_path)))
        return result
    result.dest_sha256 = digest
    if asset.sha256 and digest != asset.sha256:
        result.reason = "SHA256_MISMATCH"
        result.exit_code = util.EXIT_FAIL
        result.detail = "落位校验失败: {0} != {1}".format(digest, asset.sha256)
        return result
    result.dest_sha256_verified = bool(asset.sha256) and digest == asset.sha256
    record = write_supply_record(
        asset, result, dest_path, base_dir=dest_root, record_dir=record_dir
    )
    result.record_path = record
    result.ok = True
    result.reason = "OK"
    result.exit_code = util.EXIT_OK
    result.detail = "已供应到 {0}".format(util.redact(str(dest_path)))
    return result


def _verify(cache: ContentCache, asset: Asset) -> str:
    try:
        return cache.verify(asset, apply=False)
    except util.PathForbidden:
        return "UNKNOWN"


def _fetch_into_cache(asset: Asset, cache: ContentCache, result: SupplyResult) -> Any:
    staging = ""
    try:
        cache.incoming_dir.mkdir(parents=True, exist_ok=True)
        staging = tempfile.mkdtemp(dir=str(cache.incoming_dir), prefix="fetch-")
    except OSError as exc:
        result.attempts.append(
            {
                "type": "",
                "ok": False,
                "reason": "IO_ERROR",
                "exit_code": util.EXIT_FAIL,
                "detail": util.redact(exc),
            }
        )
        return "", {}
    used: Dict[str, Any] = {}
    try:
        for source in asset.ordered_sources():
            stype = str(source.get("type") or "")
            ref = ref_for(asset, source)
            attempt: Dict[str, Any] = {
                "type": stype,
                "priority": source.get("priority", 100),
                "ref": util.redact(source_ref_of(source)),
                "ok": False,
                "reason": "",
                "exit_code": util.EXIT_OK,
            }
            try:
                adapter = get_adapter(stype)
            except KeyError:
                attempt["reason"] = "BLOCKED:ADAPTER_UNAVAILABLE"
                attempt["exit_code"] = util.EXIT_NO_SOURCE
                result.attempts.append(attempt)
                continue
            try:
                fetched = adapter.fetch(ref, Path(staging))
            except Exception as exc:  # noqa: BLE001 - 适配器异常不得穿透到供应层
                attempt["reason"] = "ADAPTER_ERROR"
                attempt["exit_code"] = util.EXIT_FAIL
                attempt["detail"] = util.redact("adapter raised: {0}".format(exc))
                result.attempts.append(attempt)
                continue
            attempt["reason"] = fetched.reason
            attempt["exit_code"] = fetched.exit_code
            attempt["detail"] = util.redact(fetched.detail)
            if not fetched.ok or not fetched.path:
                result.attempts.append(attempt)
                continue
            put = cache.put(asset, fetched.path, apply=True)
            attempt["cache"] = put.to_dict()
            if put.ok:
                attempt["ok"] = True
                result.attempts.append(attempt)
                used = source
                return put.path, used
            attempt["reason"] = put.reason
            attempt["exit_code"] = put.exit_code
            result.attempts.append(attempt)
        return "", used
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


def _copy_atomic(source: Path, target: Path) -> Optional[str]:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".supply-")
        os.close(handle_fd)
    except OSError:
        return None
    try:
        shutil.copyfile(str(source), tmp)
        digest = util.sha256_file(tmp)
        os.replace(tmp, str(target))
        try:
            os.chmod(str(target), 0o644)
        except OSError:
            pass
    except OSError:
        return None
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return digest


# --------------------------------------------------------------------- 记录


def record_path_for(
    asset_id: str,
    base_dir: Any = None,
    record_dir: Any = None,
) -> Path:
    safe_id = util.safe_filename(asset_id)
    base = Path(str(record_dir)) if record_dir else Path(str(base_dir)) / ".ark-supply"
    return (base / (safe_id + ".json")).resolve()


def write_supply_record(
    asset: Asset,
    result: SupplyResult,
    dest_path: Path,
    base_dir: Any = None,
    record_dir: Any = None,
) -> str:
    """写 ``ark.supply-record.v1``；``is_receipt`` 恒为 False。"""
    target = record_path_for(
        asset.id, base_dir=base_dir or dest_path.parent, record_dir=record_dir
    )
    payload = {
        "schema": RECORD_SCHEMA,
        "asset_id": asset.id,
        "version": asset.version,
        "filename": asset.filename,
        "bytes": result.bytes or asset.bytes,
        "sha256": asset.sha256,
        "source_type": result.source_type,
        "source_ref": util.redact(result.source_ref),
        "dest_path": util.redact(str(dest_path)),
        "dest_sha256": result.dest_sha256,
        "dest_sha256_verified": result.dest_sha256_verified,
        "supplied_at": util.format_display(),
        "node": util.hostname(),
        "archive_owner": ARCHIVE_OWNER,
        "is_receipt": False,
    }
    util.atomic_write_json(target, payload)
    return str(target)


# --------------------------------------------------------------------- 只取件


def fetch_to_cache(
    manifest: AssetManifest,
    asset_id: str,
    cache: ContentCache,
    apply: bool = False,
) -> Dict[str, Any]:
    """把单个资产拉进缓存（不落目标目录）。"""
    asset = manifest.by_id(asset_id)
    if asset is None:
        return {
            "ok": False,
            "asset_id": str(asset_id),
            "reason": "ASSET_NOT_FOUND",
            "exit_code": util.EXIT_USAGE,
            "applied": False,
        }
    state = _verify(cache, asset)
    if state == VERIFY_OK:
        return {
            "ok": True,
            "asset_id": asset.id,
            "reason": "CACHE_HIT",
            "exit_code": util.EXIT_OK,
            "applied": bool(apply),
            "cache_state": state,
            "cache_path": util.redact(_safe_cache_path(cache, asset)),
            "attempts": [],
        }
    if not apply:
        return {
            "ok": True,
            "asset_id": asset.id,
            "reason": "DRY_RUN",
            "exit_code": util.EXIT_OK,
            "applied": False,
            "cache_state": state,
            "cache_path": util.redact(_safe_cache_path(cache, asset)),
            "attempts": [
                {
                    "type": source.get("type"),
                    "priority": source.get("priority", 100),
                    "ref": util.redact(source_ref_of(source)),
                }
                for source in asset.ordered_sources()
            ],
        }
    result = SupplyResult(ok=False, asset_id=asset.id, applied=True)
    cache_file, used = _fetch_into_cache(asset, cache, result)
    if not cache_file:
        return {
            "ok": False,
            "asset_id": asset.id,
            "reason": result.attempts[-1]["reason"] if result.attempts else "NO_SOURCE",
            "exit_code": result.attempts[-1]["exit_code"] if result.attempts else util.EXIT_NO_SOURCE,
            "applied": True,
            "cache_state": state,
            "attempts": util.redact_obj(result.attempts),
        }
    return {
        "ok": True,
        "asset_id": asset.id,
        "reason": "OK",
        "exit_code": util.EXIT_OK,
        "applied": True,
        "cache_state": VERIFY_OK,
        "cache_path": util.redact(cache_file),
        "source_type": str(used.get("type") or ""),
        "source_ref": util.redact(source_ref_of(used)),
        "attempts": util.redact_obj(result.attempts),
    }
