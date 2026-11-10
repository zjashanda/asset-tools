"""``nas`` 适配器：委托 ``fileball`` 取内网 NAS / FileStation / OSS 文件。

本仓**不实现** FTP / OSS SDK 细节，也不读取、传递或打印 NAS 凭据：
``NAS_ACCOUNT`` / ``NAS_PASSWORD`` 与 NAS 地址由被委托的 CLI 自己读取。

委托协议
--------
``<cli> list --json``::

    [{"path": "/share/fw/x.zip", "filename": "x.zip", "bytes": 123,
      "sha256": "..."}]

``<cli> get <path> --out <DIR>``：把文件落到 ``<DIR>``。

CLI 定位：``ASSET_NAS_CLI``（绝对路径或 PATH 命令名）→ 配置 ``cli`` → ``fileball``。
"""

import fnmatch
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from .. import util
from .base import (
    KIND_NAS,
    Adapter,
    AdapterStatus,
    AssetRef,
    FetchResult,
    blocked_status,
    ok_status,
    parse_int,
    stage_into,
)

DEFAULT_CLI = "fileball"


class NasAdapter(Adapter):
    name = KIND_NAS
    kind = KIND_NAS

    def probe(self) -> AdapterStatus:
        cli = self._cli()
        if not cli:
            return blocked_status(
                self.name,
                [],
                "缺少委托 CLI：ASSET_NAS_CLI 未设置且 PATH 中没有 {0}".format(DEFAULT_CLI),
            )
        return ok_status(self.name, "委托 CLI: {0}".format(os.path.basename(cli)))

    # ---------------------------------------------------------------- 内部

    def _cli(self) -> str:
        candidate = os.environ.get("ASSET_NAS_CLI") or self.config_value("cli")
        return util.resolve_cli(candidate, DEFAULT_CLI) or ""

    def _list(self, cli: str) -> List[Dict[str, Any]]:
        rc, out, err = util.run_cmd([cli, "list", "--json"], timeout=self.timeout())
        if rc == 124:
            self.last_error = "list 超时"
            return []
        if rc != 0:
            self.last_error = util.redact(err.strip() or "list 失败 rc={0}".format(rc))
            return []
        try:
            data = json.loads(out)
        except ValueError as exc:
            self.last_error = util.redact("list JSON 解析失败: {0}".format(exc))
            return []
        if isinstance(data, dict):
            data = data.get("files") or data.get("items") or []
        return [item for item in data if isinstance(item, dict)]

    # ---------------------------------------------------------------- 契约

    def discover(self, query: Dict[str, Any]) -> List[AssetRef]:
        status = self._probe()
        if not status.available:
            return []
        cli = self._cli()
        query = dict(query or {})
        glob_filter = str(query.get("filename_glob") or "")
        path_filter = str(query.get("path_contains") or "")
        root = str(query.get("root") or "")
        priority = parse_int(query.get("priority"), 40)
        refs: List[AssetRef] = []
        for item in self._list(cli):
            path = str(item.get("path") or "")
            filename = str(item.get("filename") or os.path.basename(path))
            if not path or not filename:
                continue
            if glob_filter and not fnmatch.fnmatch(filename, glob_filter):
                continue
            if path_filter and path_filter not in path:
                continue
            if root and not path.startswith(root.rstrip("/") + "/") and path != root:
                continue
            refs.append(
                AssetRef(
                    type=KIND_NAS,
                    filename=filename,
                    ref=path,
                    sha256=str(item.get("sha256") or "").strip().lower(),
                    bytes=parse_int(item.get("bytes"), 0),
                    priority=priority,
                    extra={"origin": "nas", "root": root},
                )
            )
        return refs

    def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult:
        status = self._probe()
        if not status.available:
            return self.blocked_result(status.detail)
        cli = self._cli()
        remote_path = str(ref.ref or "").strip()
        if not remote_path:
            return FetchResult.failure("USAGE", util.EXIT_USAGE, "NAS 来源缺少 path")
        try:
            filename = util.safe_filename(ref.filename or os.path.basename(remote_path))
            staging = Path(str(dest_dir)) / ".nas-staging"
            staging.mkdir(parents=True, exist_ok=True)
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        except OSError as exc:
            return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
        workdir = ""
        try:
            workdir = tempfile.mkdtemp(dir=str(staging), prefix="get-")
            rc, out, err = util.run_cmd(
                [cli, "get", remote_path, "--out", workdir], timeout=self.timeout()
            )
            if rc == 124:
                return FetchResult.failure("TIMEOUT", util.EXIT_NETWORK, "get 超时")
            if rc != 0:
                return FetchResult.failure(
                    "NAS_GET_FAILED",
                    util.EXIT_NO_SOURCE,
                    util.redact(err.strip() or "get 失败 rc={0}".format(rc)),
                )
            produced = _locate(out, Path(workdir), filename)
            if produced is None:
                return FetchResult.failure(
                    "NAS_NO_FILE", util.EXIT_NO_SOURCE, "委托 CLI 未产出文件"
                )
            return stage_into(produced, dest_dir, ref)
        finally:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
            try:
                staging.rmdir()
            except OSError:
                pass


def _locate(stdout: str, workdir: Path, filename: str = "") -> Any:
    text = (stdout or "").strip()
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("path"):
            candidate = Path(str(data["path"]))
            if candidate.is_file():
                return candidate
    if filename:
        preferred = workdir / filename
        if preferred.is_file():
            return preferred
    for candidate in sorted(workdir.rglob("*")):
        if candidate.is_file():
            return candidate
    return None
