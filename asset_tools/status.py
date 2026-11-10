"""``status``：适配器探测 + 缓存统计 + 默认 manifest 路径。

输出里**只**有变量名、路径与统计，不含任何凭据值。
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import util
from .adapters import probe_all
from .cache import ContentCache
from .manifest import load_any

STATUS_SCHEMA = "ark.status.v1"


def default_manifest_path() -> Path:
    """默认 manifest：``ARK_ASSET_MANIFEST`` → ``~/.config/ark-assets/asset-manifest.json``。"""
    raw = os.environ.get("ARK_ASSET_MANIFEST")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "ark-assets" / "asset-manifest.json"


def manifest_section(path: Any) -> Dict[str, Any]:
    target = Path(str(path)).expanduser()
    section: Dict[str, Any] = {
        "path": str(target),
        "exists": target.is_file(),
        "schema": "",
        "assets": 0,
        "by_kind": {},
        "by_status": {},
        "errors": [],
    }
    if not target.is_file():
        return section
    try:
        manifest = load_any(target)
    except (ValueError, OSError) as exc:
        section["errors"].append(util.redact("读取失败: {0}".format(exc)))
        return section
    section["schema"] = manifest.schema
    section["assets"] = len(manifest.assets)
    by_kind: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for asset in manifest.assets:
        by_kind[asset.kind] = by_kind.get(asset.kind, 0) + 1
        by_status[asset.status] = by_status.get(asset.status, 0) + 1
    section["by_kind"] = by_kind
    section["by_status"] = by_status
    section["generated_at"] = manifest.generated_at
    section["errors"] = manifest.validate()
    return section


def config_section(config_path: Any, configs: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    section: Dict[str, Any] = {
        "path": str(config_path) if config_path else "",
        "adapters": {},
    }
    for name, cfg in (configs or {}).items():
        safe = {key: val for key, val in dict(cfg or {}).items() if key not in _SECRET_KEYS}
        section["adapters"][name] = safe
    return section


_SECRET_KEYS = ("password", "passwd", "secret", "token", "credential", "access_key")


def collect_status(
    cache: Optional[ContentCache] = None,
    manifest_path: Any = None,
    configs: Optional[Dict[str, Dict[str, Any]]] = None,
    config_path: Any = None,
) -> Dict[str, Any]:
    """汇总一次 status 快照。"""
    from . import __version__

    cache_obj = cache or ContentCache()
    target = manifest_path or default_manifest_path()
    statuses = probe_all(configs)
    now = util.now_beijing()
    return {
        "schema": STATUS_SCHEMA,
        "version": __version__,
        "generated_at": util.format_display(now),
        "generated_at_rfc3339": util.format_rfc3339(now),
        "node": util.hostname(),
        "cache": cache_obj.stats(),
        "adapters": [status.to_dict() for status in statuses],
        "manifest": manifest_section(target),
        "config": config_section(config_path, configs),
    }
