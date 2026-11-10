"""``node-storage`` 适配器：从 1006 节点（storage1006）取件。

::: note
默认端点 ``storage1006`` / 端口 ``10027`` / 基目录 ``/srv/storage/ark/artifacts``
都只是**示例**，真实环境用环境变量覆盖。
:::

需要的环境变量（只出现名字）：

* ``ARK_STORAGE_SSH_PASS`` —— 必填，SSH 密码；只经 ``sshpass -e`` 的环境变量传入
* ``ARK_STORAGE_HOST`` / ``ARK_STORAGE_USER`` / ``ARK_STORAGE_PORT`` —— 可选覆盖
* ``ARK_STORAGE_BASE_DIR`` —— 可选覆盖基目录

硬约束：

* 命令固定为 ``sshpass -e scp ...``：密码走 ``SSHPASS`` 环境变量，
  **绝不**出现在 argv、日志、异常里。
* 远端路径必须在 ``base_dir`` 之下，否则 ``FORBIDDEN``。
"""

import os
import posixpath
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import util
from .base import (
    KIND_NODE_STORAGE,
    Adapter,
    AdapterStatus,
    AssetRef,
    FetchResult,
    blocked_status,
    ok_status,
    parse_int,
    stage_into,
)

DEFAULTS = {
    "host": "storage1006",
    "user": "administrator",
    "port": 10027,
    "base_dir": "/srv/storage/ark/artifacts",
}

PASSWORD_ENV = "ARK_STORAGE_SSH_PASS"


class NodeStorageAdapter(Adapter):
    name = KIND_NODE_STORAGE
    kind = KIND_NODE_STORAGE

    def probe(self) -> AdapterStatus:
        absent = [name for name in [PASSWORD_ENV] if not os.environ.get(name)]
        details: List[str] = []
        sshpass = util.which("sshpass")
        scp = util.which("scp")
        if not sshpass:
            details.append("缺少 sshpass 可执行文件")
        if not scp:
            details.append("缺少 scp 可执行文件")
        if absent:
            details.insert(0, "缺少环境变量: " + ", ".join(absent))
        if absent or details:
            return blocked_status(self.name, absent, "；".join(details))
        settings = self.settings()
        return ok_status(
            self.name,
            "端点 {0}:{1}，基目录 {2}".format(settings["host"], settings["port"], settings["base_dir"]),
        )

    # ---------------------------------------------------------------- 内部

    def settings(self) -> Dict[str, Any]:
        host = os.environ.get("ARK_STORAGE_HOST") or self.config_value("host") or DEFAULTS["host"]
        user = os.environ.get("ARK_STORAGE_USER") or self.config_value("user") or DEFAULTS["user"]
        raw_port = (
            os.environ.get("ARK_STORAGE_PORT")
            or self.config_value("port")
            or DEFAULTS["port"]
        )
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = int(DEFAULTS["port"])
        base_dir = (
            os.environ.get("ARK_STORAGE_BASE_DIR")
            or self.config_value("base_dir")
            or DEFAULTS["base_dir"]
        )
        return {
            "host": str(host),
            "user": str(user),
            "port": port,
            "base_dir": str(base_dir).rstrip("/") or "/",
        }

    def resolve_remote(self, ref_path: Any) -> str:
        """把来源路径规整为 ``base_dir`` 之下的绝对 POSIX 路径。"""
        settings = self.settings()
        base = settings["base_dir"]
        raw = str(ref_path or "").strip()
        if not raw:
            raise util.PathForbidden("空远端路径")
        if "\x00" in raw:
            raise util.PathForbidden("远端路径包含 NUL 字节")
        candidate = raw.replace("\\", "/")
        if not candidate.startswith("/"):
            candidate = posixpath.join(base, candidate)
        normalized = posixpath.normpath(candidate)
        if normalized != base and not normalized.startswith(base + "/"):
            raise util.PathForbidden("远端路径不在 base_dir 之下: {0}".format(raw))
        if normalized == base:
            raise util.PathForbidden("远端路径不能是 base_dir 本身")
        return normalized

    # ---------------------------------------------------------------- 契约

    def discover(self, query: Dict[str, Any]) -> List[AssetRef]:
        if not self._probe().available:
            return []
        query = dict(query or {})
        paths = query.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        paths = list(paths or [])
        if query.get("path"):
            paths.append(query["path"])
        priority = parse_int(query.get("priority"), 10)
        refs: List[AssetRef] = []
        for item in paths:
            try:
                remote = self.resolve_remote(item)
            except util.PathForbidden as exc:
                self.last_error = util.redact(exc)
                continue
            settings = self.settings()
            refs.append(
                AssetRef(
                    type=KIND_NODE_STORAGE,
                    filename=posixpath.basename(remote),
                    ref=remote,
                    priority=priority,
                    extra={"node": settings["host"]},
                )
            )
        return refs

    def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult:
        status = self._probe()
        if not status.available:
            return self.blocked_result(status.detail)
        ssh_pass = (os.environ.get(PASSWORD_ENV) or "").strip()
        if not ssh_pass:
            return self.blocked_result("缺少环境变量 " + PASSWORD_ENV)
        try:
            remote = self.resolve_remote(ref.ref)
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        settings = self.settings()
        sshpass = util.which("sshpass")
        scp = util.which("scp")
        if not sshpass or not scp:
            return self.blocked_result("缺少 sshpass/scp 可执行文件")
        try:
            filename = util.safe_filename(ref.filename or posixpath.basename(remote))
            dest_root = Path(str(dest_dir))
            dest_root.mkdir(parents=True, exist_ok=True)
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        except OSError as exc:
            return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
        staged = dest_root / (".scp-" + filename)
        env = os.environ.copy()
        # 密码只经环境变量交给 sshpass -e，不进入 argv
        env["SSHPASS"] = ssh_pass
        cmd = [
            sshpass,
            "-e",
            scp,
            "-P",
            str(settings["port"]),
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=no",
            "{0}@{1}:{2}".format(settings["user"], settings["host"], remote),
            str(staged),
        ]
        rc, _out, err = util.run_cmd(cmd, timeout=self.timeout(), env=env)
        if rc == 124:
            util.info("node-storage: scp 超时")
            self._cleanup(staged)
            return FetchResult.failure("TIMEOUT", util.EXIT_NETWORK, "scp 超时")
        if rc == 127:
            self._cleanup(staged)
            return self.blocked_result("缺少 scp/sshpass 可执行文件")
        if rc != 0 or not staged.is_file():
            self._cleanup(staged)
            return FetchResult.failure(
                "SCP_FAILED",
                util.EXIT_NO_SOURCE,
                util.redact(err.strip() or "scp 未产出文件（rc={0}）".format(rc)),
            )
        result = stage_into(staged, dest_root, ref)
        self._cleanup(staged)
        if not result.ok:
            return result
        return FetchResult(
            ok=True,
            path=result.path,
            sha256=result.sha256,
            bytes=result.bytes,
            detail="来源 {0}:{1}".format(settings["host"], remote),
        )

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
