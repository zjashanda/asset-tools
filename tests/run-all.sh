#!/bin/sh
# tests/run-all.sh —— 一键质量门禁：单元测试 + shell 语法 + 敏感信息扫描
#
# 用法（仓库根或任意目录）：
#   sh tests/run-all.sh        # POSIX sh 兼容（dash 也能跑）
#   bash tests/run-all.sh
# 退出码：0 全部通过；1 有失败项。
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd "$ROOT" || exit 1

FAILED=0
section() { printf '\n===== %s =====\n' "$1"; }
fail() { echo "FAIL: $1" >&2; FAILED=1; }

section "1/3 Python 单元测试"
if python3 -m unittest discover -s tests -p 'test_*.py' -v; then
  echo "unittest: PASS"
else
  fail "unittest"
fi

section "2/3 shell 语法检查"
if command -v bash >/dev/null 2>&1; then
  for script in tests/run-all.sh tools/secret-scan.sh bin/asset-tools; do
    if [ -f "$script" ]; then
      if bash -n "$script"; then
        echo "语法 OK: $script"
      else
        fail "语法错误: $script"
      fi
    fi
  done
else
  echo "提示: 本机没有 bash，跳过 shell 语法检查"
fi

section "3/3 敏感信息扫描"
if python3 tools/secret_scan.py --self-test; then
  echo "secret-scan self-test: PASS"
else
  fail "secret-scan self-test"
fi
if python3 tools/secret_scan.py --gate; then
  echo "secret-scan: PASS"
else
  fail "secret-scan"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "PASS —— 全部检查通过"
  exit 0
fi
echo "FAIL —— 存在失败项，见上方输出"
exit 1
