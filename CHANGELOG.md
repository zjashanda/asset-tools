# Changelog

本文件记录 `asset-tools` 的重要变更。版本号遵循语义化版本。

## v0.1.0 — 2026-11-10

首个版本，把分散在各项目的资产取件脚本收成一条链：发现、清单、校验、缓存、供应。

- manifest：新增 `ark.asset-manifest.v1` 与 `ark.supply-record.v1` 两份 schema；`load_any` 自动识别并转换 `ark-artifact-manifest.v1` 与 `venusahisiburner-assets/v1`；`merge` 按 id 对齐，同摘要合并来源，异摘要按 `discovered_at` 取舍并把被替换方记进 `provenance.note`。
- 缓存：内容寻址布局 `sha256/<前2>/<次2>/<sha256>/<filename>`，写入先落 `_incoming` 再原子 rename，校验 `OK`/`MISSING`/`CORRUPT`，坏副本移入 `_quarantine`，`prune` 默认 dry-run。
- 适配器：`local`、`http`、`oss`、`node-storage`、`email`、`nas` 六个来源；`probe` 不抛异常，缺凭据或缺 CLI 一律 `BLOCKED:ADAPTER_UNAVAILABLE` 并只列变量名；`email` / `nas` 委托外部 CLI，`node-storage` 走 `sshpass -e` 并校验 `base_dir` 边界。
- 供应：`supply_asset` 从缓存复制到目标目录，落位后重算 sha256，`--apply` 时写 `ark.supply-record.v1`，硬字段 `is_receipt: false` 与 `archive_owner: ark-archive` 明确本仓不产出归档回执。
- 安全：`util.redact()` 统一脱敏；`tools/secret_scan.py` 覆盖 GitHub token、云厂商 key、私钥头、赋值型密码、禁用文件名五类规则，支持 `--self-test` 与 `--gate`；`config/env-keys.json` 只列变量名，值一律 `<env>`。
- 测试：`unittest` 覆盖时间契约、三种 schema 互转与 merge、缓存写入与隔离与路径穿越拒绝、六个适配器在缺变量时的 BLOCKED 行为、supply dry-run 与 apply、CLI 退出码与 `--json` 可解析；全部用例可在无网络、无凭据、无 `sshpass` 的机器上跑绿。
