"""asset-tools：方舟资产供应层（发现 / 清单 / 校验 / 内容寻址缓存 / 供应）。

本包只包含脚本、schema 与测试，不含任何凭据；凭据一律由环境变量或受控
本机配置提供。
"""

import os
from pathlib import Path

__all__ = ["__version__", "VERSION"]

_FALLBACK_VERSION = "0.1.0"


def _read_version() -> str:
    for candidate in (
        Path(__file__).resolve().parent.parent / "VERSION",
        Path(os.environ.get("ARK_ASSET_TOOLS_VERSION_FILE") or "").expanduser(),
    ):
        try:
            if candidate and candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text.splitlines()[0].strip()
        except OSError:
            continue
    return _FALLBACK_VERSION


__version__ = _read_version()
VERSION = __version__
