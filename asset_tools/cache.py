"""内容寻址缓存：``<root>/sha256/<前2>/<次2>/<sha256>/<filename>``。

设计约束
--------
* 缓存路径完全由 sha256 决定，因此"命中"本身就是一次强校验。
* 所有写入先落到 ``<root>/_incoming/`` 的临时文件，校验通过后 ``os.replace``。
* 校验失败的旧条目在 ``apply=True`` 时移到 ``<root>/_quarantine/``，不直接删除。
* 任何越界路径（``..``、绝对路径、``<root>`` 之外）一律拒绝。
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import util
from .manifest import Asset

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "ark-assets"

VERIFY_OK = "OK"
VERIFY_MISSING = "MISSING"
VERIFY_CORRUPT = "CORRUPT"


@dataclass
class PutResult:
    """``put`` 的结果：dry-run 时 ``applied`` 为 False。"""

    ok: bool
    path: str = ""
    sha256: str = ""
    bytes: int = 0
    applied: bool = False
    existed: bool = False
    reason: str = "OK"
    detail: str = ""

    @property
    def exit_code(self) -> int:
        if self.ok:
            return util.EXIT_OK
        if self.reason == "SHA256_MISMATCH":
            return util.EXIT_FAIL
        if self.reason == "FORBIDDEN":
            return util.EXIT_FORBIDDEN
        return util.EXIT_NO_SOURCE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "applied": self.applied,
            "existed": self.existed,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class PruneEntry:
    rel: str
    path: str
    bytes: int
    age_days: int
    area: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rel": self.rel,
            "path": self.path,
            "bytes": self.bytes,
            "age_days": self.age_days,
            "area": self.area,
        }


@dataclass
class PrunePlan:
    keep_days: int
    cutoff: str
    applied: bool = False
    entries: List[PruneEntry] = field(default_factory=list)
    removed: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.entries)

    def area_bytes(self, area: str) -> int:
        return sum(item.bytes for item in self.entries if item.area == area)

    def area_count(self, area: str) -> int:
        return sum(1 for item in self.entries if item.area == area)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "keep_days": self.keep_days,
            "cutoff": self.cutoff,
            "applied": self.applied,
            "count": len(self.entries),
            "total_bytes": self.total_bytes,
            "quarantine_bytes": self.area_bytes("_quarantine"),
            "quarantine_count": self.area_count("_quarantine"),
            "incoming_bytes": self.area_bytes("_incoming"),
            "incoming_count": self.area_count("_incoming"),
            "removed": self.removed,
            "entries": [item.to_dict() for item in self.entries],
        }


@dataclass
class CacheEntry:
    rel: str
    path: str
    filename: str
    sha256: str
    bytes: int
    mtime: str
    verified: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rel": self.rel,
            "path": self.path,
            "filename": self.filename,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "mtime": self.mtime,
            "verified": self.verified,
        }


class ContentCache:
    """按 sha256 寻址的本地缓存。"""

    def __init__(self, root: Any = None) -> None:
        raw = root or os.environ.get("ARK_ASSET_CACHE") or str(DEFAULT_CACHE_DIR)
        self.root = Path(str(raw)).expanduser().resolve()
        self.last_quarantine: str = ""

    # ---------------------------------------------------------------- 路径

    def path_for(self, asset: Asset) -> Path:
        digest = str(getattr(asset, "sha256", "") or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise util.PathForbidden("资产 {0} 的 sha256 非法，无法定位缓存".format(getattr(asset, "id", "?")))
        filename = util.safe_filename(getattr(asset, "filename", "") or "")
        target = self.root / "sha256" / digest[:2] / digest[2:4] / digest / filename
        self._guard(target)
        return target

    def _guard(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise util.PathForbidden("缓存路径越界: {0}".format(resolved))
        return resolved

    @property
    def incoming_dir(self) -> Path:
        return self.root / "_incoming"

    @property
    def quarantine_dir(self) -> Path:
        return self.root / "_quarantine"

    # ---------------------------------------------------------------- 基本操作

    def has(self, asset: Asset) -> bool:
        try:
            path = self.path_for(asset)
        except util.PathForbidden:
            return False
        if not path.is_file():
            return False
        try:
            return util.sha256_file(path) == asset.sha256
        except OSError:
            return False

    def put(self, asset: Asset, src_path: Any, apply: bool = False) -> PutResult:
        """把 ``src_path`` 放入缓存。dry-run 只返回计划，不落盘。"""
        source = Path(str(src_path)).expanduser()
        if not source.is_file():
            return PutResult(
                ok=False,
                reason="SOURCE_MISSING",
                detail="源文件不存在: {0}".format(util.rel_display(source)),
            )
        try:
            dest = self.path_for(asset)
            size = source.stat().st_size
            digest = util.sha256_file(source)
        except util.PathForbidden as exc:
            return PutResult(ok=False, reason="FORBIDDEN", detail=str(exc))
        except OSError as exc:
            return PutResult(ok=False, reason="IO_ERROR", detail=util.redact(exc))
        if digest != asset.sha256:
            return PutResult(
                ok=False,
                sha256=digest,
                bytes=size,
                reason="SHA256_MISMATCH",
                detail="源文件 sha256 与清单不符: {0} != {1}".format(digest, asset.sha256),
            )
        existed = False
        if dest.is_file():
            try:
                existed = util.sha256_file(dest) == digest
            except OSError:
                existed = False
        if not apply:
            return PutResult(
                ok=True,
                path=str(dest),
                sha256=digest,
                bytes=size,
                applied=False,
                existed=existed,
                detail="DRY-RUN: 将写入缓存 {0}".format(util.rel_display(dest)),
            )
        try:
            self.incoming_dir.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp = tempfile.mkstemp(dir=str(self.incoming_dir), prefix="put-")
            os.close(handle_fd)
            try:
                shutil.copyfile(str(source), tmp)
                if util.sha256_file(tmp) != digest:
                    os.unlink(tmp)
                    return PutResult(
                        ok=False,
                        sha256=digest,
                        bytes=size,
                        reason="SHA256_MISMATCH",
                        detail="暂存副本校验失败，已丢弃",
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, str(dest))
                try:
                    os.chmod(str(dest), 0o644)
                except OSError:
                    pass
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except OSError as exc:
            return PutResult(ok=False, reason="IO_ERROR", detail=util.redact(exc))
        return PutResult(
            ok=True,
            path=str(dest),
            sha256=digest,
            bytes=size,
            applied=True,
            existed=existed,
            detail="已写入缓存",
        )

    def verify(self, asset: Asset, apply: bool = False) -> str:
        """返回 ``OK`` / ``MISSING`` / ``CORRUPT``；CORRUPT 且 apply 时隔离。"""
        try:
            path = self.path_for(asset)
        except util.PathForbidden:
            return VERIFY_CORRUPT
        if not path.is_file():
            return VERIFY_MISSING
        try:
            digest = util.sha256_file(path)
        except OSError:
            return VERIFY_CORRUPT
        if digest == asset.sha256:
            return VERIFY_OK
        if apply:
            self.quarantine(asset, path)
        return VERIFY_CORRUPT

    def quarantine(self, asset: Asset, path: Optional[Path] = None) -> str:
        """把损坏条目移入 ``_quarantine/<stamp>-<filename>``，返回新路径。"""
        source = Path(path) if path else self.path_for(asset)
        if not source.is_file():
            return ""
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        stamp = util.format_stamp()
        base = "{0}-{1}".format(stamp, source.name)
        target = self.quarantine_dir / base
        counter = 1
        while target.exists():
            target = self.quarantine_dir / "{0}-{1}-{2}".format(stamp, counter, source.name)
            counter += 1
        self._guard(target)
        shutil.move(str(source), str(target))
        self.last_quarantine = str(target)
        return str(target)

    # ---------------------------------------------------------------- 巡检

    def list_entries(self, deep: bool = False) -> List[CacheEntry]:
        base = self.root / "sha256"
        entries: List[CacheEntry] = []
        if not base.is_dir():
            return entries
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            parts = path.relative_to(self.root).parts
            digest = ""
            if len(parts) >= 5 and parts[0] == "sha256":
                digest = parts[3]
            verified: Optional[bool] = None
            if deep and digest:
                try:
                    verified = util.sha256_file(path) == digest
                except OSError:
                    verified = False
            entries.append(
                CacheEntry(
                    rel=str(path.relative_to(self.root)),
                    path=str(path),
                    filename=path.name,
                    sha256=digest,
                    bytes=stat.st_size,
                    mtime=util.format_display(datetime.fromtimestamp(stat.st_mtime, util.BEIJING)),
                    verified=verified,
                )
            )
        return entries

    def stats(self) -> Dict[str, Any]:
        entries = self.list_entries()
        quarantine = [
            item for item in self._area_files("_quarantine")
        ]
        incoming = [item for item in self._area_files("_incoming")]
        return {
            "root": str(self.root),
            "entries": len(entries),
            "total_bytes": sum(item.bytes for item in entries),
            "quarantine_files": len(quarantine),
            "quarantine_bytes": sum(size for _, size in quarantine),
            "incoming_files": len(incoming),
            "incoming_bytes": sum(size for _, size in incoming),
        }

    def _area_files(self, area: str) -> List[Any]:
        base = self.root / area
        files: List[Any] = []
        if not base.is_dir():
            return files
        for path in sorted(base.rglob("*")):
            if path.is_file():
                try:
                    files.append((path, path.stat().st_size))
                except OSError:
                    continue
        return files

    def prune(self, keep_days: int = 30, now: Optional[datetime] = None, apply: bool = False) -> PrunePlan:
        """按 mtime 清理过期条目；``apply=False`` 只列清单。"""
        keep = int(keep_days)
        if keep < 0:
            raise ValueError("keep_days 不得为负")
        moment = now or util.now_beijing()
        cutoff = moment - timedelta(days=keep)
        plan = PrunePlan(keep_days=keep, cutoff=util.format_display(cutoff))
        for area in ("sha256", "_quarantine", "_incoming"):
            base = self.root / area
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                mtime = datetime.fromtimestamp(stat.st_mtime, util.BEIJING)
                if mtime >= cutoff:
                    continue
                plan.entries.append(
                    PruneEntry(
                        rel=str(path.relative_to(self.root)),
                        path=str(path),
                        bytes=stat.st_size,
                        age_days=max(0, (moment - mtime).days),
                        area=area,
                    )
                )
        if not apply:
            return plan
        plan.applied = True
        for entry in plan.entries:
            target = Path(entry.path)
            try:
                self._guard(target)
                target.unlink()
                plan.removed += 1
            except (OSError, util.PathForbidden):
                continue
        self._prune_empty_dirs()
        return plan

    def _prune_empty_dirs(self) -> None:
        bases = [self.root / "sha256", self.root / "_quarantine", self.root / "_incoming"]
        for base in bases:
            if not base.is_dir():
                continue
            for current, _dirs, _files in os.walk(str(base), topdown=False):
                try:
                    os.rmdir(current)
                except OSError:
                    continue
