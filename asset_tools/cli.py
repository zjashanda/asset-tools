"""命令行入口（SPEC 7）。

约定
----
* ``--json`` 时 stdout **只有** JSON，人类可读信息与错误一律走 stderr。
* 任何写操作在缺 ``--apply`` 时只打印 ``DRY-RUN`` 前缀，JSON 里 ``applied`` 为 ``false``。
* 退出码：参数错 2；资产/清单不存在 3；路径越界 4；网络类 5；其余失败 1。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, util
from .adapters import ADAPTER_NAMES, get_adapter
from .cache import ContentCache
from .manifest import (
    KINDS,
    Asset,
    AssetManifest,
    cache_rel_for,
    load_any,
)
from .status import collect_status, default_manifest_path
from .supply import fetch_to_cache, plan_asset, supply_asset

PROG = "asset-tools"


class UsageError(Exception):
    """参数用法错误（→ EXIT_USAGE）。"""


# --------------------------------------------------------------------- 解析器


def _add_common(parser: argparse.ArgumentParser, suppress: bool) -> None:
    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value

    parser.add_argument(
        "--config",
        metavar="PATH",
        default=default(None),
        help="providers 配置（默认 config/providers.example.json）",
    )
    parser.add_argument(
        "--cache", metavar="PATH", default=default(None), help="缓存根目录（默认 ~/.cache/ark-assets）"
    )
    parser.add_argument("--json", action="store_true", default=default(False), help="输出 JSON")
    parser.add_argument(
        "--apply", action="store_true", default=default(False), help="真正写盘（默认 dry-run）"
    )
    parser.add_argument("--quiet", action="store_true", default=default(False), help="安静模式")
    parser.add_argument("-v", "--verbose", action="count", default=default(0), help="详细日志")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="方舟资产供应层：发现 / 清单 / 校验 / 内容寻址缓存 / 供应",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    _add_common(parser, suppress=False)
    subs = parser.add_subparsers(dest="command", metavar="<command>")

    p = subs.add_parser("status", help="适配器探测 + 缓存统计 + manifest 状态")
    _add_common(p, True)
    p.add_argument("--manifest", metavar="PATH", help="清单路径（默认 ~/.config/ark-assets/asset-manifest.json）")

    p = subs.add_parser("plan", help="只读计划：缓存命中 / 需取件 / 来源顺序")
    _add_common(p, True)
    p.add_argument("--manifest", metavar="PATH", required=True)

    p = subs.add_parser("discover", help="调用适配器发现候选资产（只读）")
    _add_common(p, True)
    p.add_argument("--provider", metavar="NAME", required=True, help="适配器名: " + ", ".join(ADAPTER_NAMES))
    p.add_argument("--query", metavar="KEY=VAL", action="append", default=[], help="发现条件，可重复")

    p = subs.add_parser("register", help="把本地文件登记进 manifest")
    _add_common(p, True)
    p.add_argument("--manifest", metavar="PATH", required=True)
    p.add_argument("--file", metavar="PATH", required=True)
    p.add_argument("--id", metavar="ID", required=True)
    p.add_argument("--kind", metavar="KIND", required=True, help=" / ".join(KINDS))
    p.add_argument("--version", metavar="VER", default="")
    p.add_argument("--project", metavar="NAME", default="")
    p.add_argument("--name", metavar="TEXT", default="")

    p = subs.add_parser("verify", help="逐个校验（缓存或指定目录）")
    _add_common(p, True)
    p.add_argument("--manifest", metavar="PATH", required=True)
    p.add_argument("--asset", metavar="ID", default="")
    p.add_argument("--dir", metavar="PATH", default="")

    p = subs.add_parser("fetch", help="拉取单个资产到缓存")
    _add_common(p, True)
    p.add_argument("--manifest", metavar="PATH", required=True)
    p.add_argument("--asset", metavar="ID", required=True)

    p = subs.add_parser("cache", help="缓存巡检：list / verify / prune")
    _add_common(p, True)
    cache_subs = p.add_subparsers(dest="action", metavar="<action>")
    for action, text in (("list", "列出缓存条目"), ("verify", "校验缓存内容"), ("prune", "按 mtime 清理过期条目")):
        sub = cache_subs.add_parser(action, help=text)
        _add_common(sub, True)
        if action == "prune":
            sub.add_argument("--keep-days", type=int, default=30, metavar="N", help="保留天数（默认 30）")

    p = subs.add_parser("supply", help="缓存 → 目标目录落位 + 供应记录")
    _add_common(p, True)
    p.add_argument("--manifest", metavar="PATH", required=True)
    p.add_argument("--asset", metavar="ID", required=True)
    p.add_argument("--dest", metavar="DIR", required=True, help="目标目录")

    p = subs.add_parser("manifest", help="清单操作：show / validate / convert / merge")
    _add_common(p, True)
    manifest_subs = p.add_subparsers(dest="action", metavar="<action>")
    sub = manifest_subs.add_parser("show", help="打印清单")
    _add_common(sub, True)
    sub.add_argument("--manifest", metavar="PATH", required=True)
    sub = manifest_subs.add_parser("validate", help="校验清单")
    _add_common(sub, True)
    sub.add_argument("--manifest", metavar="PATH", required=True)
    sub = manifest_subs.add_parser("convert", help="转换为旧 schema")
    _add_common(sub, True)
    sub.add_argument("--manifest", metavar="PATH", required=True)
    sub.add_argument("--to", choices=("ark-artifacts", "venusahisiburner"), required=True)
    sub.add_argument("--out", metavar="PATH", default="")
    sub = manifest_subs.add_parser("merge", help="合并两个清单")
    _add_common(sub, True)
    sub.add_argument("--manifest", metavar="PATH", required=True)
    sub.add_argument("--other", metavar="PATH", required=True)
    sub.add_argument("--prefer", choices=("newest", "self", "other"), default="newest")
    sub.add_argument("--out", metavar="PATH", default="")

    return parser


# --------------------------------------------------------------------- 输出


def _is_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _quiet(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "quiet", False))


def _emit(args: argparse.Namespace, payload: Any) -> None:
    if _is_json(args):
        util.emit(util.redact_obj(payload))


def _say(args: argparse.Namespace, message: str) -> None:
    if _quiet(args) or _is_json(args):
        return
    sys.stdout.write(util.redact(message) + "\n")


def _table(args: argparse.Namespace, headers: List[str], rows: List[List[Any]]) -> None:
    if _quiet(args) or _is_json(args):
        return
    text_rows = [[util.redact("" if cell is None else cell) for cell in row] for row in rows]
    widths = [len(head) for head in headers]
    for row in text_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    line = "  ".join(head.ljust(widths[index]) for index, head in enumerate(headers))
    sys.stdout.write(line.rstrip() + "\n")
    sys.stdout.write("  ".join("-" * width for width in widths) + "\n")
    for row in text_rows:
        sys.stdout.write(
            "  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)).rstrip() + "\n"
        )


# --------------------------------------------------------------------- 公共加载


def _load_config(args: argparse.Namespace) -> Tuple[Dict[str, Any], Optional[str]]:
    raw = getattr(args, "config", None)
    if raw:
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            raise UsageError("配置文件不存在: {0}".format(path))
        data = util.read_json(path)
        if not isinstance(data, dict):
            raise UsageError("配置文件必须是 JSON 对象: {0}".format(path))
        return data, str(path)
    default = util.repo_root() / "config" / "providers.example.json"
    if default.is_file():
        try:
            data = util.read_json(default)
        except (ValueError, OSError):
            return {}, str(default)
        if isinstance(data, dict):
            return data, str(default)
    return {}, None


def _adapter_configs(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = config.get("adapters")
    if not isinstance(raw, dict):
        return {}
    return {str(key): (value if isinstance(value, dict) else {}) for key, value in raw.items()}


def _cache(args: argparse.Namespace, config: Dict[str, Any]) -> ContentCache:
    explicit = getattr(args, "cache", None)
    if explicit:
        return ContentCache(explicit)
    configured = (config.get("cache") or {}).get("root") if isinstance(config.get("cache"), dict) else None
    return ContentCache(configured or None)


def _manifest_path(args: argparse.Namespace) -> Path:
    raw = getattr(args, "manifest", None)
    return Path(str(raw)).expanduser() if raw else default_manifest_path()


def _load_manifest(args: argparse.Namespace) -> AssetManifest:
    path = _manifest_path(args)
    if not path.is_file():
        raise FileNotFoundError("清单不存在: {0}".format(path))
    return load_any(path)


def _parse_query(pairs: List[str]) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    for raw in pairs or []:
        text = str(raw)
        if "=" not in text:
            raise UsageError("--query 需要 KEY=VAL 形式: {0!r}".format(text))
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            raise UsageError("--query 的 KEY 不能为空: {0!r}".format(text))
        if key == "priority":
            try:
                query[key] = int(value)
                continue
            except ValueError:
                raise UsageError("priority 必须是整数: {0!r}".format(value))
        query[key] = value
    return query


# --------------------------------------------------------------------- 子命令


def cmd_status(args: argparse.Namespace) -> int:
    config, config_path = _load_config(args)
    cache = _cache(args, config)
    payload = collect_status(
        cache=cache,
        manifest_path=_manifest_path(args) if getattr(args, "manifest", None) else None,
        configs=_adapter_configs(config),
        config_path=config_path,
    )
    _emit(args, payload)
    rows = []
    for item in payload["adapters"]:
        rows.append(
            [
                item["name"],
                "AVAILABLE" if item["available"] else "BLOCKED",
                item["reason"],
                ",".join(item.get("missing_env") or []) or "-",
            ]
        )
    _table(args, ["adapter", "state", "reason", "missing_env"], rows)
    stats = payload["cache"]
    _say(
        args,
        "cache: {0} 条 / {1}（{2}）".format(
            stats["entries"], util.human_bytes(stats["total_bytes"]), stats["root"]
        ),
    )
    manifest = payload["manifest"]
    _say(
        args,
        "manifest: {0}（{1}）".format(
            manifest["path"], "存在" if manifest["exists"] else "不存在"
        ),
    )
    return util.EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    config, _config_path = _load_config(args)
    cache = _cache(args, config)
    configs = _adapter_configs(config)
    manifest = _load_manifest(args)
    errors = manifest.validate()
    entries = [plan_asset(manifest, asset, cache, configs) for asset in manifest.assets]
    summary = {
        "assets": len(entries),
        "use_cache": sum(1 for item in entries if item["action"] == "use-cache"),
        "to_fetch": sum(1 for item in entries if item["action"] == "fetch"),
        "no_source": sum(1 for item in entries if item["action"] == "no-source"),
    }
    payload = {
        "schema": "ark.plan.v1",
        "generated_at": util.format_display(),
        "applied": False,
        "manifest": str(_manifest_path(args)),
        "assets": entries,
        "summary": summary,
        "errors": errors,
    }
    _emit(args, payload)
    rows = []
    for item in entries:
        first = item["sources"][0] if item["sources"] else {}
        rows.append(
            [
                item["id"],
                util.human_bytes(item["bytes"]),
                item["cache_state"],
                item["action"],
                "{0}@{1}".format(first.get("type", "-"), first.get("priority", "-")),
            ]
        )
    _table(args, ["asset", "bytes", "cache", "action", "first-source"], rows)
    _say(args, "DRY-RUN plan: " + json.dumps(summary, ensure_ascii=False))
    if errors:
        for message in errors:
            util.info("manifest: " + message)
        return util.EXIT_FAIL
    return util.EXIT_OK


def cmd_discover(args: argparse.Namespace) -> int:
    config, _config_path = _load_config(args)
    provider = str(args.provider)
    query = _parse_query(args.query)
    adapter = get_adapter(provider, _adapter_configs(config).get(provider))
    status = adapter._probe()
    if not status.available:
        payload = {
            "schema": "ark.discover.v1",
            "provider": provider,
            "available": False,
            "reason": status.reason,
            "detail": util.redact(status.detail),
            "missing_env": list(status.missing_env),
            "query": query,
            "assets": [],
            "applied": False,
        }
        _emit(args, payload)
        _say(args, "{0}: BLOCKED（{1}）".format(provider, util.redact(status.detail)))
        return util.EXIT_NO_SOURCE
    refs = adapter.discover(query)
    payload = {
        "schema": "ark.discover.v1",
        "provider": provider,
        "available": True,
        "reason": "OK",
        "detail": util.redact(status.detail),
        "query": query,
        "assets": [ref.to_dict() for ref in refs],
        "count": len(refs),
        "applied": False,
        "last_error": util.redact(adapter.last_error),
    }
    _emit(args, payload)
    _table(args, ["type", "filename", "bytes", "ref"], [[r.type, r.filename, r.bytes, r.ref] for r in refs])
    _say(args, "DRY-RUN discover: {0} 个候选".format(len(refs)))
    return util.EXIT_OK if refs else util.EXIT_NO_SOURCE


def cmd_register(args: argparse.Namespace) -> int:
    source = Path(str(args.file)).expanduser()
    if util.is_credential_filename(source.name):
        raise util.PathForbidden("拒绝登记凭据文件名: {0}".format(source.name))
    if not source.is_file():
        raise FileNotFoundError("待登记文件不存在: {0}".format(source))
    kind = str(args.kind)
    if kind not in KINDS:
        raise UsageError("--kind 必须是 {0} 之一".format(" / ".join(KINDS)))
    asset_id = str(args.id).strip()
    if not asset_id:
        raise UsageError("--id 不能为空")
    digest = util.sha256_file(source)
    size = source.stat().st_size
    path = _manifest_path(args)
    manifest = load_any(path) if path.is_file() else AssetManifest()
    outcome = "added"
    existing = manifest.by_id(asset_id)
    duplicate = None
    for asset in manifest.assets:
        if asset.sha256 == digest and (existing is None or asset.id != asset_id):
            duplicate = asset
            break
    source_entry = {"type": "local", "path": str(source.resolve()), "priority": 5}
    if existing is not None and existing.sha256 == digest:
        outcome = "merged"
        existing.sources = existing.sources + [source_entry]
        existing.name = str(args.name or existing.name)
        existing.version = str(args.version or existing.version)
        existing.project = str(args.project or existing.project)
        asset = existing
    elif duplicate is not None:
        outcome = "duplicate"
        duplicate.sources = duplicate.sources + [source_entry]
        asset = duplicate
    else:
        if existing is not None:
            outcome = "replaced"
        asset = Asset(
            id=asset_id,
            kind=kind,
            name=str(args.name or asset_id),
            version=str(args.version or ""),
            filename=source.name,
            bytes=size,
            sha256=digest,
            project=str(args.project or ""),
            sources=[source_entry],
            install={
                "cache_rel": cache_rel_for(digest, source.name),
                "material_rel": source.name,
            },
            provenance={
                "origin": "local",
                "note": "registered from {0}".format(util.rel_display(source)),
                "discovered_at": util.format_display(),
            },
            status="verified",
        )
        if existing is not None:
            asset.provenance["note"] += "; superseded: {0} sha={1}".format(
                existing.id, existing.sha256 or "-"
            )
            index = manifest.assets.index(existing)
            manifest.assets[index] = asset
        else:
            manifest.assets.append(asset)
    manifest.generated_at = util.format_display()
    errors = manifest.validate()
    applied = bool(getattr(args, "apply", False))
    if applied:
        if errors:
            payload = {"applied": False, "manifest": str(path), "asset_id": asset.id, "errors": errors}
            _emit(args, payload)
            for message in errors:
                util.info("manifest: " + message)
            return util.EXIT_FAIL
        manifest.save(path)
    payload = {
        "schema": "ark.register.v1",
        "applied": applied,
        "manifest": str(path),
        "asset_id": asset.id,
        "asset": asset.to_dict(),
        "outcome": outcome,
        "errors": errors,
    }
    _emit(args, payload)
    prefix = "" if applied else "DRY-RUN "
    _say(
        args,
        "{0}register: {1} → {2}（{3}，{4}）".format(
            prefix, asset.id, path, outcome, util.human_bytes(size)
        ),
    )
    return util.EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    config, _config_path = _load_config(args)
    cache = _cache(args, config)
    manifest = _load_manifest(args)
    target_id = str(getattr(args, "asset", "") or "")
    if target_id:
        asset = manifest.by_id(target_id)
        if asset is None:
            _emit(
                args,
                {"ok": False, "reason": "ASSET_NOT_FOUND", "asset_id": target_id, "applied": False},
            )
            util.info("清单中没有资产: " + target_id)
            return util.EXIT_NO_SOURCE
        assets = [asset]
    else:
        assets = list(manifest.assets)
    directory = str(getattr(args, "dir", "") or "")
    base = Path(directory).expanduser() if directory else None
    rows: List[Dict[str, Any]] = []
    all_ok = True
    for asset in assets:
        if base is not None:
            candidate = base / asset.material_rel
            state = _dir_state(candidate, asset)
            observed = _observed_sha(candidate)
        else:
            state = cache.verify(asset, apply=False)
            try:
                observed = util.sha256_file(cache.path_for(asset))
            except (util.PathForbidden, OSError):
                observed = ""
        all_ok = all_ok and state == "OK"
        rows.append(
            {
                "id": asset.id,
                "state": state,
                "sha256": asset.sha256,
                "observed": observed,
                "bytes": asset.bytes,
            }
        )
    payload = {
        "schema": "ark.verify.v1",
        "ok": all_ok,
        "applied": False,
        "manifest": str(_manifest_path(args)),
        "scope": "dir" if base is not None else "cache",
        "assets": rows,
        "summary": {"total": len(rows), "ok": sum(1 for row in rows if row["state"] == "OK")},
    }
    _emit(args, payload)
    _table(
        args,
        ["asset", "state", "sha256"],
        [[row["id"], row["state"], (row["observed"] or row["sha256"])[:16] + "…"] for row in rows],
    )
    return util.EXIT_OK if all_ok else util.EXIT_FAIL


def _dir_state(candidate: Path, asset: Asset) -> str:
    if not candidate.is_file():
        return "MISSING"
    try:
        return "OK" if util.sha256_file(candidate) == asset.sha256 else "CORRUPT"
    except OSError:
        return "CORRUPT"


def _observed_sha(candidate: Path) -> str:
    if not candidate.is_file():
        return ""
    try:
        return util.sha256_file(candidate)
    except OSError:
        return ""


def cmd_fetch(args: argparse.Namespace) -> int:
    config, _config_path = _load_config(args)
    cache = _cache(args, config)
    manifest = _load_manifest(args)
    result = fetch_to_cache(manifest, str(args.asset), cache, apply=bool(getattr(args, "apply", False)))
    _emit(args, result)
    prefix = "" if result.get("applied") else "DRY-RUN "
    _say(
        args,
        "{0}fetch: {1} → {2}（{3}）".format(
            prefix, result.get("asset_id"), result.get("reason"), result.get("cache_path", "-")
        ),
    )
    return int(result.get("exit_code") or util.EXIT_OK)


def cmd_cache(args: argparse.Namespace) -> int:
    config, _config_path = _load_config(args)
    cache = _cache(args, config)
    action = str(getattr(args, "action", "") or "list")
    if action == "list":
        entries = cache.list_entries()
        payload = {
            "schema": "ark.cache.v1",
            "action": "list",
            "applied": False,
            "root": str(cache.root),
            "count": len(entries),
            "entries": [entry.to_dict() for entry in entries],
            "stats": cache.stats(),
        }
        _emit(args, payload)
        _table(
            args,
            ["rel", "bytes", "mtime"],
            [[entry.rel, entry.bytes, entry.mtime] for entry in entries],
        )
        _say(args, "cache: {0} 条 / {1}".format(len(entries), util.human_bytes(payload["stats"]["total_bytes"])))
        return util.EXIT_OK
    if action == "verify":
        entries = cache.list_entries(deep=True)
        bad = [entry for entry in entries if entry.verified is False]
        payload = {
            "schema": "ark.cache.v1",
            "action": "verify",
            "applied": False,
            "root": str(cache.root),
            "count": len(entries),
            "bad": len(bad),
            "entries": [entry.to_dict() for entry in entries],
        }
        _emit(args, payload)
        _table(
            args,
            ["rel", "verified"],
            [[entry.rel, "OK" if entry.verified else "CORRUPT"] for entry in entries],
        )
        _say(args, "cache verify: {0} 条，异常 {1} 条".format(len(entries), len(bad)))
        return util.EXIT_OK if not bad else util.EXIT_FAIL
    if action == "prune":
        _keep_days = getattr(args, "keep_days", None)
        keep_days = 30 if _keep_days is None else int(_keep_days)
        plan = cache.prune(keep_days=keep_days, apply=bool(getattr(args, "apply", False)))
        payload = dict(plan.to_dict())
        payload["schema"] = "ark.cache.v1"
        payload["action"] = "prune"
        _emit(args, payload)
        prefix = "" if plan.applied else "DRY-RUN "
        _say(
            args,
            "{0}prune: 可清 {1} 条 / {2}（保留 {3} 天）".format(
                prefix, len(plan.entries), util.human_bytes(plan.total_bytes), keep_days
            ),
        )
        return util.EXIT_OK
    raise UsageError("未知 cache 子命令: {0}".format(action))


def cmd_supply(args: argparse.Namespace) -> int:
    config, _config_path = _load_config(args)
    cache = _cache(args, config)
    manifest = _load_manifest(args)
    result = supply_asset(
        manifest,
        str(args.asset),
        args.dest,
        cache,
        apply=bool(getattr(args, "apply", False)),
    )
    _emit(args, result.to_dict())
    prefix = "" if result.applied else "DRY-RUN "
    _say(
        args,
        "{0}supply: {1} → {2}（{3}）".format(
            prefix, result.asset_id, result.reason, result.dest_path or "-"
        ),
    )
    return int(result.exit_code)


def cmd_manifest(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "") or "")
    if action == "show":
        manifest = _load_manifest(args)
        payload = {
            "applied": False,
            "manifest": str(_manifest_path(args)),
            "schema": manifest.schema,
            "generated_at": manifest.generated_at,
            "assets": [asset.to_dict() for asset in manifest.assets],
        }
        _emit(args, payload)
        _table(
            args,
            ["id", "kind", "version", "bytes", "status"],
            [[a.id, a.kind, a.version or "-", a.bytes, a.status] for a in manifest.assets],
        )
        _say(args, "manifest: {0} 个资产".format(len(manifest.assets)))
        return util.EXIT_OK
    if action == "validate":
        manifest = _load_manifest(args)
        errors = manifest.validate()
        payload = {
            "applied": False,
            "manifest": str(_manifest_path(args)),
            "ok": not errors,
            "count": len(manifest.assets),
            "errors": errors,
        }
        _emit(args, payload)
        for message in errors:
            util.info("manifest: " + message)
        _say(args, "manifest validate: {0}".format("OK" if not errors else "FAIL"))
        return util.EXIT_OK if not errors else util.EXIT_FAIL
    if action == "convert":
        manifest = _load_manifest(args)
        target = str(args.to)
        if target == "ark-artifacts":
            document = manifest.to_ark_artifacts()
        else:
            document = manifest.to_venusahisiburner(description="converted by asset-tools")
        out = str(getattr(args, "out", "") or "")
        applied = bool(getattr(args, "apply", False))
        if out and applied:
            util.atomic_write_json(out, document)
        payload = {
            "applied": bool(out and applied),
            "to": target,
            "out": out,
            "schema": document.get("schema"),
            "document": document,
        }
        _emit(args, payload)
        prefix = "" if (out and applied) else "DRY-RUN "
        _say(
            args,
            "{0}convert: {1} → {2}{3}".format(
                prefix, len(manifest.assets), target, "（已写 {0}）".format(out) if out and applied else ""
            ),
        )
        return util.EXIT_OK
    if action == "merge":
        base_path = _manifest_path(args)
        if not base_path.is_file():
            raise FileNotFoundError("清单不存在: {0}".format(base_path))
        other_path = Path(str(args.other)).expanduser()
        if not other_path.is_file():
            raise FileNotFoundError("清单不存在: {0}".format(other_path))
        merged = load_any(base_path).merge(load_any(other_path), prefer=str(args.prefer))
        out = str(getattr(args, "out", "") or "")
        applied = bool(getattr(args, "apply", False))
        if out and applied:
            merged.save(out)
        payload = {
            "applied": bool(out and applied),
            "out": out,
            "prefer": str(args.prefer),
            "assets": [asset.to_dict() for asset in merged.assets],
            "count": len(merged.assets),
        }
        _emit(args, payload)
        prefix = "" if (out and applied) else "DRY-RUN "
        _say(args, "{0}merge: {1} 个资产".format(prefix, len(merged.assets)))
        return util.EXIT_OK
    raise UsageError("未知 manifest 子命令: {0!r}".format(action))


_HANDLERS = {
    "status": cmd_status,
    "plan": cmd_plan,
    "discover": cmd_discover,
    "register": cmd_register,
    "verify": cmd_verify,
    "fetch": cmd_fetch,
    "cache": cmd_cache,
    "supply": cmd_supply,
    "manifest": cmd_manifest,
}


# --------------------------------------------------------------------- main


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    command = str(getattr(args, "command", "") or "")
    handler = _HANDLERS.get(command)
    if handler is None:
        raise UsageError("未知命令: {0!r}".format(command))
    if command in ("cache", "manifest"):
        action = str(getattr(args, "action", "") or "")
        if not action:
            raise UsageError("{0} 需要子命令（{1}）".format(
                command,
                "list/verify/prune" if command == "cache" else "show/validate/convert/merge",
            ))
    return int(handler(args))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse 用 SystemExit 表达 --help / 用法错
        code = exc.code if exc.code is not None else 0
        return int(code)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return util.EXIT_USAGE
    try:
        return int(_dispatch(args, parser))
    except UsageError as exc:
        util.info("{0}: {1}".format(PROG, exc))
        return util.EXIT_USAGE
    except util.PathForbidden as exc:
        util.info("{0}: {1}".format(PROG, exc))
        return util.EXIT_FORBIDDEN
    except FileNotFoundError as exc:
        util.info("{0}: {1}".format(PROG, exc))
        return util.EXIT_NO_SOURCE
    except PermissionError as exc:
        util.info("{0}: {1}".format(PROG, exc))
        return util.EXIT_FORBIDDEN
    except KeyError as exc:
        util.info("{0}: {1}".format(PROG, exc))
        return util.EXIT_USAGE
    except KeyboardInterrupt:
        util.info(PROG + ": 已中断")
        return 130
    except BrokenPipeError:
        return util.EXIT_FAIL
    except (ValueError, OSError) as exc:
        util.info("{0}: {1}".format(PROG, util.redact(exc)))
        return util.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
