"""``ark.asset-manifest.v1`` 清单模型与三种旧 schema 的互转。

本模块只处理数据结构与校验，**不做**任何网络 / 磁盘取件动作。
"""

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import util

SCHEMA = "ark.asset-manifest.v1"
SCHEMA_ARK_MESH = "ark-artifact-manifest.v1"
SCHEMA_VENUSAHISIBURNER = "venusahisiburner-assets/v1"

KINDS = ("firmware", "tool", "test_material", "dataset", "other")
STATUSES = ("verified", "unverified", "missing")
SOURCE_TYPES = ("local", "http", "oss", "node-storage", "email", "nas")
DEFAULT_PRIORITY = 100

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

#: 已知的 Asset 字段；其余键原样保留在 ``Asset.extra``（向前兼容）
_ASSET_FIELDS = (
    "id",
    "kind",
    "name",
    "version",
    "filename",
    "bytes",
    "sha256",
    "project",
    "sources",
    "install",
    "provenance",
    "status",
)


def cache_rel_for(sha256: str, filename: str) -> str:
    """内容寻址相对路径：``sha256/<前2>/<次2>/<sha256>/<filename>``。"""
    digest = str(sha256 or "").lower()
    name = str(filename or "")
    if not SHA256_RE.match(digest):
        raise ValueError("sha256 非法: {0!r}".format(sha256))
    if not name:
        raise ValueError("filename 为空，无法计算 cache_rel")
    return "sha256/{0}/{1}/{2}/{3}".format(digest[:2], digest[2:4], digest, name)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _source_key(source: Dict[str, Any]) -> tuple:
    return (
        str(source.get("type") or ""),
        str(source.get("node") or ""),
        str(source.get("path") or source.get("key") or source.get("url") or source.get("id") or ""),
    )


def normalize_source(source: Any) -> Dict[str, Any]:
    """把来源条目规整成 dict，priority 转 int（缺省 100）。"""
    if not isinstance(source, dict):
        raise ValueError("source 必须是对象: {0!r}".format(source))
    item = dict(source)
    item["type"] = str(item.get("type") or "").strip()
    item["priority"] = _as_int(item.get("priority", DEFAULT_PRIORITY), DEFAULT_PRIORITY)
    return item


def merge_sources(*groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并来源：按 (type, node, path) 去重，priority 取最小值。"""
    merged: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for group in groups:
        for raw in group or []:
            try:
                source = normalize_source(raw)
            except ValueError:
                continue
            key = _source_key(source)
            if key in merged:
                merged[key]["priority"] = min(
                    _as_int(merged[key].get("priority"), DEFAULT_PRIORITY),
                    _as_int(source.get("priority"), DEFAULT_PRIORITY),
                )
                for extra_key, extra_val in source.items():
                    merged[key].setdefault(extra_key, extra_val)
            else:
                merged[key] = source
                order.append(key)
    return [merged[key] for key in order]


@dataclass
class Asset:
    """单个资产条目（字段见 SPEC 3）。"""

    id: str
    kind: str = "other"
    name: str = ""
    version: str = ""
    filename: str = ""
    bytes: int = 0
    sha256: str = ""
    project: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    install: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    status: str = "unverified"
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        self.kind = str(self.kind or "other").strip() or "other"
        self.name = str(self.name or self.id)
        self.version = str(self.version or "")
        self.filename = str(self.filename or "")
        self.sha256 = str(self.sha256 or "").strip().lower()
        self.project = str(self.project or "")
        self.bytes = _as_int(self.bytes, 0)
        self.status = str(self.status or "unverified").strip() or "unverified"
        self.sources = [normalize_source(item) for item in (self.sources or [])]
        self.install = dict(self.install or {})
        self.provenance = dict(self.provenance or {})
        self.extra = dict(self.extra or {})
        if self.filename:
            self.install.setdefault("material_rel", self.filename)
            if not self.install.get("cache_rel") and SHA256_RE.match(self.sha256):
                self.install["cache_rel"] = cache_rel_for(self.sha256, self.filename)

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Asset":
        if not isinstance(data, dict):
            raise ValueError("asset 必须是对象: {0!r}".format(data))
        known = {key: data[key] for key in _ASSET_FIELDS if key in data}
        known.setdefault("id", data.get("id") or data.get("name") or "")
        extra = {key: val for key, val in data.items() if key not in _ASSET_FIELDS}
        known["extra"] = extra
        return cls(**known)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "filename": self.filename,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "project": self.project,
            "sources": [dict(item) for item in self.sources],
            "install": dict(self.install),
            "provenance": dict(self.provenance),
            "status": self.status,
        }
        for key, val in self.extra.items():
            if key not in payload:
                payload[key] = copy.deepcopy(val)
        return payload

    # ---------------------------------------------------------------- 视图

    @property
    def material_rel(self) -> str:
        return str(self.install.get("material_rel") or self.filename or self.id)

    @property
    def cache_rel(self) -> str:
        value = str(self.install.get("cache_rel") or "")
        if value:
            return value
        if SHA256_RE.match(self.sha256) and self.filename:
            return cache_rel_for(self.sha256, self.filename)
        return ""

    def ordered_sources(self) -> List[Dict[str, Any]]:
        return sorted(
            self.sources,
            key=lambda item: (_as_int(item.get("priority"), DEFAULT_PRIORITY), _source_key(item)),
        )

    def isEmpty(self) -> bool:  # noqa: N802 - 兼容旧调用方命名
        return not (self.id or self.filename)


@dataclass
class AssetManifest:
    """``ark.asset-manifest.v1`` 顶层容器。"""

    assets: List[Asset] = field(default_factory=list)
    generated_at: str = ""
    schema: str = SCHEMA
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.assets = [
            item if isinstance(item, Asset) else Asset.from_dict(item) for item in (self.assets or [])
        ]
        if not self.generated_at:
            self.generated_at = util.format_display()
        self.schema = str(self.schema or SCHEMA)

    # ---------------------------------------------------------------- 读写

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssetManifest":
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON 对象")
        schema = str(data.get("schema") or SCHEMA).strip()
        if schema != SCHEMA:
            raise ValueError("不支持的 schema: {0!r}（请用 load_any 做旧 schema 转换）".format(schema))
        extra = {
            key: copy.deepcopy(val)
            for key, val in data.items()
            if key not in ("schema", "generated_at", "assets")
        }
        return cls(
            assets=[Asset.from_dict(item) for item in (data.get("assets") or [])],
            generated_at=str(data.get("generated_at") or ""),
            schema=schema,
            extra=extra,
        )

    @classmethod
    def load(cls, path: Any) -> "AssetManifest":
        """只接受 canonical schema；旧 schema 请用 :func:`load_any`。"""
        return cls.from_dict(util.read_json(path))

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "assets": [asset.to_dict() for asset in self.assets],
        }
        for key, val in self.extra.items():
            if key not in payload:
                payload[key] = copy.deepcopy(val)
        return payload

    def dumps(self) -> str:
        return util.dumps(self.to_dict())

    def save(self, path: Any) -> Path:
        return util.atomic_write_json(path, self.to_dict())

    # ---------------------------------------------------------------- 查询

    def by_id(self, asset_id: str) -> Optional[Asset]:
        target = str(asset_id or "")
        for asset in self.assets:
            if asset.id == target:
                return asset
        return None

    def ids(self) -> List[str]:
        return [asset.id for asset in self.assets]

    def add(self, asset: Asset) -> Asset:
        existing = self.by_id(asset.id)
        if existing is None:
            self.assets.append(asset)
            return asset
        merged = merge_sources(existing.sources, asset.sources)
        existing.sources = merged
        if asset.sha256 and asset.sha256 != existing.sha256:
            existing.sha256 = asset.sha256
            existing.bytes = asset.bytes or existing.bytes
        return existing

    # ---------------------------------------------------------------- 校验

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.schema != SCHEMA:
            errors.append("schema 必须为 {0}，实际 {1!r}".format(SCHEMA, self.schema))
        if not isinstance(self.generated_at, str) or not self.generated_at.strip():
            errors.append("generated_at 不能为空")
        else:
            try:
                util.parse_time(self.generated_at)
            except ValueError:
                errors.append("generated_at 不是合法时间: {0!r}".format(self.generated_at))
        seen = set()
        for index, asset in enumerate(self.assets):
            where = "assets[{0}]".format(index)
            if not asset.id:
                errors.append(where + ".id 不能为空")
            elif asset.id in seen:
                errors.append(where + ".id 重复: {0}".format(asset.id))
            else:
                seen.add(asset.id)
            if not asset.kind:
                errors.append(where + ".kind 不能为空")
            elif asset.kind not in KINDS:
                errors.append(where + ".kind 不在枚举内: {0!r}".format(asset.kind))
            if not asset.filename:
                errors.append(where + ".filename 不能为空")
            elif "/" in asset.filename or "\\" in asset.filename:
                errors.append(where + ".filename 不得包含路径分隔符: {0!r}".format(asset.filename))
            if not SHA256_RE.match(asset.sha256):
                errors.append(where + ".sha256 必须是 64 位小写十六进制")
            if asset.status not in STATUSES:
                errors.append(where + ".status 不在枚举内: {0!r}".format(asset.status))
            if asset.bytes < 0:
                errors.append(where + ".bytes 不得为负")
            if SHA256_RE.match(asset.sha256) and asset.filename:
                expected = cache_rel_for(asset.sha256, asset.filename)
                actual = str(asset.install.get("cache_rel") or "")
                if actual != expected:
                    errors.append(
                        "{0}.install.cache_rel 必须是 {1!r}，实际 {2!r}".format(where, expected, actual)
                    )
            for position, source in enumerate(asset.sources):
                label = "{0}.sources[{1}]".format(where, position)
                stype = str(source.get("type") or "")
                if stype not in SOURCE_TYPES:
                    errors.append(label + ".type 不在枚举内: {0!r}".format(stype))
                priority = source.get("priority", DEFAULT_PRIORITY)
                if isinstance(priority, bool) or not isinstance(priority, int):
                    errors.append(label + ".priority 必须是整数")
                if stype == "local" and not source.get("path"):
                    errors.append(label + " local 缺少 path")
                if stype == "http" and not (source.get("url") or source.get("path")):
                    errors.append(label + " http 缺少 url")
                if stype == "oss" and not source.get("key"):
                    errors.append(label + " oss 缺少 key")
                if stype == "node-storage" and not source.get("path"):
                    errors.append(label + " node-storage 缺少 path")
        return errors

    # ---------------------------------------------------------------- 合并

    def merge(self, other: "AssetManifest", prefer: str = "newest") -> "AssetManifest":
        """按 id 合并；同 sha 合并来源，不同 sha 由 ``prefer`` 决定保留谁。"""
        mode = str(prefer or "newest").strip().lower()
        result: Dict[str, Asset] = {asset.id: copy.deepcopy(asset) for asset in self.assets}
        order: List[str] = [asset.id for asset in self.assets]
        for incoming in other.assets:
            if incoming.id not in result:
                result[incoming.id] = copy.deepcopy(incoming)
                order.append(incoming.id)
                continue
            current = result[incoming.id]
            if current.sha256 and current.sha256 == incoming.sha256:
                current.sources = merge_sources(current.sources, incoming.sources)
                _fill_blanks(current, incoming)
                continue
            winner, loser = _pick_winner(current, incoming, mode)
            merged = copy.deepcopy(winner)
            note = "superseded: {0} sha={1} version={2}".format(
                loser.id, loser.sha256 or "-", loser.version or "-"
            )
            previous = str(merged.provenance.get("note") or "").strip()
            merged.provenance["note"] = note if not previous else note + "; " + previous
            merged.sources = merge_sources(winner.sources, loser.sources)
            if not merged.sha256 and loser.sha256:
                merged.sha256 = loser.sha256
            result[incoming.id] = merged
        return AssetManifest(assets=[result[key] for key in order])

    # ---------------------------------------------------------------- 转换

    def to_ark_artifacts(self) -> Dict[str, Any]:
        """转出 ark-mesh 的 ``ark-artifact-manifest.v1``。"""
        artifacts: List[Dict[str, Any]] = []
        for asset in self.assets:
            node_source = None
            for source in asset.ordered_sources():
                if str(source.get("type")) == "node-storage":
                    node_source = source
                    break
            primary = node_source or (asset.ordered_sources()[0] if asset.sources else {})
            artifacts.append(
                {
                    "name": asset.id,
                    "project": asset.project,
                    "version": asset.version,
                    "source_node": str(primary.get("node") or "") if node_source else "",
                    "source_path": str(
                        primary.get("path")
                        or primary.get("key")
                        or primary.get("url")
                        or asset.filename
                    ),
                    "destination": asset.material_rel,
                    "bytes": asset.bytes,
                    "sha256": asset.sha256,
                    "fetched_at": str(asset.provenance.get("discovered_at") or self.generated_at),
                }
            )
        return {"schema": SCHEMA_ARK_MESH, "artifacts": artifacts, "updated_at": self.generated_at}

    def to_venusahisiburner(self, description: str = "") -> Dict[str, Any]:
        """转出 venusahisiburner 的 ``venusahisiburner-assets/v1``。"""
        assets: List[Dict[str, Any]] = []
        for asset in self.assets:
            oss_source = None
            for source in asset.ordered_sources():
                if str(source.get("type")) == "oss":
                    oss_source = source
                    break
            target_chip = str(
                asset.extra.get("target_chip") or asset.provenance.get("target_chip") or ""
            )
            entry = {
                "id": asset.id,
                "kind": asset.kind,
                "target_chip": target_chip,
                "version": asset.version,
                "file": asset.filename or asset.material_rel,
                "bytes": asset.bytes,
                "sha256": asset.sha256,
                "oss": {"key": str(oss_source.get("key") or "")} if oss_source else {},
                "install": {
                    "material_rel": asset.material_rel,
                    "cache_rel": asset.cache_rel,
                },
                "provenance": {
                    "source": str(asset.provenance.get("origin") or ""),
                    "note": str(asset.provenance.get("note") or ""),
                },
            }
            assets.append(entry)
        return {"schema": SCHEMA_VENUSAHISIBURNER, "description": description, "assets": assets}


# --------------------------------------------------------------------- 合并辅助


def _cache_time(asset: Asset) -> Optional[Any]:
    for key in ("discovered_at", "fetched_at"):
        raw = asset.provenance.get(key)
        if raw:
            try:
                return util.parse_time(str(raw))
            except ValueError:
                continue
    return None


def _pick_winner(current: Asset, incoming: Asset, mode: str) -> Any:
    if mode in ("self", "existing", "current", "old", "first", "local"):
        return current, incoming
    if mode in ("other", "incoming", "new", "second", "remote"):
        return incoming, current
    if mode != "newest":
        raise ValueError("未知 prefer: {0!r}（可选 newest/self/other）".format(mode))
    current_time = _cache_time(current)
    incoming_time = _cache_time(incoming)
    if current_time is None and incoming_time is None:
        return current, incoming
    if current_time is None:
        return incoming, current
    if incoming_time is None:
        return current, incoming
    return (current, incoming) if current_time >= incoming_time else (incoming, current)


def _fill_blanks(target: Asset, source: Asset) -> None:
    for attr in ("name", "version", "project", "filename"):
        if not getattr(target, attr) and getattr(source, attr):
            setattr(target, attr, getattr(source, attr))
    for key, val in source.install.items():
        target.install.setdefault(key, val)
    for key, val in source.provenance.items():
        target.provenance.setdefault(key, val)
    target.bytes = target.bytes or source.bytes


# --------------------------------------------------------------------- 旧 schema


def _dedupe_id(base: str, used: Dict[str, int], fallback: str = "asset") -> str:
    candidate = util.slugify(base, fallback=fallback)
    if candidate not in used:
        used[candidate] = 1
        return candidate
    used[candidate] += 1
    while "{0}-{1}".format(candidate, used[candidate]) in used:
        used[candidate] += 1
    final = "{0}-{1}".format(candidate, used[candidate])
    used[final] = 1
    return final


def from_ark_mesh(data: Dict[str, Any]) -> AssetManifest:
    """把 ark-mesh ``ark-artifact-manifest.v1`` 转成 canonical manifest。"""
    used: Dict[str, int] = {}
    assets: List[Asset] = []
    for raw in data.get("artifacts") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("filename") or raw.get("source_path") or "artifact")
        filename = str(
            raw.get("filename") or Path(str(raw.get("source_path") or raw.get("destination") or name)).name
        )
        digest = str(raw.get("sha256") or "").strip().lower()
        source_node = str(raw.get("source_node") or "")
        source_path = str(raw.get("source_path") or "")
        sources: List[Dict[str, Any]] = []
        if source_node or source_path:
            sources.append(
                {
                    "type": "node-storage",
                    "node": source_node,
                    "path": source_path or filename,
                    "priority": 10,
                }
            )
        asset_id = _dedupe_id(str(raw.get("id") or name), used)
        install = {"material_rel": str(raw.get("destination") or filename)}
        if digest and filename:
            install["cache_rel"] = cache_rel_for(digest, filename)
        assets.append(
            Asset(
                id=asset_id,
                kind="other",
                name=name,
                version=str(raw.get("version") or ""),
                filename=filename,
                bytes=_as_int(raw.get("bytes"), 0),
                sha256=digest,
                project=str(raw.get("project") or ""),
                sources=sources,
                install=install,
                provenance={
                    "origin": "node-storage:{0}".format(source_node) if source_node else "ark-mesh",
                    "note": str(raw.get("note") or ""),
                    "discovered_at": str(raw.get("fetched_at") or data.get("updated_at") or ""),
                },
                status="verified" if SHA256_RE.match(digest) else "unverified",
            )
        )
    return AssetManifest(assets=assets, generated_at=str(data.get("updated_at") or ""))


def from_venusahisiburner(data: Dict[str, Any]) -> AssetManifest:
    """把 venusahisiburner ``venusahisiburner-assets/v1`` 转成 canonical manifest。"""
    used: Dict[str, int] = {}
    assets: List[Asset] = []
    for raw in data.get("assets") or []:
        if not isinstance(raw, dict):
            continue
        filename = str(raw.get("file") or raw.get("filename") or "")
        digest = str(raw.get("sha256") or "").strip().lower()
        oss_block = raw.get("oss") if isinstance(raw.get("oss"), dict) else {}
        legacy_install = raw.get("install") if isinstance(raw.get("install"), dict) else {}
        provenance_in = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
        sources: List[Dict[str, Any]] = []
        if oss_block.get("key"):
            sources.append({"type": "oss", "key": str(oss_block["key"]), "priority": 20})
        install = {"material_rel": str(legacy_install.get("material_rel") or filename)}
        if digest and filename:
            install["cache_rel"] = cache_rel_for(digest, filename)
        asset_id = _dedupe_id(str(raw.get("id") or filename), used)
        extra: Dict[str, Any] = {}
        if raw.get("target_chip"):
            extra["target_chip"] = str(raw["target_chip"])
        if legacy_install.get("cache_rel"):
            extra["legacy_cache_rel"] = str(legacy_install["cache_rel"])
        assets.append(
            Asset(
                id=asset_id,
                kind=str(raw.get("kind") or "other"),
                name=asset_id,
                version=str(raw.get("version") or ""),
                filename=filename,
                bytes=_as_int(raw.get("bytes"), 0),
                sha256=digest,
                project=str(raw.get("project") or ""),
                sources=sources,
                install=install,
                provenance={
                    "origin": "venusahisiburner-assets/v1",
                    "note": str(provenance_in.get("note") or ""),
                    "source": str(provenance_in.get("source") or ""),
                    "discovered_at": str(raw.get("discovered_at") or data.get("updated_at") or ""),
                },
                status="verified" if SHA256_RE.match(digest) else "unverified",
                extra=extra,
            )
        )
    return AssetManifest(assets=assets, generated_at=str(data.get("updated_at") or ""))


def load_any(path: Any) -> AssetManifest:
    """自动识别三种 schema 并转换（canonical 原样返回）。"""
    data = util.read_json(path)
    if not isinstance(data, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    schema = str(data.get("schema") or "").strip()
    if schema == SCHEMA:
        return AssetManifest.from_dict(data)
    if schema == SCHEMA_ARK_MESH or (not schema and "artifacts" in data):
        return from_ark_mesh(data)
    if schema == SCHEMA_VENUSAHISIBURNER or "venusahisiburner" in schema:
        return from_venusahisiburner(data)
    if schema.startswith("ark.artifact-manifest") or "artifact-manifest" in schema:
        return from_ark_mesh(data)
    if not schema and "assets" in data:
        return AssetManifest.from_dict(dict(data, schema=SCHEMA))
    raise ValueError("未知 manifest schema: {0!r}".format(schema or "<missing>"))
