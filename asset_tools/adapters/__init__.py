"""适配器注册表。

``get_adapter(name, config=None)`` 未知名字抛 :class:`KeyError`（CLI 转 ``EXIT_USAGE``），
``probe_all()`` 返回全部适配器状态（``probe`` 永不抛异常）。
"""

from typing import Any, Dict, List, Optional

from .base import (
    BLOCKED,
    KIND_EMAIL,
    KIND_HTTP,
    KIND_LOCAL,
    KIND_NAS,
    KIND_NODE_STORAGE,
    KIND_OSS,
    Adapter,
    AdapterStatus,
    AssetRef,
    FetchResult,
    blocked_status,
    ok_status,
)
from .email import EmailAdapter
from .http import HttpAdapter
from .local import LocalAdapter
from .nas import NasAdapter
from .node_storage import NodeStorageAdapter
from .oss import OssAdapter

ADAPTERS: Dict[str, Any] = {
    KIND_LOCAL: LocalAdapter,
    KIND_HTTP: HttpAdapter,
    KIND_OSS: OssAdapter,
    KIND_NODE_STORAGE: NodeStorageAdapter,
    KIND_EMAIL: EmailAdapter,
    KIND_NAS: NasAdapter,
}

ADAPTER_NAMES = tuple(ADAPTERS.keys())


def get_adapter(name: Any, config: Optional[Dict[str, Any]] = None) -> Adapter:
    """按名字取适配器实例；未知名字抛 ``KeyError``。"""
    key = str(name or "").strip()
    if key not in ADAPTERS:
        raise KeyError("未知适配器: {0!r}（可用: {1}）".format(key, ", ".join(ADAPTER_NAMES)))
    return ADAPTERS[key](config)


def probe_all(configs: Optional[Dict[str, Dict[str, Any]]] = None) -> List[AdapterStatus]:
    """探测全部适配器；单个适配器失败不影响其它。"""
    mapping = configs or {}
    statuses: List[AdapterStatus] = []
    for name in ADAPTER_NAMES:
        try:
            adapter = get_adapter(name, mapping.get(name))
            statuses.append(adapter._probe())
        except KeyError:
            statuses.append(blocked_status(name, [], "适配器未注册"))
    return statuses


__all__ = [
    "ADAPTERS",
    "ADAPTER_NAMES",
    "Adapter",
    "AdapterStatus",
    "AssetRef",
    "FetchResult",
    "get_adapter",
    "probe_all",
    "blocked_status",
    "ok_status",
    "BLOCKED",
]
