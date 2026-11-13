"""1006 可复用 TTS 音频库核心（``ark.tts-asset.v1``）。

本模块是 TTS 共享音频库的 canonical 实现，只负责：

* 由完整合成规格推导确定性 cache key / asset_id；
* 在库根上做精确查找、候选提示、取件复制、发布、校验、索引重建与状态巡检；
* 发布时的锁、compare-and-create、版本化目录、query-back 与 outbox。

边界（与 ``result/tts-library-proposal.md`` 一致）
---------------------------------------------------
* 合成永远由 ``tts-runtime`` 完成；本模块不调用任何 TTS 引擎。
* 派生处理（重采样/增益/降噪/裁剪）只发生在任务副本，本模块拒绝写库原件。
* 库根只允许本模块写；调用方只能通过 :meth:`TtsLibrary.fetch` 取件。
* 任何时间字段统一走 :mod:`asset_tools.util` 的北京时间展示格式
  ``YYYY-MM-DD HH:MM:SS``（无时区后缀、无小数秒）。

退出码沿用 :mod:`asset_tools.util`：0 OK / 1 FAIL / 2 USAGE /
3 NO_SOURCE / 4 FORBIDDEN / 5 NETWORK / 6 NEEDS_ELEVATION。
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import util

SCHEMA_SPEC = "ark.tts-spec.v1"
SCHEMA_ASSET = "ark.tts-asset.v1"
SCHEMA_INDEX = "ark.tts-library-index.v1"
SCHEMA_LOOKUP = "ark.tts-lookup.v1"
SCHEMA_FETCH = "ark.tts-fetch.v1"
SCHEMA_PUBLISH = "ark.tts-publish.v1"
SCHEMA_VERIFY = "ark.tts-verify.v1"
SCHEMA_STATUS = "ark.tts-status.v1"
SCHEMA_OUTBOX = "ark.tts-outbox.v1"

DEFAULT_ROOT = "/srv/storage/ark/artifacts/audio/tts-library"
DEFAULT_LOCK_TIMEOUT = 600.0
LOCK_POLL_INTERVAL = 0.05

VISIBILITIES = ("public", "project", "restricted")
STATUSES = ("active", "stale", "superseded")
QUALITIES = ("formal", "candidate", "evidence_gap")

SYNTHESIS_KEYS = ("rate", "pitch", "style", "emotion", "speed")
OUTPUT_KEYS = ("container", "codec", "sample_rate", "channels", "bit_depth")

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ASSET_ID_RE = re.compile(r"^tts-[0-9a-f]{16}$")
ASSET_DIR_RE = re.compile(r"^(tts-[0-9a-f]{16})(?:-v([0-9]+))?$")
LANGUAGE_RE = re.compile(r"^[A-Z]{2,8}(?:-[A-Z0-9]{1,8})*$")

#: 零宽字符：不参与文本语义，规范化阶段一律删除。
ZERO_WIDTH = frozenset(
    "\u00ad\u034f\u061c\u180e\u200b\u200c\u200d\u200e\u200f"
    "\u2060\u2061\u2062\u2063\u2064\ufeff"
)

POLICY = {
    "rule": "先查库、miss 才合成",
    "responsible_repo": "asset-tools",
    "library_root_kind": "1006",
    "miss_owner": "tts-runtime",
}


class TtsUsageError(ValueError):
    """规格/参数不合法（CLI 映射为 ``EXIT_USAGE``）。"""


class TtsForbidden(util.PathForbidden):
    """ACL 越权、敏感发布、路径越界（CLI 映射为 ``EXIT_FORBIDDEN``）。"""


class LockTimeout(RuntimeError):
    """等待资产锁超时。"""


# --------------------------------------------------------------------- 文本与键


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(text: Any) -> str:
    """NFKC → 去零宽字符 → 去首尾空白 → 连续空白折叠为单个半角空格。"""
    if not isinstance(text, str):
        raise TtsUsageError("text 必须是字符串")
    value = unicodedata.normalize("NFKC", text)
    value = "".join(ch for ch in value if ch not in ZERO_WIDTH)
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_language(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise TtsUsageError("language 不能为空")
    if not LANGUAGE_RE.match(text):
        raise TtsUsageError("language 非法: {0!r}".format(value))
    return text


def _optional_string(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _optional_number(value: Any, field: str, integer: bool = False) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TtsUsageError("{0} 必须是数字".format(field))
    if integer:
        if isinstance(value, float) and not value.is_integer():
            raise TtsUsageError("{0} 必须是整数".format(field))
        try:
            return int(value)
        except (TypeError, ValueError):
            raise TtsUsageError("{0} 必须是整数".format(field))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise TtsUsageError("{0} 必须是数字".format(field))
    if number.is_integer():
        return int(number)
    return number


def normalize_voice(voice: Any) -> Dict[str, Optional[str]]:
    """voice 只能是 ``speaker`` 或 ``voice_ref_sha256`` 二选一，禁止路径。"""
    if not isinstance(voice, dict):
        raise TtsUsageError("voice 必须是对象")
    speaker = _optional_string(voice.get("speaker"), "voice.speaker")
    ref = _optional_string(voice.get("voice_ref_sha256"), "voice.voice_ref_sha256")
    if ref is not None:
        ref = ref.lower()
        if not SHA256_RE.match(ref):
            raise TtsUsageError("voice.voice_ref_sha256 必须是 64 位小写十六进制")
    if speaker and ref:
        raise TtsUsageError("voice 只能二选一：speaker 或 voice_ref_sha256")
    if not speaker and not ref:
        raise TtsUsageError("voice 必须提供 speaker 或 voice_ref_sha256")
    if speaker:
        if "/" in speaker or "\\" in speaker or os.path.isabs(speaker) or re.match(r"^[A-Za-z]:", speaker):
            raise TtsUsageError("voice.speaker 不得写成路径: {0!r}".format(speaker))
    return {"speaker": speaker, "voice_ref_sha256": ref}


def normalize_engine(engine: Any) -> Dict[str, Optional[str]]:
    if isinstance(engine, str):
        engine = {"engine": engine}
    if not isinstance(engine, dict):
        raise TtsUsageError("engine 必须是对象或字符串")
    name = _optional_string(engine.get("engine"), "engine.engine")
    if not name:
        raise TtsUsageError("engine.engine 不能为空")
    return {
        "engine": name,
        "model": _optional_string(engine.get("model"), "engine.model"),
        "model_version": _optional_string(engine.get("model_version"), "engine.model_version"),
    }


def _fixed_object(
    raw: Any,
    keys: Tuple[str, ...],
    field: str,
    string_keys: Tuple[str, ...] = (),
    integer_keys: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """按固定键集输出；未设置的键显式写 ``null``；未知键直接拒绝。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TtsUsageError("{0} 必须是对象".format(field))
    unknown = sorted(set(raw.keys()) - set(keys))
    if unknown:
        raise TtsUsageError("{0} 含未知键: {1}".format(field, ", ".join(unknown)))
    result: Dict[str, Any] = {}
    for key in keys:
        value = raw.get(key)
        if key in string_keys:
            result[key] = _optional_string(value, "{0}.{1}".format(field, key))
        else:
            result[key] = _optional_number(value, "{0}.{1}".format(field, key), integer=key in integer_keys)
    return result


def normalize_spec(raw: Any) -> Dict[str, Any]:
    """把输入规格规整成参与缓存键计算的 canonical 结构。

    接受两种输入形状：

    * 原始形状：``{"text": {"raw": "...", "language": "zh"}, ...}``
      或 ``{"text": "...", "language": "zh", ...}``；
    * 规范化形状：``{"normalized_text": "...", "language": "ZH", ...}``。

    其余键（``voice`` / ``engine`` / ``synthesis_params`` / ``output`` /
    ``normalization_profile``）在两种形状里一致。
    """
    if not isinstance(raw, dict):
        raise TtsUsageError("spec 必须是 JSON 对象")
    text_section = raw.get("text")
    language = raw.get("language")
    if isinstance(text_section, dict):
        raw_text = text_section.get("raw")
        if raw_text is None:
            raw_text = text_section.get("normalized")
        language = text_section.get("language", language)
    elif isinstance(text_section, str):
        raw_text = text_section
    elif text_section is None:
        raw_text = raw.get("normalized_text")
    else:
        raise TtsUsageError("spec.text 必须是字符串或对象")
    if raw_text is None:
        raise TtsUsageError("spec 缺少文本（text.raw / text.normalized / normalized_text）")
    if not isinstance(raw_text, str):
        raise TtsUsageError("spec 文本必须是字符串")

    normalized_text = normalize_text(raw_text)
    if not normalized_text:
        raise TtsUsageError("规范化后的文本为空")

    profile_raw = raw.get("normalization_profile")
    profile = _optional_string(profile_raw, "normalization_profile") or "none"

    return {
        "schema": SCHEMA_SPEC,
        "raw_text": raw_text,
        "raw_sha256": _sha256_text(raw_text),
        "normalized_text": normalized_text,
        "normalized_sha256": _sha256_text(normalized_text),
        "language": normalize_language(language),
        "voice": normalize_voice(raw.get("voice")),
        "engine": normalize_engine(raw.get("engine")),
        "synthesis_params": _fixed_object(
            raw.get("synthesis_params"),
            SYNTHESIS_KEYS,
            "synthesis_params",
            string_keys=("style", "emotion"),
        ),
        "output": _fixed_object(
            raw.get("output"),
            OUTPUT_KEYS,
            "output",
            string_keys=("container", "codec"),
            integer_keys=("sample_rate", "channels", "bit_depth"),
        ),
        "normalization_profile": profile,
    }


def key_object(canonical: Dict[str, Any]) -> Dict[str, Any]:
    """参与 ``cache_key`` 的精确对象（字段顺序无关，序列化时排序）。"""
    engine = canonical.get("engine") or {}
    return {
        "normalized_text": canonical["normalized_text"],
        "language": canonical["language"],
        "voice": {
            "speaker": canonical["voice"]["speaker"],
            "voice_ref_sha256": canonical["voice"]["voice_ref_sha256"],
        },
        "engine": engine.get("engine"),
        "model": engine.get("model"),
        "model_version": engine.get("model_version"),
        "synthesis_params": dict(canonical["synthesis_params"]),
        "output": dict(canonical["output"]),
        "normalization_profile": canonical["normalization_profile"],
    }


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cache_key_for(canonical: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(key_object(canonical)).encode("utf-8")).hexdigest()


def asset_id_for(cache_key: str) -> str:
    digest = str(cache_key or "").lower()
    if not SHA256_RE.match(digest):
        raise TtsUsageError("cache_key 非法: {0!r}".format(cache_key))
    return "tts-" + digest[:16]


def compute_key(spec: Any) -> Dict[str, Any]:
    """返回 ``cache_key`` / ``asset_id`` 与用于复核的 canonical 视图。"""
    canonical = normalize_spec(spec)
    key = cache_key_for(canonical)
    return {
        "schema": SCHEMA_SPEC,
        "cache_key": key,
        "asset_id": asset_id_for(key),
        "language": canonical["language"],
        "normalized_text": canonical["normalized_text"],
        "normalized_sha256": canonical["normalized_sha256"],
        "raw_sha256": canonical["raw_sha256"],
        "canonical": key_object(canonical),
    }


# --------------------------------------------------------------------- 作用域


def normalize_scope(scope: Any) -> Dict[str, Any]:
    if scope is None:
        scope = {"visibility": "public", "projects": [], "nodes": []}
    if not isinstance(scope, dict):
        raise TtsUsageError("scope 必须是对象")
    visibility = str(scope.get("visibility") or "public").strip().lower()
    if visibility not in VISIBILITIES:
        raise TtsUsageError("scope.visibility 必须是 public / project / restricted")
    projects = [str(item).strip() for item in (scope.get("projects") or []) if str(item).strip()]
    nodes = [str(item).strip() for item in (scope.get("nodes") or []) if str(item).strip()]
    if visibility == "project" and not projects:
        raise TtsUsageError("scope.visibility=project 时 projects 不能为空")
    if visibility == "restricted" and not (projects or nodes):
        raise TtsUsageError("scope.visibility=restricted 时 projects 或 nodes 至少一个非空")
    return {"visibility": visibility, "projects": projects, "nodes": nodes}


def acl_allows(scope: Any, project: Optional[str] = None, node: Optional[str] = None) -> bool:
    try:
        normalized = normalize_scope(scope)
    except TtsUsageError:
        return False
    visibility = normalized["visibility"]
    if visibility == "public":
        return True
    if project and project in normalized["projects"]:
        return True
    if node and node in normalized["nodes"]:
        return True
    return False


def _sensitive_reason(manifest: Dict[str, Any]) -> Optional[str]:
    shareable = manifest.get("shareable")
    sensitivity = str(manifest.get("sensitivity") or "none").strip().lower()
    if shareable is not True:
        return "SENSITIVE_DENIED"
    if sensitivity not in ("none", ""):
        return "SENSITIVE_DENIED"
    return None


# --------------------------------------------------------------------- manifest


def _truncate_text(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def manifest_from_spec(
    spec: Any,
    audio_path: Any,
    *,
    shareable: bool = True,
    scope: Any = None,
    quality: str = "formal",
    status: str = "active",
    sensitivity: str = "none",
    provenance: Any = None,
    created_at: Optional[str] = None,
    duration_seconds: Any = None,
    supersedes: Optional[str] = None,
    derived_from_asset_id: Optional[str] = None,
    processing_chain: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """由合成规格 + 音频文件生成 ``ark.tts-asset.v1`` 清单。"""
    canonical = normalize_spec(spec)
    key = cache_key_for(canonical)
    asset_id = asset_id_for(key)
    audio = Path(str(audio_path))
    if not audio.is_file():
        raise TtsUsageError("音频文件不存在: {0}".format(audio))
    digest = util.sha256_file(audio)
    size = audio.stat().st_size
    if quality not in QUALITIES:
        raise TtsUsageError("quality 必须是 {0} 之一".format(" / ".join(QUALITIES)))
    if status not in STATUSES:
        raise TtsUsageError("status 必须是 {0} 之一".format(" / ".join(STATUSES)))
    scope_obj = normalize_scope(scope)
    sensitive = (not shareable) or str(sensitivity).strip().lower() not in ("none", "")
    stored_text = _truncate_text(canonical["normalized_text"]) if sensitive else canonical["normalized_text"]
    now = util.format_display()
    prov = {
        "generated_at": now,
        "node": util.hostname(),
        "tool": "tts-runtime",
        "tool_version": "",
        "tool_commit": "",
        "license": "internal",
        "source": "cache-miss-synthesis",
    }
    if isinstance(provenance, dict):
        prov.update(provenance)
    return {
        "schema": SCHEMA_ASSET,
        "asset_id": asset_id,
        "cache_key": key,
        "status": status,
        "quality": quality,
        "shareable": bool(shareable),
        "sensitivity": str(sensitivity or "none"),
        "scope": scope_obj,
        "text": {
            "raw_sha256": canonical["raw_sha256"],
            "normalized": stored_text,
            "language": canonical["language"],
            "truncated": bool(sensitive),
            "normalized_sha256": canonical["normalized_sha256"],
        },
        "voice": canonical["voice"],
        "engine": canonical["engine"],
        "synthesis_params": canonical["synthesis_params"],
        "output": canonical["output"],
        "normalization_profile": canonical["normalization_profile"],
        "audio": {
            "filename": digest + ".wav",
            "bytes": size,
            "sha256": digest,
            "duration_seconds": _optional_number(duration_seconds, "duration_seconds"),
        },
        "provenance": prov,
        "supersedes": supersedes,
        "superseded_by": None,
        "derived_from_asset_id": derived_from_asset_id,
        "processing_chain": list(processing_chain or []),
        "created_at": created_at or now,
    }


def spec_from_manifest(manifest: Any) -> Optional[Dict[str, Any]]:
    """从 manifest 反推可复核 key 的规格；截断文本返回 ``None``。"""
    if not isinstance(manifest, dict):
        return None
    text = manifest.get("text")
    if not isinstance(text, dict) or text.get("truncated"):
        return None
    normalized = text.get("normalized")
    if not isinstance(normalized, str) or not normalized:
        return None
    return {
        "normalized_text": normalized,
        "language": text.get("language"),
        "voice": manifest.get("voice"),
        "engine": manifest.get("engine"),
        "synthesis_params": manifest.get("synthesis_params"),
        "output": manifest.get("output"),
        "normalization_profile": manifest.get("normalization_profile"),
    }


def validate_manifest(manifest: Any) -> List[str]:
    """结构校验：返回错误列表（空列表代表结构可用）。"""
    errors: List[str] = []
    if not isinstance(manifest, dict):
        return ["manifest 必须是对象"]
    if manifest.get("schema") != SCHEMA_ASSET:
        errors.append("schema 必须是 {0}".format(SCHEMA_ASSET))
    asset_id = str(manifest.get("asset_id") or "")
    key = str(manifest.get("cache_key") or "")
    if not ASSET_ID_RE.match(asset_id):
        errors.append("asset_id 非法")
    if not SHA256_RE.match(key):
        errors.append("cache_key 非法")
    elif asset_id and asset_id != asset_id_for(key):
        errors.append("asset_id 与 cache_key 不一致")
    if manifest.get("status") not in STATUSES:
        errors.append("status 非法")
    if manifest.get("quality") not in QUALITIES:
        errors.append("quality 非法")
    try:
        normalize_scope(manifest.get("scope"))
    except TtsUsageError as exc:
        errors.append(str(exc))
    text = manifest.get("text")
    if not isinstance(text, dict):
        errors.append("text 必须是对象")
    else:
        if not SHA256_RE.match(str(text.get("raw_sha256") or "")):
            errors.append("text.raw_sha256 非法")
        try:
            normalize_language(text.get("language"))
        except TtsUsageError as exc:
            errors.append(str(exc))
        if not isinstance(text.get("normalized"), str):
            errors.append("text.normalized 必须是字符串")
        if "raw" in text:
            errors.append("manifest 不得保存原始全文（text.raw）")
    try:
        normalize_voice(manifest.get("voice"))
    except TtsUsageError as exc:
        errors.append(str(exc))
    try:
        normalize_engine(manifest.get("engine"))
    except TtsUsageError as exc:
        errors.append(str(exc))
    for field, keys, strings, integers in (
        ("synthesis_params", SYNTHESIS_KEYS, ("style", "emotion"), ()),
        ("output", OUTPUT_KEYS, ("container", "codec"), ("sample_rate", "channels", "bit_depth")),
    ):
        try:
            _fixed_object(manifest.get(field), keys, field, string_keys=strings, integer_keys=integers)
        except TtsUsageError as exc:
            errors.append(str(exc))
    audio = manifest.get("audio")
    if not isinstance(audio, dict):
        errors.append("audio 必须是对象")
    else:
        sha = str(audio.get("sha256") or "")
        if not SHA256_RE.match(sha):
            errors.append("audio.sha256 非法")
        if str(audio.get("filename") or "") != sha + ".wav":
            errors.append("audio.filename 必须等于 <audio.sha256>.wav")
        try:
            bytes_value = int(audio.get("bytes"))
            if bytes_value < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("audio.bytes 必须是非负整数")
    if not isinstance(manifest.get("provenance"), dict):
        errors.append("provenance 必须是对象")
    if not isinstance(manifest.get("processing_chain"), list):
        errors.append("processing_chain 必须是数组")
    return errors


# --------------------------------------------------------------------- 工具


def _read_json(path: Path) -> Any:
    return util.read_json(path)


def _version_of(dirname: str) -> Tuple[str, int]:
    match = ASSET_DIR_RE.match(dirname)
    if not match:
        return "", 0
    return match.group(1), int(match.group(2) or 1)


def _entry_from_manifest(manifest: Dict[str, Any], dirname: str) -> Dict[str, Any]:
    text = manifest.get("text") or {}
    voice = manifest.get("voice") or {}
    engine = manifest.get("engine") or {}
    audio = manifest.get("audio") or {}
    scope = manifest.get("scope") or {}
    _base_id, version = _version_of(dirname)
    filename = str(audio.get("filename") or "")
    return {
        "asset_id": str(manifest.get("asset_id") or ""),
        "dir": dirname,
        "version": version,
        "cache_key": str(manifest.get("cache_key") or ""),
        "status": str(manifest.get("status") or ""),
        "quality": str(manifest.get("quality") or ""),
        "shareable": bool(manifest.get("shareable")),
        "sensitivity": str(manifest.get("sensitivity") or "none"),
        "visibility": str(scope.get("visibility") or ""),
        "projects": list(scope.get("projects") or []),
        "nodes": list(scope.get("nodes") or []),
        "language": str(text.get("language") or ""),
        "text_sha256": str(text.get("raw_sha256") or ""),
        "text_normalized_sha256": str(text.get("normalized_sha256") or ""),
        "voice_speaker": voice.get("speaker"),
        "voice_ref_sha256": voice.get("voice_ref_sha256"),
        "engine": engine.get("engine"),
        "model": engine.get("model"),
        "model_version": engine.get("model_version"),
        "audio": {
            "sha256": str(audio.get("sha256") or ""),
            "bytes": int(audio.get("bytes") or 0),
            "duration_seconds": audio.get("duration_seconds"),
            "path": "{0}/{1}".format(dirname, filename) if filename else "",
        },
        "path": dirname,
        "manifest_path": "{0}/manifest.json".format(dirname),
        "created_at": str(manifest.get("created_at") or ""),
        "supersedes": manifest.get("supersedes"),
        "superseded_by": manifest.get("superseded_by"),
        "scope": {
            "visibility": str(scope.get("visibility") or ""),
            "projects": list(scope.get("projects") or []),
            "nodes": list(scope.get("nodes") or []),
        },
    }


def _summary(entry: Dict[str, Any], match: str) -> Dict[str, Any]:
    """对外返回的受控元数据：只给相对库根的路径，不给绝对路径。"""
    audio = entry.get("audio") or {}
    return {
        "asset_id": entry["asset_id"],
        "cache_key": entry["cache_key"],
        "match": match,
        "status": entry["status"],
        "quality": entry["quality"],
        "shareable": entry["shareable"],
        "visibility": entry["visibility"],
        "language": entry["language"],
        "voice": {
            "speaker": entry.get("voice_speaker"),
            "voice_ref_sha256": entry.get("voice_ref_sha256"),
        },
        "engine": {
            "engine": entry.get("engine"),
            "model": entry.get("model"),
            "model_version": entry.get("model_version"),
        },
        "audio": {
            "sha256": audio.get("sha256") or "",
            "bytes": int(audio.get("bytes") or 0),
            "duration_seconds": audio.get("duration_seconds"),
        },
        "path": entry.get("dir") or "",
        "manifest_path": entry.get("manifest_path") or "",
        "audio_path": audio.get("path") or "",
        "created_at": entry.get("created_at") or "",
        "supersedes": entry.get("supersedes"),
        "superseded_by": entry.get("superseded_by"),
    }


# --------------------------------------------------------------------- 库


class TtsLibrary:
    """1006 TTS 共享音频库的读写门面。"""

    def __init__(self, root: Any = None) -> None:
        raw = root or os.environ.get("ARK_TTS_LIBRARY") or DEFAULT_ROOT
        self.root = Path(str(raw)).expanduser()
        self.reclaimed_locks: List[str] = []
        self.last_outbox: str = ""

    # ---------------------------------------------------------------- 路径

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    @property
    def incoming_dir(self) -> Path:
        return self.root / "_incoming"

    @property
    def quarantine_dir(self) -> Path:
        return self.root / "_quarantine"

    def _safe_join(self, rel: Any) -> Path:
        text = util.safe_rel_path(rel)
        target = self.root / text
        if not util.is_within(target, self.root):
            raise TtsForbidden("路径越界: {0!r}".format(rel))
        return target

    # ---------------------------------------------------------------- 可达性

    def reachable(self) -> Tuple[bool, str]:
        try:
            if not self.root.is_dir():
                return False, "库根目录不可达"
            if not os.access(str(self.root), os.R_OK | os.X_OK):
                return False, "库根目录不可读"
        except OSError as exc:
            return False, util.redact(exc)
        return True, "OK"

    def _ensure_writable(self) -> Tuple[bool, str, int]:
        # 1006 库根由运维预置；本工具绝不自动创建，避免把未挂载路径写成本地目录。
        try:
            if not self.root.is_dir():
                return False, "库根目录不可达", util.EXIT_NO_SOURCE
        except OSError as exc:
            return False, util.redact(exc), util.EXIT_NO_SOURCE
        if not os.access(str(self.root), os.W_OK | os.X_OK):
            return False, "库根只读，拒绝发布", util.EXIT_FORBIDDEN
        try:
            self.locks_dir.mkdir(parents=True, exist_ok=True)
            self.incoming_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            return False, util.redact(exc), util.EXIT_FORBIDDEN
        except OSError as exc:
            return False, util.redact(exc), util.EXIT_NO_SOURCE
        return True, "OK", util.EXIT_OK

    # ---------------------------------------------------------------- 索引

    def scan_entries(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(self.root.iterdir(), key=lambda item: item.name)
        except OSError:
            return entries
        for child in children:
            try:
                if not child.is_dir():
                    continue
                base_id, version = _version_of(child.name)
                if not base_id or version < 1:
                    continue
                manifest_path = child / "manifest.json"
                if not manifest_path.is_file():
                    continue
                manifest = _read_json(manifest_path)
                if not isinstance(manifest, dict):
                    continue
                if manifest.get("schema") != SCHEMA_ASSET:
                    continue
                entry = _entry_from_manifest(manifest, child.name)
                if not ASSET_ID_RE.match(str(entry.get("asset_id") or "")):
                    continue
                entries.append(entry)
            except (OSError, ValueError):
                continue
        entries.sort(key=lambda item: (item["asset_id"], int(item.get("version") or 1)))
        return entries

    def _annotate_versions(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            grouped.setdefault(entry["asset_id"], []).append(entry)
        for group in grouped.values():
            group.sort(key=lambda item: int(item.get("version") or 1))
            for entry in group[:-1]:
                if not entry.get("superseded_by"):
                    entry["superseded_by"] = group[-1]["dir"]
            newest = group[-1]
            newest["superseded_by"] = newest.get("superseded_by") or None
        return entries

    def entries(self) -> List[Dict[str, Any]]:
        payload: Any = None
        try:
            if self.index_path.is_file():
                payload = _read_json(self.index_path)
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("schema") == SCHEMA_INDEX:
            assets = payload.get("assets")
            if isinstance(assets, list):
                clean = [
                    item
                    for item in assets
                    if isinstance(item, dict) and item.get("asset_id") and item.get("dir")
                ]
                if clean:
                    return self._annotate_versions(clean)
        return self._annotate_versions(self.scan_entries())

    def entries_by_asset(self) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for entry in self.entries():
            grouped.setdefault(str(entry.get("asset_id")), []).append(entry)
        for group in grouped.values():
            group.sort(key=lambda item: int(item.get("version") or 1))
        return grouped

    def build_index(self) -> Dict[str, Any]:
        entries = self._annotate_versions(self.scan_entries())
        asset_ids = {entry["asset_id"] for entry in entries}
        return {
            "schema": SCHEMA_INDEX,
            "generated_at": util.format_display(),
            "library": {"kind": "1006", "relative_root": "audio/tts-library"},
            "policy": dict(POLICY),
            "count": len(entries),
            "asset_count": len(asset_ids),
            "assets": entries,
        }

    def write_index(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        document = payload or self.build_index()
        util.atomic_write_json(self.index_path, document)
        return document

    def rebuild_index(self) -> Dict[str, Any]:
        return self.write_index(self.build_index())

    # ---------------------------------------------------------------- 清单读取

    def manifest_for(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        directory = self._safe_join(entry["dir"])
        manifest = _read_json(directory / "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA_ASSET:
            raise ValueError("资产清单非法: {0}".format(entry["dir"]))
        return manifest

    def audio_state(self, entry: Dict[str, Any], manifest: Dict[str, Any]) -> Tuple[str, str, int]:
        """返回 ``(state, observed_sha, observed_bytes)``；state ∈ OK/MISSING/CORRUPT。"""
        audio = manifest.get("audio") or {}
        filename = str(audio.get("filename") or "")
        try:
            target = self._safe_join("{0}/{1}".format(entry["dir"], util.safe_filename(filename)))
        except (util.PathForbidden, ValueError):
            return "CORRUPT", "", 0
        if not target.is_file():
            return "MISSING", "", 0
        try:
            observed = util.sha256_file(target)
            size = target.stat().st_size
        except OSError:
            return "CORRUPT", "", 0
        expected = str(audio.get("sha256") or "")
        if observed != expected:
            return "CORRUPT", observed, size
        try:
            expected_bytes = int(audio.get("bytes"))
        except (TypeError, ValueError):
            expected_bytes = -1
        if expected_bytes >= 0 and size != expected_bytes:
            return "CORRUPT", observed, size
        return "OK", observed, size

    def _canonical_entry(self, asset_id: str) -> Optional[Dict[str, Any]]:
        group = self.entries_by_asset().get(asset_id)
        if not group:
            return None
        return group[-1]

    # ---------------------------------------------------------------- 评估

    def _evaluate(
        self,
        entry: Dict[str, Any],
        manifest: Dict[str, Any],
        *,
        expected_key: Optional[str],
        project: Optional[str],
        node: Optional[str],
        allow_candidate: bool,
        confirm: bool,
        include_stale: bool,
        verify: bool,
    ) -> Dict[str, Any]:
        errors = validate_manifest(manifest)
        if errors:
            return {"allowed": False, "usable": False, "reason": "MANIFEST_INVALID", "detail": "; ".join(errors), "exit_code": util.EXIT_FAIL}
        entry_id = str(manifest.get("asset_id") or "")
        if entry_id != str(entry.get("asset_id") or ""):
            return {"allowed": False, "usable": False, "reason": "ASSET_ID_MISMATCH", "detail": "", "exit_code": util.EXIT_FAIL}
        if expected_key is not None and str(manifest.get("cache_key") or "") != expected_key:
            return {"allowed": False, "usable": False, "reason": "CACHE_KEY_MISMATCH", "detail": "", "exit_code": util.EXIT_FAIL}
        expected_asset = asset_id_for(expected_key) if expected_key else entry_id
        if expected_key is not None and entry_id != expected_asset:
            return {"allowed": False, "usable": False, "reason": "ASSET_ID_MISMATCH", "detail": "", "exit_code": util.EXIT_FAIL}
        status = str(manifest.get("status") or "")
        if status != "active":
            if include_stale:
                return {"allowed": True, "usable": False, "reason": "STATUS_NOT_ACTIVE", "detail": status, "exit_code": util.EXIT_OK, "quality_downgrade": False}
            return {"allowed": False, "usable": False, "reason": "STATUS_NOT_ACTIVE", "detail": status, "exit_code": util.EXIT_NO_SOURCE}
        quality = str(manifest.get("quality") or "")
        downgrade = False
        if quality != "formal":
            if not (allow_candidate and confirm):
                return {"allowed": False, "usable": False, "reason": "QUALITY_NOT_FORMAL", "detail": quality, "exit_code": util.EXIT_NO_SOURCE}
            downgrade = True
        if manifest.get("shareable") is not True:
            return {"allowed": False, "usable": False, "reason": "NOT_SHAREABLE", "detail": "", "exit_code": util.EXIT_FORBIDDEN}
        if not acl_allows(manifest.get("scope"), project=project, node=node):
            return {"allowed": False, "usable": False, "reason": "ACL_DENIED", "detail": "", "exit_code": util.EXIT_FORBIDDEN}
        if verify:
            state, observed, size = self.audio_state(entry, manifest)
            if state != "OK":
                return {"allowed": False, "usable": False, "reason": "SHA256_MISMATCH" if state == "CORRUPT" else "AUDIO_MISSING", "detail": observed or state, "exit_code": util.EXIT_FAIL}
        return {"allowed": True, "usable": True, "reason": "OK", "detail": "", "exit_code": util.EXIT_OK, "quality_downgrade": downgrade}

    # ---------------------------------------------------------------- lookup

    def lookup(
        self,
        spec: Any,
        *,
        project: Optional[str] = None,
        node: Optional[str] = None,
        allow_candidate: bool = False,
        confirm: bool = False,
        include_stale: bool = False,
        verify: bool = True,
    ) -> Dict[str, Any]:
        ok, reason = self.reachable()
        if not ok:
            return {
                "schema": SCHEMA_LOOKUP,
                "hit": False,
                "usable": False,
                "match": "unreachable",
                "unreachable": True,
                "reason": "UNREACHABLE",
                "detail": reason,
                "message": "TTS 库 UNREACHABLE",
                "asset": None,
                "candidates": [],
                "exit_code": util.EXIT_NO_SOURCE,
            }
        computed = compute_key(spec)
        key = computed["cache_key"]
        asset_id = computed["asset_id"]
        entry = self._canonical_entry(asset_id)
        if entry is not None:
            try:
                manifest = self.manifest_for(entry)
            except (OSError, ValueError) as exc:
                return {
                    "schema": SCHEMA_LOOKUP,
                    "hit": False,
                    "usable": False,
                    "match": "rejected",
                    "unreachable": False,
                    "cache_key": key,
                    "asset_id": asset_id,
                    "reason": "MANIFEST_INVALID",
                    "detail": util.redact(exc),
                    "asset": None,
                    "candidates": [],
                    "exit_code": util.EXIT_FAIL,
                }
            verdict = self._evaluate(
                entry,
                manifest,
                expected_key=key,
                project=project,
                node=node,
                allow_candidate=allow_candidate,
                confirm=confirm,
                include_stale=include_stale,
                verify=verify,
            )
            downgrade = bool(verdict.get("quality_downgrade"))
            payload = {
                "schema": SCHEMA_LOOKUP,
                "hit": bool(verdict["allowed"] and verdict["usable"] and not downgrade),
                "usable": bool(verdict["usable"]),
                "match": (
                    "candidate"
                    if downgrade
                    else ("exact" if verdict["usable"] else ("stale" if verdict["reason"] == "STATUS_NOT_ACTIVE" else "rejected"))
                ),
                "unreachable": False,
                "cache_key": key,
                "asset_id": asset_id,
                "reason": verdict["reason"],
                "detail": verdict["detail"],
                "quality_downgrade": bool(verdict.get("quality_downgrade")),
                "asset": _summary(entry, "exact"),
                "candidates": [],
                "exit_code": verdict["exit_code"],
            }
            if verdict["usable"]:
                payload["exit_code"] = util.EXIT_OK
            return payload

        candidates = self._candidates(computed, project=project, node=node)
        payload = {
            "schema": SCHEMA_LOOKUP,
            "hit": False,
            "usable": False,
            "match": "candidate" if candidates else "miss",
            "unreachable": False,
            "cache_key": key,
            "asset_id": asset_id,
            "reason": "NOT_FOUND",
            "detail": "",
            "quality_downgrade": False,
            "asset": None,
            "candidates": candidates,
            "exit_code": util.EXIT_NO_SOURCE,
        }
        if allow_candidate and confirm:
            for candidate in candidates:
                entry = self._canonical_entry(candidate["asset_id"])
                if entry is None:
                    continue
                try:
                    manifest = self.manifest_for(entry)
                except (OSError, ValueError):
                    continue
                verdict = self._evaluate(
                    entry,
                    manifest,
                    expected_key=None,
                    project=project,
                    node=node,
                    allow_candidate=True,
                    confirm=True,
                    include_stale=False,
                    verify=verify,
                )
                if verdict["usable"]:
                    payload["usable"] = True
                    payload["asset"] = _summary(entry, "candidate")
                    payload["asset"]["match"] = "candidate"
                    payload["quality_downgrade"] = bool(verdict.get("quality_downgrade"))
                    payload["detail"] = "candidate accepted via --allow-candidate --confirm"
                    payload["exit_code"] = util.EXIT_OK
                    break
        return payload

    def _candidates(
        self,
        computed: Dict[str, Any],
        *,
        project: Optional[str],
        node: Optional[str],
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        language = computed["language"]
        normalized_sha = computed["normalized_sha256"]
        canonical_voice = computed["canonical"]["voice"]
        speaker = canonical_voice.get("speaker")
        ref = canonical_voice.get("voice_ref_sha256")
        scored: List[Tuple[int, str, Dict[str, Any]]] = []
        for asset_id, group in self.entries_by_asset().items():
            entry = group[-1]
            if entry.get("status") != "active" or entry.get("shareable") is not True:
                continue
            if not acl_allows(entry.get("scope"), project=project, node=node):
                continue
            if str(entry.get("language") or "") != language:
                continue
            score = 0
            if entry.get("text_normalized_sha256") and entry["text_normalized_sha256"] == normalized_sha:
                score += 3
            if speaker and entry.get("voice_speaker") == speaker:
                score += 2
            if ref and entry.get("voice_ref_sha256") == ref:
                score += 2
            if score <= 0:
                continue
            scored.append((-score, asset_id, _summary(entry, "candidate")))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    # ---------------------------------------------------------------- fetch

    def fetch(
        self,
        asset_id: str,
        dest: Any,
        *,
        project: Optional[str] = None,
        node: Optional[str] = None,
        allow_candidate: bool = False,
        confirm: bool = False,
        verify: bool = True,
    ) -> Dict[str, Any]:
        ok, reason = self.reachable()
        if not ok:
            return {
                "schema": SCHEMA_FETCH,
                "ok": False,
                "unreachable": True,
                "reason": "UNREACHABLE",
                "detail": reason,
                "asset_id": str(asset_id or ""),
                "exit_code": util.EXIT_NO_SOURCE,
            }
        target_id = str(asset_id or "").strip()
        if not ASSET_ID_RE.match(target_id):
            raise TtsUsageError("asset_id 非法: {0!r}".format(asset_id))
        entry = self._canonical_entry(target_id)
        if entry is None:
            return {
                "schema": SCHEMA_FETCH,
                "ok": False,
                "unreachable": False,
                "reason": "ASSET_NOT_FOUND",
                "detail": "",
                "asset_id": target_id,
                "exit_code": util.EXIT_NO_SOURCE,
            }
        try:
            manifest = self.manifest_for(entry)
        except (OSError, ValueError) as exc:
            return {
                "schema": SCHEMA_FETCH,
                "ok": False,
                "unreachable": False,
                "reason": "MANIFEST_INVALID",
                "detail": util.redact(exc),
                "asset_id": target_id,
                "exit_code": util.EXIT_FAIL,
            }
        verdict = self._evaluate(
            entry,
            manifest,
            expected_key=None,
            project=project,
            node=node,
            allow_candidate=allow_candidate,
            confirm=confirm,
            include_stale=False,
            verify=verify,
        )
        if not verdict["usable"]:
            return {
                "schema": SCHEMA_FETCH,
                "ok": False,
                "unreachable": False,
                "reason": verdict["reason"],
                "detail": verdict["detail"],
                "asset_id": target_id,
                "exit_code": verdict["exit_code"],
            }
        dest_path = Path(str(dest)).expanduser()
        if util.is_within(dest_path, self.root) or dest_path.resolve() == self.root.resolve():
            raise TtsForbidden("fetch 目标不得位于库根内")
        dest_path.mkdir(parents=True, exist_ok=True)
        audio = manifest["audio"]
        filename = util.safe_filename(audio["filename"])
        source_audio = self._safe_join("{0}/{1}".format(entry["dir"], filename))
        observed = util.sha256_file(source_audio)
        if verify and observed != audio["sha256"]:
            return {
                "schema": SCHEMA_FETCH,
                "ok": False,
                "unreachable": False,
                "reason": "SHA256_MISMATCH",
                "detail": observed,
                "asset_id": target_id,
                "exit_code": util.EXIT_FAIL,
            }
        tmp_name = ".copy-{0}-{1}".format(os.getpid(), os.urandom(4).hex())
        tmp_audio = dest_path / tmp_name
        try:
            shutil.copyfile(str(source_audio), str(tmp_audio))
            copied = util.sha256_file(tmp_audio)
            if copied != audio["sha256"]:
                raise ValueError("复制后 SHA256 不一致")
            try:
                os.chmod(str(tmp_audio), 0o444)
            except OSError:
                pass
            os.replace(str(tmp_audio), str(dest_path / filename))
        finally:
            if tmp_audio.exists():
                try:
                    os.unlink(str(tmp_audio))
                except OSError:
                    pass
        manifest_path = dest_path / "manifest.json"
        util.atomic_write_json(manifest_path, manifest)
        try:
            os.chmod(str(manifest_path), 0o444)
        except OSError:
            pass
        return {
            "schema": SCHEMA_FETCH,
            "ok": True,
            "unreachable": False,
            "reason": "OK",
            "detail": "",
            "asset_id": target_id,
            "cache_key": manifest.get("cache_key"),
            "quality_downgrade": bool(verdict.get("quality_downgrade")),
            "source": {
                "dir": entry["dir"],
                "manifest_path": entry.get("manifest_path") or "",
                "audio_path": (entry.get("audio") or {}).get("path") or "",
            },
            "dest": {
                "dir": str(dest_path),
                "manifest_path": str(manifest_path),
                "audio_path": str(dest_path / filename),
            },
            "audio": {"sha256": audio["sha256"], "bytes": audio["bytes"]},
            "verified": True,
            "exit_code": util.EXIT_OK,
        }

    # ---------------------------------------------------------------- 锁

    @contextlib.contextmanager
    def _asset_lock(self, asset_id: str, timeout: float) -> Any:
        lock_path = self.locks_dir / (asset_id + ".lock")
        deadline = time.monotonic() + max(0.0, float(timeout))
        handle = None
        payload = {
            "asset_id": asset_id,
            "pid": os.getpid(),
            "created_at": util.format_display(),
        }
        while True:
            try:
                handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(handle, (canonical_json(payload) + "\n").encode("utf-8"))
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = 0
                if age > DEFAULT_LOCK_TIMEOUT:
                    try:
                        lock_path.unlink()
                        self.reclaimed_locks.append(asset_id)
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise LockTimeout("等待资产锁超时: {0}".format(asset_id))
                time.sleep(LOCK_POLL_INTERVAL)
            except OSError as exc:
                raise TtsForbidden("无法创建资产锁: {0}".format(util.redact(exc)))
        try:
            yield lock_path
        finally:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass

    # ---------------------------------------------------------------- publish

    def publish(
        self,
        staging: Any,
        *,
        task_dir: Any,
        project: Optional[str] = None,
        node: Optional[str] = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        allow_candidate: bool = False,
    ) -> Dict[str, Any]:
        task_root = Path(str(task_dir or "")).expanduser()
        if not str(task_dir or "").strip():
            raise TtsUsageError("publish 必须提供 --task-dir（outbox 与审计用）")
        staging_path = Path(str(staging)).expanduser()
        if not staging_path.is_dir():
            raise TtsUsageError("暂存目录不存在: {0}".format(staging_path))

        manifest_path = staging_path / "manifest.json"
        manifest: Any = None
        if manifest_path.is_file():
            try:
                manifest = _read_json(manifest_path)
            except (OSError, ValueError) as exc:
                manifest = None
                read_error = util.redact(exc)
            else:
                read_error = ""
        else:
            read_error = "暂存目录缺少 manifest.json"

        asset_id = str((manifest or {}).get("asset_id") or "")
        audio_path = self._staging_audio(staging_path, manifest or {})
        actual_sha = util.sha256_file(audio_path) if audio_path is not None else ""
        actual_size = audio_path.stat().st_size if audio_path is not None else 0
        idem_seed = asset_id or str(staging_path.resolve())
        idempotency_key = hashlib.sha256(
            (idem_seed + "|" + actual_sha).encode("utf-8")
        ).hexdigest()

        def fail(reason: str, exit_code: int, detail: str = "", **extra: Any) -> Dict[str, Any]:
            outbox = self._write_outbox(
                task_root,
                idempotency_key=idempotency_key,
                asset_id=asset_id,
                cache_key=str((manifest or {}).get("cache_key") or ""),
                audio_sha256=actual_sha,
                staging=str(staging_path),
                status="pending",
                reason=reason,
                exit_code=exit_code,
                detail=detail,
            )
            payload = {
                "schema": SCHEMA_PUBLISH,
                "ok": False,
                "created": False,
                "asset_id": asset_id,
                "cache_key": str((manifest or {}).get("cache_key") or ""),
                "audio_sha256": actual_sha,
                "idempotency_key": idempotency_key,
                "query_back": "fail",
                "reason": reason,
                "detail": detail,
                "outbox": outbox,
                "stale_lock_reclaimed": bool(self.reclaimed_locks),
                "exit_code": exit_code,
            }
            payload.update(extra)
            return payload

        if manifest is None:
            return fail("MANIFEST_INVALID", util.EXIT_USAGE, read_error)
        if audio_path is None:
            return fail("AUDIO_MISSING", util.EXIT_NO_SOURCE, "暂存目录没有可用音频")

        errors = validate_manifest(manifest)
        if errors:
            return fail("MANIFEST_INVALID", util.EXIT_USAGE, "; ".join(errors))
        if str(manifest.get("asset_id") or "") != asset_id:
            return fail("ASSET_ID_MISMATCH", util.EXIT_FAIL)

        sensitive = _sensitive_reason(manifest)
        if sensitive:
            return fail(sensitive, util.EXIT_FORBIDDEN, "shareable=true 且 sensitivity=none 才允许发布")
        try:
            scope = normalize_scope(manifest.get("scope"))
        except TtsUsageError as exc:
            return fail("SCOPE_DENIED", util.EXIT_FORBIDDEN, str(exc))
        if scope["visibility"] == "project" and not scope["projects"]:
            return fail("SCOPE_DENIED", util.EXIT_FORBIDDEN, "project 作用域缺少 projects")
        if manifest.get("quality") != "formal" and not allow_candidate:
            return fail("QUALITY_REJECTED", util.EXIT_FAIL, "质检未通过（需 --allow-candidate 降级）")
        if manifest.get("status") != "active":
            return fail("STATUS_NOT_ACTIVE", util.EXIT_FAIL, str(manifest.get("status")))

        spec = spec_from_manifest(manifest)
        if spec is None:
            return fail("MANIFEST_INVALID", util.EXIT_USAGE, "manifest 文本被截断，无法复核 cache_key")
        try:
            computed = compute_key(spec)
        except TtsUsageError as exc:
            return fail("MANIFEST_INVALID", util.EXIT_USAGE, str(exc))
        key = computed["cache_key"]
        if key != str(manifest.get("cache_key") or ""):
            return fail("CACHE_KEY_MISMATCH", util.EXIT_FAIL, "manifest 字段与 cache_key 不一致")
        if computed["asset_id"] != asset_id:
            return fail("ASSET_ID_MISMATCH", util.EXIT_FAIL, "asset_id 与 cache_key 不一致")

        declared = manifest.get("audio") or {}
        if str(declared.get("sha256") or "") != actual_sha:
            return fail("AUDIO_SHA256_MISMATCH", util.EXIT_FAIL, "音频实测 SHA256 与 manifest 不一致")

        ok, reason, code = self._ensure_writable()
        if not ok:
            return fail("LIBRARY_UNWRITABLE", code, reason)

        try:
            with self._asset_lock(asset_id, lock_timeout):
                result = self._publish_locked(
                    source_audio=audio_path,
                    manifest=manifest,
                    asset_id=asset_id,
                    cache_key=key,
                    actual_sha=actual_sha,
                    actual_size=actual_size,
                )
        except LockTimeout as exc:
            return fail("LOCK_TIMEOUT", util.EXIT_FAIL, str(exc))
        except TtsForbidden as exc:
            return fail("FORBIDDEN", util.EXIT_FORBIDDEN, util.redact(exc))
        except OSError as exc:
            return fail("PUBLISH_IO_ERROR", util.EXIT_FAIL, util.redact(exc))

        if not result["ok"]:
            return fail(result["reason"], result["exit_code"], result.get("detail", ""))

        self.rebuild_index()
        query_back, detail = self._query_back(result["dir"], asset_id, key, actual_sha, actual_size)
        if query_back != "ok":
            return fail("QUERY_BACK_FAILED", util.EXIT_FAIL, detail, created=result["created"], dir=result["dir"], version=result["version"])
        outbox = self._resolve_outbox(task_root, idempotency_key)
        return {
            "schema": SCHEMA_PUBLISH,
            "ok": True,
            "created": result["created"],
            "asset_id": asset_id,
            "cache_key": key,
            "version": result["version"],
            "dir": result["dir"],
            "audio_sha256": actual_sha,
            "audio_bytes": actual_size,
            "idempotency_key": idempotency_key,
            "query_back": "ok",
            "reason": "OK",
            "detail": "",
            "outbox": outbox,
            "stale_lock_reclaimed": bool(self.reclaimed_locks),
            "exit_code": util.EXIT_OK,
        }

    def _publish_locked(
        self,
        *,
        source_audio: Path,
        manifest: Dict[str, Any],
        asset_id: str,
        cache_key: str,
        actual_sha: str,
        actual_size: int,
    ) -> Dict[str, Any]:
        group = self.entries_by_asset().get(asset_id, [])
        for entry in group:
            try:
                existing = self.manifest_for(entry)
            except (OSError, ValueError):
                continue
            if str((existing.get("audio") or {}).get("sha256") or "") == actual_sha:
                return {"ok": True, "created": False, "dir": entry["dir"], "version": int(entry.get("version") or 1), "reason": "OK", "detail": ""}

        version = max([int(entry.get("version") or 1) for entry in group] or [0]) + 1
        dirname = asset_id if version == 1 else "{0}-v{1}".format(asset_id, version)
        target = self.root / dirname
        supersedes = group[-1]["dir"] if group else manifest.get("supersedes")
        document = dict(manifest)
        document["asset_id"] = asset_id
        document["cache_key"] = cache_key
        document["audio"] = {
            "filename": actual_sha + ".wav",
            "bytes": actual_size,
            "sha256": actual_sha,
            "duration_seconds": (manifest.get("audio") or {}).get("duration_seconds"),
        }
        document["supersedes"] = supersedes
        document["created_at"] = document.get("created_at") or util.format_display()
        provenance = dict(document.get("provenance") or {})
        provenance.setdefault("generated_at", util.format_display())
        document["provenance"] = provenance

        tmp_dir = Path(tempfile.mkdtemp(prefix=".{0}.".format(dirname), dir=str(self.incoming_dir)))
        try:
            util.atomic_write_json(tmp_dir / "manifest.json", document)
            staged_audio = tmp_dir / (actual_sha + ".wav")
            shutil.copyfile(str(source_audio), str(staged_audio))
            copied = util.sha256_file(staged_audio)
            if copied != actual_sha:
                raise ValueError("暂存音频复制后 SHA256 不一致")
            try:
                os.chmod(str(staged_audio), 0o444)
                os.chmod(str(tmp_dir / "manifest.json"), 0o444)
            except OSError:
                pass
            incoming_target = self.incoming_dir / dirname
            if incoming_target.exists():
                shutil.rmtree(str(incoming_target), ignore_errors=True)
            os.replace(str(tmp_dir), str(incoming_target))
            try:
                os.replace(str(incoming_target), str(target))
            except OSError:
                # 并发发布者先落地：同 SHA 视为幂等成功，否则顺延版本号。
                if target.is_dir():
                    try:
                        existing = _read_json(target / "manifest.json")
                    except (OSError, ValueError):
                        existing = {}
                    if str((existing.get("audio") or {}).get("sha256") or "") == actual_sha:
                        shutil.rmtree(str(incoming_target), ignore_errors=True)
                        return {"ok": True, "created": False, "dir": dirname, "version": version, "reason": "OK", "detail": ""}
                shutil.rmtree(str(incoming_target), ignore_errors=True)
                raise
            return {"ok": True, "created": True, "dir": dirname, "version": version, "reason": "OK", "detail": ""}
        finally:
            if tmp_dir.exists():
                shutil.rmtree(str(tmp_dir), ignore_errors=True)

    def _staging_audio(self, staging_path: Path, manifest: Dict[str, Any]) -> Optional[Path]:
        audio = manifest.get("audio") or {}
        candidates: List[Path] = []
        filename = str(audio.get("filename") or "")
        if filename:
            try:
                candidates.append(staging_path / util.safe_filename(filename))
            except util.PathForbidden:
                pass
        sha = str(audio.get("sha256") or "")
        if SHA256_RE.match(sha):
            candidates.append(staging_path / (sha + ".wav"))
        try:
            for child in sorted(staging_path.iterdir(), key=lambda item: item.name):
                if child.is_file() and child.name != "manifest.json" and child not in candidates:
                    candidates.append(child)
        except OSError:
            pass
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _query_back(self, dirname: str, asset_id: str, cache_key: str, sha: str, size: int) -> Tuple[str, str]:
        directory = self.root / dirname
        try:
            manifest = _read_json(directory / "manifest.json")
        except (OSError, ValueError) as exc:
            return "fail", util.redact(exc)
        if manifest.get("asset_id") != asset_id:
            return "fail", "asset_id 不一致"
        if manifest.get("cache_key") != cache_key:
            return "fail", "cache_key 不一致"
        entry = _entry_from_manifest(manifest, dirname)
        state, observed, observed_size = self.audio_state(entry, manifest)
        if state != "OK":
            return "fail", "音频校验失败: {0}".format(state)
        if observed != sha or observed_size != size:
            return "fail", "音频 SHA/体积与发布输入不一致"
        return "ok", ""

    # ---------------------------------------------------------------- outbox

    def _outbox_dir(self, task_root: Path) -> Path:
        return task_root / "receipts" / "outbox"

    def _write_outbox(
        self,
        task_root: Path,
        *,
        idempotency_key: str,
        asset_id: str,
        cache_key: str,
        audio_sha256: str,
        staging: str,
        status: str,
        reason: str,
        exit_code: int,
        detail: str = "",
    ) -> str:
        directory = self._outbox_dir(task_root)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (idempotency_key + ".json")
        existing: Dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = _read_json(path)
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, ValueError):
                existing = {}
        now = util.format_display()
        entry = dict(existing)
        entry.update(
            {
                "schema": SCHEMA_OUTBOX,
                "idempotency_key": idempotency_key,
                "asset_id": asset_id,
                "cache_key": cache_key,
                "audio_sha256": audio_sha256,
                "staging": staging,
                "task_dir": str(task_root),
                "status": status,
                "reason": reason,
                "exit_code": int(exit_code),
                "detail": util.redact(detail),
                "updated_at": now,
            }
        )
        entry.setdefault("created_at", now)
        entry["attempts"] = int(existing.get("attempts") or 0) + 1
        util.atomic_write_json(path, entry)
        self.last_outbox = str(path)
        return str(path)

    def _resolve_outbox(self, task_root: Path, idempotency_key: str) -> str:
        path = self._outbox_dir(task_root) / (idempotency_key + ".json")
        if not path.is_file():
            return ""
        try:
            entry = _read_json(path)
        except (OSError, ValueError):
            return str(path)
        if isinstance(entry, dict):
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["status"] = "resolved"
            entry["reason"] = "OK"
            entry["exit_code"] = util.EXIT_OK
            entry["resolved_at"] = util.format_display()
            entry["updated_at"] = entry["resolved_at"]
            util.atomic_write_json(path, entry)
        return str(path)

    # ---------------------------------------------------------------- verify

    def verify(self, asset_id: Optional[str] = None, *, quarantine: bool = True) -> Dict[str, Any]:
        ok, reason = self.reachable()
        if not ok:
            return {
                "schema": SCHEMA_VERIFY,
                "ok": False,
                "unreachable": True,
                "reason": "UNREACHABLE",
                "detail": reason,
                "checked": 0,
                "corrupt": 0,
                "assets": [],
                "quarantined": [],
                "exit_code": util.EXIT_NO_SOURCE,
            }
        entries = self.scan_entries()
        if asset_id:
            entries = [entry for entry in entries if entry["asset_id"] == asset_id]
            if not entries:
                return {
                    "schema": SCHEMA_VERIFY,
                    "ok": False,
                    "unreachable": False,
                    "reason": "ASSET_NOT_FOUND",
                    "detail": "",
                    "checked": 0,
                    "corrupt": 0,
                    "assets": [],
                    "quarantined": [],
                    "exit_code": util.EXIT_NO_SOURCE,
                }
        rows: List[Dict[str, Any]] = []
        quarantined: List[str] = []
        corrupt = 0
        for entry in entries:
            try:
                manifest = self.manifest_for(entry)
            except (OSError, ValueError) as exc:
                state, observed, size = "CORRUPT", "", 0
                detail = util.redact(exc)
            else:
                state, observed, size = self.audio_state(entry, manifest)
                detail = ""
            row = {
                "asset_id": entry["asset_id"],
                "dir": entry["dir"],
                "version": entry["version"],
                "state": state,
                "expected_sha256": entry["audio"]["sha256"],
                "observed_sha256": observed,
                "bytes": size,
                "detail": detail,
            }
            if state != "OK":
                corrupt += 1
                if quarantine:
                    row["quarantined_to"] = self._quarantine(entry)
                    if row["quarantined_to"]:
                        quarantined.append(row["quarantined_to"])
            rows.append(row)
        self.rebuild_index()
        return {
            "schema": SCHEMA_VERIFY,
            "ok": corrupt == 0,
            "unreachable": False,
            "reason": "OK" if corrupt == 0 else "CORRUPT",
            "detail": "",
            "checked": len(rows),
            "corrupt": corrupt,
            "assets": rows,
            "quarantined": quarantined,
            "exit_code": util.EXIT_OK if corrupt == 0 else util.EXIT_FAIL,
        }

    def _quarantine(self, entry: Dict[str, Any]) -> str:
        source = self.root / entry["dir"]
        if not source.exists():
            return ""
        try:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            destination = self.quarantine_dir / "{0}-{1}".format(entry["dir"], util.format_stamp())
            if destination.exists():
                destination = self.quarantine_dir / "{0}-{1}-{2}".format(
                    entry["dir"], util.format_stamp(), os.getpid()
                )
            os.replace(str(source), str(destination))
            return str(destination.relative_to(self.root))
        except OSError:
            return ""

    # ---------------------------------------------------------------- index / status

    def index(self) -> Dict[str, Any]:
        ok, reason = self.reachable()
        if not ok:
            return {
                "schema": SCHEMA_INDEX,
                "ok": False,
                "unreachable": True,
                "reason": "UNREACHABLE",
                "detail": reason,
                "count": 0,
                "assets": [],
                "exit_code": util.EXIT_NO_SOURCE,
            }
        payload = self.rebuild_index()
        result = dict(payload)
        result["ok"] = True
        result["unreachable"] = False
        result["reason"] = "OK"
        result["exit_code"] = util.EXIT_OK
        return result

    def status(self) -> Dict[str, Any]:
        ok, reason = self.reachable()
        if not ok:
            return {
                "schema": SCHEMA_STATUS,
                "ok": False,
                "reachable": False,
                "unreachable": True,
                "status": "UNREACHABLE",
                "reason": "UNREACHABLE",
                "detail": reason,
                "root_kind": "1006",
                "counts": {},
                "total_bytes": 0,
                "exit_code": util.EXIT_NO_SOURCE,
            }
        entries = self.scan_entries()
        counts: Dict[str, Any] = {
            "assets": len(entries),
            "unique_assets": len({entry["asset_id"] for entry in entries}),
            "active": sum(1 for entry in entries if entry["status"] == "active"),
            "candidate": sum(1 for entry in entries if entry["quality"] == "candidate"),
            "formal": sum(1 for entry in entries if entry["quality"] == "formal"),
            "by_visibility": {},
        }
        total = 0
        for entry in entries:
            visibility = entry["visibility"] or "unknown"
            counts["by_visibility"][visibility] = counts["by_visibility"].get(visibility, 0) + 1
            total += int((entry.get("audio") or {}).get("bytes") or 0)
        counts["by_visibility"] = dict(sorted(counts["by_visibility"].items()))
        return {
            "schema": SCHEMA_STATUS,
            "ok": True,
            "reachable": True,
            "unreachable": False,
            "status": "REACHABLE",
            "reason": "OK",
            "detail": "",
            "root_kind": "1006",
            "counts": counts,
            "total_bytes": total,
            "index": {
                "path": "index.json",
                "exists": self.index_path.is_file(),
            },
            "exit_code": util.EXIT_OK,
        }
