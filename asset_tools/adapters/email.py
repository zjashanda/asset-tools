"""``email`` 适配器：委托 ``qw-email-handle`` 取企业邮箱附件。

本仓**不实现** IMAP/SMTP，也不读取、传递或打印邮箱凭据：``EMAIL_ACCOUNT`` /
``EMAIL_PASSWORD`` 由被委托的 CLI 自己从环境变量读取。

委托协议
--------
``<cli> list --json``::

    [{"id": "...", "subject": "...", "from": "...", "received_at": "...",
      "filename": "...", "bytes": 123, "sha256": "..."}]

``<cli> get <id> --out <DIR>``：把附件落到 ``<DIR>``，stdout 打印
``{"path": "..."}``（或 0 退出且目录内出现同名文件）。

CLI 定位：``ASSET_EMAIL_CLI``（绝对路径或 PATH 命令名）→ 配置 ``cli`` →
``qw-email-handle``。找不到即 ``BLOCKED``。
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
    KIND_EMAIL,
    Adapter,
    AdapterStatus,
    AssetRef,
    FetchResult,
    blocked_status,
    ok_status,
    parse_int,
    stage_into,
)

DEFAULT_CLI = "qw-email-handle"


class EmailAdapter(Adapter):
    name = KIND_EMAIL
    kind = KIND_EMAIL

    def probe(self) -> AdapterStatus:
        cli = self._cli()
        if not cli:
            return blocked_status(
                self.name,
                [],
                "缺少委托 CLI：ASSET_EMAIL_CLI 未设置且 PATH 中没有 {0}".format(DEFAULT_CLI),
            )
        return ok_status(self.name, "委托 CLI: {0}".format(os.path.basename(cli)))

    # ---------------------------------------------------------------- 内部

    def _cli(self) -> str:
        candidate = os.environ.get("ASSET_EMAIL_CLI") or self.config_value("cli")
        return util.resolve_cli(candidate, DEFAULT_CLI) or ""

    def _run(self, cli: str, args: List[str]) -> Any:
        return util.run_cmd([cli] + args, timeout=self.timeout())

    def _list(self, cli: str) -> List[Dict[str, Any]]:
        rc, out, err = self._run(cli, ["list", "--json"])
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
            data = data.get("messages") or data.get("items") or data.get("attachments") or []
        items = [item for item in data if isinstance(item, dict)]
        return items

    # ---------------------------------------------------------------- 契约

    def discover(self, query: Dict[str, Any]) -> List[AssetRef]:
        status = self._probe()
        if not status.available:
            return []
        cli = self._cli()
        query = dict(query or {})
        subject_filter = str(query.get("subject_contains") or "").lower()
        file_filter = str(query.get("filename_glob") or "")
        priority = parse_int(query.get("priority"), 30)
        refs: List[AssetRef] = []
        for item in self._list(cli):
            filename = str(item.get("filename") or "")
            if not filename:
                continue
            subject = str(item.get("subject") or "")
            if subject_filter and subject_filter not in subject.lower():
                continue
            if file_filter and not fnmatch.fnmatch(filename, file_filter):
                continue
            refs.append(
                AssetRef(
                    type=KIND_EMAIL,
                    filename=filename,
                    ref=str(item.get("id") or ""),
                    sha256=str(item.get("sha256") or "").strip().lower(),
                    bytes=parse_int(item.get("bytes"), 0),
                    priority=priority,
                    extra={
                        "origin": "email",
                        "subject": subject,
                        "from": str(item.get("from") or ""),
                        "received_at": str(item.get("received_at") or ""),
                    },
                )
            )
        return refs

    def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult:
        status = self._probe()
        if not status.available:
            return self.blocked_result(status.detail)
        cli = self._cli()
        attachment_id = str(ref.ref or "").strip()
        if not attachment_id:
            return FetchResult.failure("USAGE", util.EXIT_USAGE, "邮件附件缺少 id")
        try:
            filename = util.safe_filename(ref.filename or "attachment.bin")
            staging = Path(str(dest_dir)) / ".email-staging"
            staging.mkdir(parents=True, exist_ok=True)
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        except OSError as exc:
            return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
        workdir = ""
        try:
            workdir = tempfile.mkdtemp(dir=str(staging), prefix="get-")
            rc, out, err = self._run(cli, ["get", attachment_id, "--out", workdir])
            if rc == 124:
                return FetchResult.failure("TIMEOUT", util.EXIT_NETWORK, "get 超时")
            if rc != 0:
                return FetchResult.failure(
                    "EMAIL_GET_FAILED",
                    util.EXIT_NO_SOURCE,
                    util.redact(err.strip() or "get 失败 rc={0}".format(rc)),
                )
            produced = self._locate(out, Path(workdir), filename)
            if produced is None:
                return FetchResult.failure(
                    "EMAIL_NO_ATTACHMENT", util.EXIT_NO_SOURCE, "委托 CLI 未产出附件文件"
                )
            return stage_into(produced, dest_dir, ref)
        finally:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
            try:
                staging.rmdir()
            except OSError:
                pass

    @staticmethod
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
