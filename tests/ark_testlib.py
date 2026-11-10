# -*- coding: utf-8 -*-
"""测试公共工具：仓库定位、子进程、环境隔离、fixture 组装。

本文件不是测试用例（文件名不以 ``test_`` 开头），只被各测试模块导入。
全部仅用标准库；不联网、不 ssh、不触碰仓库外路径（临时目录除外）。
"""

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures")
BIN_DIR = os.path.join(REPO_ROOT, "bin")

FIRMWARE_FIXTURE = os.path.join(FIXTURES_DIR, "sample-firmware.txt")
TOOL_FIXTURE = os.path.join(FIXTURES_DIR, "sample-tool.txt")

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

#: 一律从测试环境里清掉的凭据类变量（只按名字判断）
CREDENTIAL_ENV_NAMES = (
    "ARK_STORAGE_SSH_PASS",
    "ARK_STORAGE_HOST",
    "ARK_STORAGE_USER",
    "ARK_STORAGE_PORT",
    "ARK_STORAGE_BASE_DIR",
    "ARK_OSS_ENDPOINT",
    "ARK_OSS_BUCKET",
    "ARK_OSS_ACCESS_KEY_ID",
    "ARK_OSS_ACCESS_KEY_SECRET",
    "ASSET_OSS_CLI",
    "ASSET_EMAIL_CLI",
    "ASSET_NAS_CLI",
    "ASSET_HTTP_TIMEOUT",
    "ASSET_ADAPTER_TIMEOUT",
    "EMAIL_ACCOUNT",
    "EMAIL_PASSWORD",
    "NAS_ACCOUNT",
    "NAS_PASSWORD",
    "GITHUB_TOKEN",
    "ARK_ASSET_CACHE",
    "ARK_ASSET_MANIFEST",
    "ARK_ASSET_CONFIG",
    "ARK_NODE_NAME",
    "SSHPASS",
)


def python_exe():
    return sys.executable or "python3"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path, payload):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def clean_env(**overrides):
    """返回一份不携带凭据环境的 env 字典（供子进程使用）。"""
    env = os.environ.copy()
    for name in CREDENTIAL_ENV_NAMES:
        env.pop(name, None)
    for name in list(env.keys()):
        upper = name.upper()
        if upper.endswith("_PASSWORD") or upper.endswith("_TOKEN") or upper.endswith("_SECRET"):
            env.pop(name, None)
    env["PYTHONPATH"] = REPO_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


@contextlib.contextmanager
def env_guard(**overrides):
    """进程内环境隔离：清掉凭据变量、写入指定变量，退出时完整还原。"""
    saved = dict(os.environ)
    try:
        for name in CREDENTIAL_ENV_NAMES:
            os.environ.pop(name, None)
        for name in list(os.environ.keys()):
            upper = name.upper()
            if upper.endswith("_PASSWORD") or upper.endswith("_TOKEN") or upper.endswith("_SECRET"):
                os.environ.pop(name, None)
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextlib.contextmanager
def temp_dir(prefix="asset-tools-test-"):
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def run(args, env=None, cwd=None, timeout=180):
    """跑子进程（text=True），返回 CompletedProcess。"""
    return subprocess.run(
        args,
        cwd=cwd or REPO_ROOT,
        env=env if env is not None else clean_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_module(args, extra_env=None, cwd=None, timeout=180):
    """``python -m asset_tools …``。"""
    env = clean_env(**(extra_env or {}))
    return run([python_exe(), "-m", "asset_tools"] + list(args), env=env, cwd=cwd, timeout=timeout)


def run_bin(args, extra_env=None, timeout=180):
    """通过 ``bin/asset-tools`` 薄封装执行。"""
    env = clean_env(**(extra_env or {}))
    script = os.path.join(BIN_DIR, "asset-tools")
    return run(["bash", script] + list(args), env=env, timeout=timeout)


def json_stdout(completed):
    """解析子进程 stdout 的 JSON（失败时把 stderr 带进断言信息）。"""
    assert completed.stdout.strip(), "stdout 为空; stderr={0}".format(completed.stderr)
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise AssertionError(
            "stdout 不是 JSON: {0}\nstdout={1}\nstderr={2}".format(exc, completed.stdout, completed.stderr)
        )


def asset_doc(path, asset_id="fixture", kind="firmware", material_rel=None, sources=None, status="verified"):
    """按真实文件内容生成一个 asset 字典（sha256/bytes 真实计算）。"""
    digest = sha256_file(path)
    name = os.path.basename(path)
    entry = {
        "id": asset_id,
        "kind": kind,
        "name": asset_id,
        "version": "1.0.0",
        "filename": name,
        "bytes": os.path.getsize(path),
        "sha256": digest,
        "project": "fixture-project",
        "sources": sources if sources is not None else [{"type": "local", "path": os.path.abspath(path), "priority": 5}],
        "install": {
            "cache_rel": "sha256/{0}/{1}/{2}/{3}".format(digest[:2], digest[2:4], digest, name),
            "material_rel": material_rel or name,
        },
        "provenance": {"origin": "local", "note": "test fixture", "discovered_at": "2026-11-10 10:00:00"},
        "status": status,
    }
    return entry


def manifest_doc(*assets):
    return {
        "schema": "ark.asset-manifest.v1",
        "generated_at": "2026-11-10 10:00:00",
        "assets": list(assets),
    }
