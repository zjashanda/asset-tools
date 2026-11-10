"""``local`` 适配器：本机文件系统。

配置项（``config/providers.example.json`` → ``adapters.local``）::

    {"allowed_roots": ["/data/assets"], "priority": 5}

``allowed_roots`` 为空表示不额外限制（仍需存在且可读）；非空时越界路径返回
``FORBIDDEN``。
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import util
from .base import (
    KIND_LOCAL,
    Adapter,
    AdapterStatus,
    AssetRef,
    FetchResult,
    ok_status,
    parse_int,
    stage_into,
)


class LocalAdapter(Adapter):
    name = KIND_LOCAL
    kind = KIND_LOCAL

    def probe(self) -> AdapterStatus:
        roots = self._allowed_roots()
        if roots:
            detail = "本机文件系统；允许根目录 {0} 个".format(len(roots))
        else:
            detail = "本机文件系统；未限制 allowed_roots"
        return ok_status(self.name, detail)

    # ---------------------------------------------------------------- 内部

    def _allowed_roots(self) -> List[Path]:
        raw = self.config_value("allowed_roots") or []
        roots: List[Path] = []
        if isinstance(raw, str):
            raw = [raw]
        for item in raw:
            try:
                roots.append(Path(str(item)).expanduser().resolve())
            except OSError:
                continue
        return roots

    def _resolve(self, ref: Any) -> Path:
        raw = str(ref or "").strip()
        if not raw:
            raise util.PathForbidden("空路径")
        if "\x00" in raw:
            raise util.PathForbidden("路径包含 NUL 字节")
        path = Path(raw).expanduser()
        resolved = path.resolve()
        roots = self._allowed_roots()
        if roots and not any(util.is_within(resolved, root) for root in roots):
            raise util.PathForbidden("路径不在 allowed_roots 内: {0}".format(raw))
        return resolved

    # ---------------------------------------------------------------- 契约

    def discover(self, query: Dict[str, Any]) -> List[AssetRef]:
        query = dict(query or {})
        candidates: List[Path] = []
        paths = query.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        for item in paths or []:
            candidates.append(Path(str(item)).expanduser())
        if query.get("path"):
            candidates.append(Path(str(query["path"])).expanduser())
        pattern = query.get("glob")
        root = query.get("root") or self.config_value("root")
        if pattern and root:
            try:
                candidates.extend(sorted(Path(str(root)).expanduser().glob(str(pattern))))
            except (OSError, ValueError) as exc:
                self.last_error = util.redact(exc)
        priority = parse_int(query.get("priority"), 5)
        refs: List[AssetRef] = []
        for path in candidates:
            try:
                resolved = self._resolve(path)
            except util.PathForbidden as exc:
                self.last_error = util.redact(exc)
                continue
            if not resolved.is_file() or not os.access(str(resolved), os.R_OK):
                continue
            try:
                digest = util.sha256_file(resolved)
                size = resolved.stat().st_size
            except OSError as exc:
                self.last_error = util.redact(exc)
                continue
            refs.append(
                AssetRef(
                    type=KIND_LOCAL,
                    filename=resolved.name,
                    ref=str(resolved),
                    sha256=digest,
                    bytes=size,
                    priority=priority,
                    extra={"origin": "local"},
                )
            )
        return refs

    def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult:
        try:
            path = self._resolve(ref.ref)
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        if util.is_credential_filename(path.name):
            return FetchResult.failure(
                "FORBIDDEN", util.EXIT_FORBIDDEN, "拒绝取件凭据文件名: {0}".format(path.name)
            )
        if not path.is_file():
            return FetchResult.failure("SOURCE_MISSING", util.EXIT_NO_SOURCE, "本地文件不存在")
        if not os.access(str(path), os.R_OK):
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, "本地文件不可读")
        return stage_into(path, dest_dir, ref)
