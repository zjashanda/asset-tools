"""``oss`` 适配器：OSS 对象取件（不引入 SDK）。

凭据与配置全部来自环境变量，本仓只出现**变量名**：

==================================  ==========================================
变量                                 用途
==================================  ==========================================
``ARK_OSS_ENDPOINT``                 端点（``oss-cn-xxx.aliyuncs.com`` 或完整 URL）
``ARK_OSS_BUCKET``                   bucket 名
``ARK_OSS_ACCESS_KEY_ID``            AccessKeyId
``ARK_OSS_ACCESS_KEY_SECRET``        AccessKeySecret
``ASSET_OSS_CLI``                    可选：委托 CLI 的路径/命令名
==================================  ==========================================

取件优先级：

1. 配了 ``ASSET_OSS_CLI`` 且可执行 → ``<cli> get --bucket B --key K --out PATH``；
2. 否则 ``urllib`` 直链 ``https://<bucket>.<endpoint>/<key>``（仅公开对象或由
   CLI 预签时可用），失败即 ``EXIT_NO_SOURCE``。

**绝不**拼接、打印或落盘任何签名参数。
"""

import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from .. import util
from .base import (
    KIND_OSS,
    Adapter,
    AdapterStatus,
    AssetRef,
    FetchResult,
    blocked_status,
    missing_env,
    ok_status,
    parse_int,
)

REQUIRED_ENV = (
    "ARK_OSS_ENDPOINT",
    "ARK_OSS_BUCKET",
    "ARK_OSS_ACCESS_KEY_ID",
    "ARK_OSS_ACCESS_KEY_SECRET",
)
USER_AGENT = "asset-tools/0.1 (+ark asset supply layer)"


class OssAdapter(Adapter):
    name = KIND_OSS
    kind = KIND_OSS

    def probe(self) -> AdapterStatus:
        absent = missing_env(list(REQUIRED_ENV))
        if absent:
            return blocked_status(
                self.name, absent, "缺少环境变量: " + ", ".join(absent)
            )
        mode = "cli" if self._cli() else "url"
        return ok_status(self.name, "取件方式: {0}".format(mode))

    # ---------------------------------------------------------------- 内部

    def _cli(self) -> str:
        candidate = os.environ.get("ASSET_OSS_CLI") or self.config_value("cli")
        return util.resolve_cli(candidate) or ""

    def _env(self, name: str) -> str:
        return str(os.environ.get(name) or "").strip()

    def _object_url(self, key: str) -> str:
        endpoint = self._env("ARK_OSS_ENDPOINT")
        bucket = self._env("ARK_OSS_BUCKET")
        clean_key = urllib.parse.quote(str(key or "").lstrip("/"), safe="/")
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            base = endpoint.rstrip("/")
        else:
            base = "https://{0}.{1}".format(bucket, endpoint.strip("/"))
        return "{0}/{1}".format(base, clean_key)

    # ---------------------------------------------------------------- 契约

    def discover(self, query: Dict[str, Any]) -> List[AssetRef]:
        if not self._probe().available:
            return []
        query = dict(query or {})
        keys = query.get("keys")
        if isinstance(keys, str):
            keys = [keys]
        keys = list(keys or [])
        if query.get("key"):
            keys.append(query["key"])
        prefix = str(query.get("prefix") or "")
        refs: List[AssetRef] = []
        for key in keys:
            text = str(key).strip().lstrip("/")
            if not text:
                continue
            refs.append(
                AssetRef(
                    type=KIND_OSS,
                    filename=os.path.basename(text),
                    ref=text,
                    priority=parse_int(query.get("priority"), 20),
                    extra={"origin": "oss", "prefix": prefix},
                )
            )
        return refs

    def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult:
        status = self._probe()
        if not status.available:
            return self.blocked_result(status.detail)
        key = str(ref.ref or "").strip().lstrip("/")
        if not key:
            return FetchResult.failure("USAGE", util.EXIT_USAGE, "oss 缺少 object key")
        cli = self._cli()
        if cli:
            return self._fetch_cli(cli, key, ref, dest_dir)
        return self._fetch_url(key, ref, dest_dir)

    def _fetch_cli(self, cli: str, key: str, ref: AssetRef, dest_dir: Path) -> FetchResult:
        bucket = self._env("ARK_OSS_BUCKET")
        try:
            filename = util.safe_filename(ref.filename or os.path.basename(key))
            target_root = Path(str(dest_dir))
            target_root.mkdir(parents=True, exist_ok=True)
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        except OSError as exc:
            return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
        out = target_root / filename
        rc, _out, err = util.run_cmd(
            [cli, "get", "--bucket", bucket, "--key", key, "--out", str(out)],
            timeout=self.timeout(),
        )
        if rc == 124:
            return FetchResult.failure("TIMEOUT", util.EXIT_NETWORK, "委托 CLI 超时")
        if rc != 0 or not out.is_file():
            return FetchResult.failure(
                "OSS_FETCH_FAILED", util.EXIT_NO_SOURCE, util.redact(err or "委托 CLI 未产出文件")
            )
        digest = util.sha256_file(out)
        size = out.stat().st_size
        if ref.sha256 and digest != ref.sha256:
            return FetchResult(
                ok=False,
                sha256=digest,
                bytes=size,
                reason="SHA256_MISMATCH",
                exit_code=util.EXIT_FAIL,
                detail="sha256 不匹配: {0} != {1}".format(digest, ref.sha256),
            )
        return FetchResult(ok=True, path=str(out), sha256=digest, bytes=size)

    def _fetch_url(self, key: str, ref: AssetRef, dest_dir: Path) -> FetchResult:
        try:
            filename = util.safe_filename(ref.filename or os.path.basename(key))
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        url = self._object_url(key)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=self.timeout()) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            return FetchResult.failure(
                "OSS_NOT_FOUND" if code == 404 else "OSS_HTTP_ERROR",
                util.EXIT_NO_SOURCE,
                "HTTP {0}（直链仅适用于公开对象）".format(code),
            )
        except urllib.error.URLError as exc:
            return FetchResult.failure(
                "OSS_UNAVAILABLE", util.EXIT_NO_SOURCE, str(getattr(exc, "reason", exc))
            )
        except (TimeoutError, OSError) as exc:
            return FetchResult.failure("NETWORK_ERROR", util.EXIT_NETWORK, str(exc))
        digest = util.sha256_bytes(data)
        if ref.sha256 and digest != ref.sha256:
            return FetchResult(
                ok=False,
                sha256=digest,
                bytes=len(data),
                reason="SHA256_MISMATCH",
                exit_code=util.EXIT_FAIL,
                detail="sha256 不匹配: {0} != {1}".format(digest, ref.sha256),
            )
        target_root = Path(str(dest_dir))
        tmp = ""
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            target = target_root / filename
            handle_fd, tmp = tempfile.mkstemp(dir=str(target_root), prefix=".oss-")
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, str(target))
        except OSError as exc:
            return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return FetchResult(ok=True, path=str(target), sha256=digest, bytes=len(data))
