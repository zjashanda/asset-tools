#!/usr/bin/env python3
"""仓库敏感信息扫描（CI 安全门禁）。

五类规则
--------
1. GitHub token：``gh[pousr]_`` 与 ``github_pat_``。
2. 云厂商 AccessKey：``AKIA`` / ``LTAI`` 形态。
3. 私钥头：``-----BEGIN (RSA|OPENSSH|EC|PGP|PRIVATE) ``。
4. 赋值型凭据：``password|passwd|secret|token|access_key`` 后接 ``=``/``:`` 与长值。
5. 禁用文件名：``.git-credentials``、``id_rsa``、``id_ed25519``、``*.pem``、``*.pfx``、``auth.json``。

用法::

    python3 tools/secret_scan.py              # 扫描仓库根
    python3 tools/secret_scan.py --root DIR   # 扫描指定目录
    python3 tools/secret_scan.py --self-test  # 用内存样本自检五类规则
    python3 tools/secret_scan.py --json

退出码：0 干净 / 1 有命中（或自检失败）。
"""

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
DEFAULT_ALLOWLIST = TOOLS_DIR / "scan-allowlist.txt"

MAX_FILE_BYTES = 4 * 1024 * 1024
HEAD_BYTES = 8192
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ark-supply",
    "build",
    "dist",
}

#: 行内放行标记（与 ark-archive 保持一致）
INLINE_ALLOW = "ark-scan:allow"

RULES: List[Tuple[str, Any, str]] = [
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub fine-grained PAT"),
    (
        "cloud-access-key",
        re.compile(r"\b(?:AKIA[0-9A-Z]{16}|LTAI[A-Za-z0-9]{12,})\b"),
        "AWS / 阿里云 AccessKey",
    ),
    ("private-key", re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|PGP|PRIVATE) "), "私钥头"),
    (
        "assigned-secret",
        re.compile(r"(?i)(?:password|passwd|secret|token|access_key)\s*[=:]\s*[\"']?[^\s\"'$<{]{8,}"),
        "赋值型凭据",
    ),
]

FILENAME_PATTERNS = (
    ".git-credentials",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.pem",
    "*.pfx",
    "auth.json",
)


def mask(snippet: str) -> str:
    text = str(snippet)
    if len(text) <= 6:
        return text[:2] + "***"
    return text[:4] + "***"


def looks_binary(path: Path) -> bool:
    try:
        with open(str(path), "rb") as handle:
            head = handle.read(HEAD_BYTES)
    except OSError:
        return True
    return b"\x00" in head


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                if path.is_symlink() and not path.exists():
                    continue
            except OSError:
                continue
            yield path


def filename_hits(path: Path, root: Path) -> List[Dict[str, Any]]:
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    hits: List[Dict[str, Any]] = []
    for pattern in FILENAME_PATTERNS:
        if fnmatch.fnmatch(path.name, pattern):
            hits.append(
                {
                    "file": rel.replace(os.sep, "/"),
                    "line": 0,
                    "rule": "forbidden-filename",
                    "detail": "命中禁用文件名规则 {0}".format(pattern),
                    "masked": mask(path.name),
                }
            )
            break
    return hits


def load_allowlist(path: Optional[Path]) -> List[Any]:
    target = path or DEFAULT_ALLOWLIST
    entries: List[Any] = []
    if not target or not Path(target).is_file():
        return entries
    try:
        text = Path(target).read_text(encoding="utf-8")
    except OSError:
        return entries
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entries.append(re.compile(line))
        except re.error:
            continue
    return entries


def _allowed(line: str, snippet: str, allowlist: List[Any]) -> bool:
    if INLINE_ALLOW in line:
        return True
    for pattern in allowlist:
        if pattern.search(line) or pattern.search(snippet):
            return True
    return False


def scan_file(path: Path, root: Path, allowlist: List[Any]) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    try:
        rel = str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        rel = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return hits
    if size > MAX_FILE_BYTES or looks_binary(path):
        return hits
    try:
        handle = open(str(path), "r", encoding="utf-8", errors="replace")
    except OSError:
        return hits
    with handle:
        for lineno, line in enumerate(handle, start=1):
            for name, pattern, _description in RULES:
                match = pattern.search(line)
                if not match:
                    continue
                snippet = match.group(0)
                if _allowed(line, snippet, allowlist):
                    continue
                hits.append(
                    {
                        "file": rel,
                        "line": lineno,
                        "rule": name,
                        "detail": _description,
                        "masked": mask(snippet),
                    }
                )
    return hits


def scan_tree(root: Path, allowlist: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(root).resolve()
    entries = load_allowlist(allowlist)
    hits: List[Dict[str, Any]] = []
    scanned = 0
    for path in iter_files(target):
        hits.extend(filename_hits(path, target))
        if not path.is_file():
            continue
        scanned += 1
        hits.extend(scan_file(path, target, entries))
    hits.sort(key=lambda item: (item["file"], item["line"], item["rule"]))
    return {
        "root": str(target),
        "scanned": scanned,
        "hits": hits,
        "ok": not hits,
    }


# --------------------------------------------------------------------- 自检


def self_test() -> Tuple[bool, List[str]]:
    """用内存样本验证五类规则都能命中，且干净文本不误报。"""
    problems: List[str] = []
    covered = set()
    samples = [
        ("github-token", "token is " + "ghp_" + "a" * 36),
        ("github-pat", "pat " + "github_pat_" + "B" * 30),
        ("cloud-access-key", "id " + "AKIA" + "Q" * 16),
        ("private-key", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
        ("assigned-secret", "DB_PASSWORD=" + "Sup3rS3cret!"),
    ]
    for expected, sample in samples:
        matched = [name for name, pattern, _ in RULES if pattern.search(sample)]
        if expected not in matched:
            problems.append("自检失败：样本未命中规则 {0}（实际 {1}）".format(expected, matched))
        covered.update(matched)
    clean = [
        "SCHEDULE = 30",
        "EMAIL_PASSWORD=<env>",
        "ARK_OSS_ACCESS_KEY_SECRET=<env>",
        "timeout_seconds: 120",
        "password_note = 只出现变量名",
    ]
    for sample in clean:
        matched = [name for name, pattern, _ in RULES if pattern.search(sample)]
        if matched:
            problems.append("自检误报：{0!r} 命中 {1}".format(sample, matched))
    expected_rules = {"github-token", "github-pat", "cloud-access-key", "private-key", "assigned-secret"}
    missing = expected_rules - covered
    if missing:
        problems.append("自检未覆盖规则: {0}".format(", ".join(sorted(missing))))
    if not any(fnmatch.fnmatch("id_rsa", pattern) for pattern in FILENAME_PATTERNS):
        problems.append("自检失败：禁用文件名规则未覆盖 id_rsa")
    if not any(fnmatch.fnmatch("server.pem", pattern) for pattern in FILENAME_PATTERNS):
        problems.append("自检失败：禁用文件名规则未覆盖 *.pem")
    return (not problems), problems


# --------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="仓库敏感信息扫描")
    parser.add_argument("--root", metavar="DIR", default=str(REPO_ROOT), help="扫描根目录")
    parser.add_argument("--allowlist", metavar="PATH", default="", help="白名单文件（每行一个正则）")
    parser.add_argument("--self-test", action="store_true", help="用内存样本自检五类规则")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    parser.add_argument("--gate", action="store_true", help="门禁模式：有命中即退出 1")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.self_test:
        ok, problems = self_test()
        if args.json:
            print(json.dumps({"ok": ok, "problems": problems}, ensure_ascii=False, indent=2))
        else:
            if ok:
                print("SELF-TEST: PASS（五类规则均可命中，干净样本无误报）")
            else:
                print("SELF-TEST: FAIL", file=sys.stderr)
                for problem in problems:
                    print("  - " + problem, file=sys.stderr)
        return 0 if ok else 1

    allowlist = Path(args.allowlist).expanduser() if args.allowlist else None
    report = scan_tree(Path(args.root), allowlist=allowlist)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if report["ok"]:
            if not args.quiet:
                print("PASS —— 未发现敏感信息（扫描 {0} 个文件）".format(report["scanned"]))
        else:
            print(
                "FAIL —— 发现 {0} 处疑似敏感信息（扫描 {1} 个文件）".format(
                    len(report["hits"]), report["scanned"]
                ),
                file=sys.stderr,
            )
            for hit in report["hits"]:
                print(
                    "{0}:{1}: {2} [masked {3}]".format(
                        hit["file"], hit["line"], hit["rule"], hit["masked"]
                    ),
                    file=sys.stderr,
                )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
