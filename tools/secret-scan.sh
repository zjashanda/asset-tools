#!/usr/bin/env bash
# tools/secret-scan.sh —— 敏感信息扫描薄封装（CI 安全门禁）
#
# 用法：
#   bash tools/secret-scan.sh              # 扫描仓库根
#   bash tools/secret-scan.sh --gate       # 门禁模式（有命中即退出 1）
#   bash tools/secret-scan.sh --self-test  # 用内存样本自检五类规则
#   bash tools/secret-scan.sh --root DIR   # 扫描指定目录
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/tools/secret_scan.py" "$@"
