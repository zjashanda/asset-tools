"""``http`` 适配器：直链下载（``urllib.request``，仅标准库）。

契约要点
--------
* 只允许 ``http`` / ``https``；其他 scheme（含 ``file://``）→ ``FORBIDDEN``。
* 重定向后重新校验 scheme，防止跳到 ``file://``。
* 4xx（408/429 除外）→ ``EXIT_NO_SOURCE``；5xx / 连接错误 / 超时 → ``EXIT_NETWORK``。
* ``Content-Length`` 与实收字节不符只告警，**以 sha256 为准**。
* 先写临时文件，校验通过再原子 rename；任何提前返回都会清理临时文件。
"""

import hashlib
import http.client
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from .. import util
from .base import (
    DEFAULT_HTTP_TIMEOUT,
    KIND_HTTP,
    Adapter,
    AdapterStatus,
    AssetRef,
    FetchResult,
    ok_status,
    parse_int,
)

ALLOWED_SCHEMES = ("http", "https")
USER_AGENT = "asset-tools/0.1 (+ark asset supply layer)"
CHUNK = 64 * 1024


class HttpAdapter(Adapter):
    name = KIND_HTTP
    kind = KIND_HTTP

    def probe(self) -> AdapterStatus:
        timeout = self._timeout()
        return ok_status(
            self.name,
            "urllib 直链；超时 {0:g}s；仅允许 {1}".format(timeout, "/".join(ALLOWED_SCHEMES)),
        )

    # ---------------------------------------------------------------- 内部

    def _timeout(self) -> float:
        return util.env_float("ASSET_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT)

    @staticmethod
    def _scheme(url: str) -> str:
        return str(urllib.parse.urlparse(url).scheme or "").lower()

    # ---------------------------------------------------------------- 契约

    def discover(self, query: Dict[str, Any]) -> List[AssetRef]:
        query = dict(query or {})
        urls = query.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        urls = list(urls or [])
        if query.get("url"):
            urls.append(query["url"])
        priority = parse_int(query.get("priority"), 20)
        refs: List[AssetRef] = []
        for url in urls:
            text = str(url).strip()
            if self._scheme(text) not in ALLOWED_SCHEMES:
                self.last_error = "非 http(s) URL 已忽略"
                continue
            filename = os.path.basename(urllib.parse.urlparse(text).path) or "download.bin"
            refs.append(
                AssetRef(
                    type=KIND_HTTP,
                    filename=filename,
                    ref=text,
                    priority=priority,
                    extra={"origin": "http"},
                )
            )
        return refs

    def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult:
        url = str(ref.ref or "").strip()
        if self._scheme(url) not in ALLOWED_SCHEMES:
            return FetchResult.failure(
                "FORBIDDEN", util.EXIT_FORBIDDEN, "只允许 http/https: {0}".format(url)
            )
        try:
            filename = util.safe_filename(ref.filename or os.path.basename(url) or "download.bin")
        except util.PathForbidden as exc:
            return FetchResult.failure("FORBIDDEN", util.EXIT_FORBIDDEN, str(exc))
        target_root = Path(str(dest_dir))
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp = tempfile.mkstemp(dir=str(target_root), prefix=".http-")
            os.close(handle_fd)
        except OSError as exc:
            return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
        try:
            return self._download(ref, url, Path(tmp), target_root / filename)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _download(self, ref: AssetRef, url: str, tmp: Path, target: Path) -> FetchResult:
        timeout = self._timeout()
        digest = hashlib.sha256()
        total = 0
        declared = ""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                code = int(getattr(exc, "code", 0) or 0)
                if code in (408, 429):
                    return FetchResult.failure(
                        "HTTP_TRANSIENT", util.EXIT_NETWORK, "HTTP {0}".format(code)
                    )
                if 400 <= code < 500:
                    reason = "HTTP_NOT_FOUND" if code == 404 else "HTTP_CLIENT_ERROR"
                    return FetchResult.failure(reason, util.EXIT_NO_SOURCE, "HTTP {0}".format(code))
                return FetchResult.failure(
                    "HTTP_SERVER_ERROR", util.EXIT_NETWORK, "HTTP {0}".format(code)
                )
            except urllib.error.URLError as exc:
                return FetchResult.failure(
                    "NETWORK_ERROR", util.EXIT_NETWORK, str(getattr(exc, "reason", exc))
                )
            except (TimeoutError, OSError) as exc:
                return FetchResult.failure("NETWORK_ERROR", util.EXIT_NETWORK, str(exc))
            try:
                final_url = str(response.geturl() or url)
                if self._scheme(final_url) not in ALLOWED_SCHEMES:
                    return FetchResult.failure(
                        "FORBIDDEN", util.EXIT_FORBIDDEN, "重定向到非 http(s): {0}".format(final_url)
                    )
                declared = str(response.headers.get("Content-Length") or "")
                with open(str(tmp), "wb") as handle:
                    while True:
                        try:
                            block = response.read(CHUNK)
                        except http.client.IncompleteRead as exc:
                            # 服务器声明的 Content-Length 偏大：收下已有字节，
                            # 由 sha256 决定成败（SPEC 5.2）
                            partial = exc.partial or b""
                            if partial:
                                handle.write(partial)
                                digest.update(partial)
                                total += len(partial)
                            break
                        if not block:
                            break
                        handle.write(block)
                        digest.update(block)
                        total += len(block)
            finally:
                try:
                    response.close()
                except OSError:
                    pass
        except http.client.HTTPException as exc:
            return FetchResult.failure("HTTP_PROTOCOL_ERROR", util.EXIT_NETWORK, str(exc))
        except (TimeoutError, OSError) as exc:
            return FetchResult.failure("NETWORK_ERROR", util.EXIT_NETWORK, str(exc))
        actual = digest.hexdigest()
        warning = ""
        if declared.strip().isdigit() and int(declared) != total:
            warning = "Content-Length={0} 与实收 {1} 不符（以 sha256 为准）".format(declared, total)
        if ref.sha256 and actual != ref.sha256:
            return FetchResult(
                ok=False,
                sha256=actual,
                bytes=total,
                reason="SHA256_MISMATCH",
                exit_code=util.EXIT_FAIL,
                detail="sha256 不匹配: {0} != {1}".format(actual, ref.sha256),
            )
        try:
            os.replace(str(tmp), str(target))
            try:
                os.chmod(str(target), 0o644)
            except OSError:
                pass
        except OSError as exc:
            return FetchResult.failure("IO_ERROR", util.EXIT_FAIL, util.redact(exc))
        return FetchResult(
            ok=True,
            path=str(target),
            sha256=actual,
            bytes=total,
            detail=util.redact(warning),
        )
