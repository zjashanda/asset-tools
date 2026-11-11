# asset-tools

> 本仓库只包含脚本、schema、测试与文档，不含任何凭据；凭据一律由环境变量或受控本机配置提供。

**版本**：`v0.1.0` · **状态**：初始版本（initial） · **平台**：Linux（bash、python3.8+）与 Windows（PowerShell 5.1、python 3.8+）

## 这是什么

`asset-tools` 是方舟资产供应层的 canonical 公共仓库。它只做五件事：发现资产、维护清单、校验 SHA-256、按内容寻址缓存、把资产供应到目标目录。

这五件事此前散在各个项目脚本里，同一份固件在不同机器上有不同的名字、不同的校验方式、不同的落盘位置。本仓把流程收成一条链：

```
discover ──► manifest ──► cache（sha256 内容寻址）──► supply ──► supply record
```

链上的每一段都只依赖上一段留下的可复核证据：manifest 里有 sha256，缓存路径由 sha256 决定，供应记录里记下落位后的 sha256。任何一环对不上，流程就停在原地并给出可读的原因。

三条设计目标：

- **来源可替换**：同一份资产可以同时挂在 node-storage、OSS、NAS、邮件、HTTP、本机六个来源上，按 `priority` 依次尝试，谁先通谁上。来源都是适配器，加新来源不动主流程。
- **落盘可复核**：缓存与落位都重算 sha256，不一致进隔离区，不覆盖、不静默降级。
- **默认只读**：没有 `--apply` 就是 dry-run，打印 `DRY-RUN` 前缀，JSON 里 `"applied": false`。

## 它不做什么（边界）

- **不归档结果、不产出 receipt**。归档与回执归 `ark-archive`。供应记录里硬写 `"archive_owner": "ark-archive"` 与 `"is_receipt": false`，就是为了让下游一眼看出这不是归档凭证。
- **不烧录、不跑测试**。烧录归 `venusahisiburner`，测试执行归 `lstest`。本仓只负责把正确的二进制放到正确的位置。
- **不实现邮件协议**。`email` 适配器委托 `qw-email-handle`，本仓不碰 IMAP/SMTP，也不读邮箱口令。
- **不实现 NAS 协议**。`nas` 适配器委托 `fileball`，本仓不碰 FTP、WebDAV、FileStation，也不读 NAS 口令。
- **不引第三方依赖**。只跑标准库（Python 3.8+），OSS 也不引 SDK。
- **不保存凭据**。仓库里只出现环境变量名字；日志、manifest、供应记录、异常信息统一过 `util.redact()`。

## 与相邻仓库的分工

| 仓库 | 职责 | 与本仓的接口 |
|---|---|---|
| `ark-archive` | 结果归档、远端复核、生成 receipt | 本仓只管「把资产放到位」，不放 receipt；供应记录的 `archive_owner` / `is_receipt` 两个字段划清归属 |
| `venusahisiburner` | 烧录执行 | 消费本仓的 manifest；`manifest convert --to venusahisiburner` 可直接还原成它认识的 `venusahisiburner-assets/v1` |
| `lstest` | 测试执行 | 从缓存或 supply 落位目录取测试材料，只读 |
| `qw-email-handle` | 企业邮箱附件 CLI | `email` 适配器委托它 `list --json` 与 `get <id> --out <DIR>` |
| `fileball` | 内网 NAS / FileStation CLI | `nas` 适配器委托同样两条命令 |
| `ark-mesh` | 老的节点间取件与产物清单 | 旧的 `artifact-manifest.json` 可被 `load_any` 直接读入并转成新 schema |
| `ark-node` | 节点环境同步与取件入口 | 共用同一套时间契约与退出码；`~/.config/ark/mesh-sources.json` 始终是节点本地文件，永不进仓库 |

## 目录结构

```
asset-tools/
├── README.md  CHANGELOG.md  MIGRATION.md  LICENSE  VERSION
├── pyproject.toml
├── bin/asset-tools            # bash 薄封装
├── bin/asset-tools.ps1        # PowerShell 薄封装
├── asset_tools/
│   ├── __init__.py            # __version__ 从 VERSION 读取，缺省常量 "0.1.0"
│   ├── util.py                # 时间契约、退出码、sha256、脱敏
│   ├── manifest.py            # AssetManifest / Asset / load_any / merge / 两种导出
│   ├── cache.py               # ContentCache：内容寻址、校验、隔离、prune
│   ├── supply.py              # supply_asset：缓存→目标目录→供应记录
│   ├── status.py              # 全部适配器 probe + 缓存统计
│   ├── cli.py                 # 参数解析与子命令分发
│   └── adapters/
│       ├── __init__.py        # ADAPTERS registry、get_adapter(name)、probe_all()
│       ├── base.py            # AdapterStatus / AssetRef / FetchResult / Adapter
│       ├── local.py  http.py  oss.py  node_storage.py  email.py  nas.py
├── schemas/asset-manifest.v1.json
├── schemas/supply-record.v1.json
├── config/providers.example.json
├── config/env-keys.json
├── tests/run-all.sh
├── tests/ark_testlib.py   tests/test_*.py   tests/fixtures/
├── tools/secret_scan.py   tools/secret-scan.sh
└── docs/{architecture,contracts,adapters,cache,security,rollback,runbook}.md
```

- `asset_tools/util.py`：全仓唯一的时间与退出码来源，也是唯一做脱敏的地方。
- `asset_tools/manifest.py`：清单模型与三种 schema 的互转，`merge` 的幂等规则也在这里。
- `asset_tools/cache.py`：内容寻址缓存的全部写路径都在这里，路径穿越在这里被拒。
- `asset_tools/adapters/`：一个来源一个文件，只实现 `probe` / `discover` / `fetch` 三个方法。
- `schemas/`：两份 JSON Schema（draft-07），是契约的机器可读版本。
- `config/env-keys.json`：只列环境变量名字，值一律写 `<env>`，兼作扫描白名单的依据。
- `tools/`：仓库自检，`secret_scan.py` 扫凭据，`secret-scan.sh` 是给 CI 与本地钩子用的薄封装。

## 5 分钟上手

三条命令，全部默认 dry-run。

```bash
cd asset-tools

# 1) 看这台机器能用哪些来源、缓存多大、manifest 默认路径是什么
bash bin/asset-tools status

# 2) 只读计划：每个资产是缓存命中还是要取件，按哪个来源取
bash bin/asset-tools plan --manifest ./assets.json

# 3) 供应到目标目录：默认只打印计划，加 --apply 才真正落盘
bash bin/asset-tools supply --manifest ./assets.json --asset pvm1005-firmware --dest ./inbox
```

`python3 -m asset_tools ...` 与 `bash bin/asset-tools ...` 等价；Windows 上用 `.\bin\asset-tools.ps1`，参数与退出码透传。

`status` 会打印本机的 manifest 默认路径。上面的示例显式传 `--manifest`，是为了不依赖默认值。

`./assets.json` 换成你手头的清单路径即可；还没有清单时见本节的 `register` 示例。

确认计划无误后再落盘：

```bash
bash bin/asset-tools supply --manifest ./assets.json --asset pvm1005-firmware --dest ./inbox --apply
```

供应记录写到 `<dest>/.ark-supply/<asset_id>.json`，schema 是 `ark.supply-record.v1`，也可以显式指定记录目录。记录里的 `dest_sha256_verified: true` 表示落位后重算过哈希。

手头还没有 manifest 时，从本机文件登记一条（`register` 也是写操作，先 dry-run 再 `--apply`）：

```bash
bash bin/asset-tools register --manifest ./assets.json \
  --file ./pvm1005-v1.2.3.zip --id pvm1005-firmware --kind firmware \
  --version 1.2.3 --project tcl-pvm1005 --name "PVM1005 固件"

bash bin/asset-tools register --manifest ./assets.json \
  --file ./pvm1005-v1.2.3.zip --id pvm1005-firmware --kind firmware \
  --version 1.2.3 --project tcl-pvm1005 --name "PVM1005 固件" --apply
```

`register` 会算 sha256 与 bytes，同 id 同 sha256 重复登记不会产生重复条目。

## 命令表

全局参数：`--config PATH`（providers 配置）、`--cache PATH`、`--json`、`--apply`、`--quiet`、`-v/--verbose`。

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `status` | 输出全部适配器 probe 结果 + 缓存统计 + manifest 默认路径 | `--manifest`（可选） |
| `plan` | 只读计划：每个 asset 的缓存命中/需取件/来源顺序 | `--manifest` |
| `discover` | 调适配器发现候选资产（dry-run 默认） | `--provider`、`--query KEY=VAL`（可重复） |
| `register` | 把本地文件登记进 manifest（算 sha256/bytes，去重） | `--manifest`、`--file`、`--id`、`--kind`、`--version`、`--project`、`--name` |
| `verify` | 逐个校验（缓存或指定目录） | `--manifest`、`--asset`、`--dir` |
| `fetch` | 拉取单个资产到缓存 | `--manifest`、`--asset` |
| `cache` | `list` / `verify` / `prune` | `--keep-days`、`--apply` |
| `supply` | 缓存→目标目录落位 + 记录 | `--manifest`、`--asset`、`--dest` |
| `manifest` | `show` / `validate` / `convert --to ark-artifacts\|venusahisiburner` / `merge` | 清单路径用 `--manifest` 指定 |

输出有两种形态：人类可读表格（默认）与 `--json`。`--json` 时 stdout 只有 JSON，错误走 stderr，管道可以直接喂给 `jq`。

任何写操作在没有 `--apply` 时都会打印 `DRY-RUN` 前缀，JSON 里对应 `"applied": false`。

退出码与 `ark-node` 完全一致：

| 码 | 名称 | 触发 |
|---|---|---|
| 0 | `OK` | 成功 |
| 1 | `FAIL` | 未归类失败 |
| 2 | `USAGE` | 参数错误、未知适配器、`supply` 在清单里查不到该 id |
| 3 | `NO_SOURCE` | 所有来源都不可用、HTTP 4xx、取不到该资产 |
| 4 | `FORBIDDEN` | 路径越界、命中安全守卫 |
| 5 | `NETWORK` | 超时、连接失败、5xx、适配器超时 |
| 6 | `NEEDS_ELEVATION` | 需要提权但不具备 |

调度器只依赖退出码就能分流：`3` 换来源，`5` 退避重试，`2`/`4` 人工介入，`6` 提权重跑。

## manifest 与缓存契约摘要

清单 schema 是 `ark.asset-manifest.v1`，顶层必填 `schema` 与 `assets`，每个资产必填 `id`、`kind`、`filename`、`sha256`。`sha256` 是 64 位小写十六进制，schema 用 `^[a-f0-9]{64}$` 校验。`additionalProperties: true`，所以旧清单里多出来的字段不会导致校验失败。

资产上最关键的四个字段：

- `sources[]`：候选来源列表，`type` ∈ `local|http|oss|node-storage|email|nas`，`priority` 越小越先，缺省 100。
- `install.cache_rel`：必须等于内容寻址相对路径 `sha256/<前2>/<次2>/<sha256>/<filename>`。
- `install.material_rel`：供应到目标目录时的相对路径，缺省用 `filename`。
- `status`：`verified|unverified|missing`，只描述「有没有校验过」，不描述「能不能取到」。

缓存根目录按三级解析：`--cache` 参数 → 环境变量 `ARK_ASSET_CACHE` → 默认 `~/.cache/ark-assets`。目录布局是：

```
<cache-root>/sha256/<sha[:2]>/<sha[2:4]>/<sha>/<filename>
<cache-root>/_incoming/     # 落位前的暂存，校验通过才 rename 进正式路径
<cache-root>/_quarantine/   # 校验失败的副本，带 <stamp>- 前缀
```

`verify` 有三个结果：`OK`、`MISSING`、`CORRUPT`。`CORRUPT` 在 `--apply` 下会被移进 `_quarantine`，不会被静默删除。

字段级细节见 [docs/contracts.md](docs/contracts.md) 与 [docs/cache.md](docs/cache.md)。

## 适配器协议摘要

| 类型 | 依赖 | 说明 |
|---|---|---|
| `local` | 无 | 本机路径，必须存在且可读；越界返回 `FORBIDDEN` |
| `http` | 无 | 只允许 `http/https`，超时取 `ASSET_HTTP_TIMEOUT`（默认 30s） |
| `oss` | `ARK_OSS_*` 四个变量，或 `ASSET_OSS_CLI` | 优先委托 CLI，未配置时用 `urllib` 走公开直链 |
| `node-storage` | `ARK_STORAGE_SSH_PASS` + `sshpass` | 走 `sshpass -e`，密码只在环境里 |
| `email` | `ASSET_EMAIL_CLI`（缺省 `qw-email-handle`） | 委托 CLI，本仓不读邮箱口令 |
| `nas` | `ASSET_NAS_CLI`（缺省 `fileball`） | 委托 CLI，本仓不读 NAS 口令 |

三条通用规则：

1. `probe()` 绝不抛异常。缺凭据、缺 CLI 一律返回 `available=False`、`reason="BLOCKED:ADAPTER_UNAVAILABLE"`，并在 `missing_env` 里列出缺的变量名。只列名字，不列值。
2. 委托型适配器（`email`、`nas`）只认两条命令：`<cli> list --json` 与 `<cli> get <id|path> --out <DIR>`，超时统一由 `ASSET_ADAPTER_TIMEOUT` 控制（默认 120s）。
3. 所有输出与异常都过 `util.redact()`，已知凭据变量的值替换为 `***`。

协议细节与 JSON 样例见 [docs/adapters.md](docs/adapters.md)。

## 安全与脱敏

- **仓库里只有变量名**。`config/env-keys.json` 列的是 `GITHUB_TOKEN`、`ARK_OSS_ACCESS_KEY_SECRET`、`ARK_STORAGE_SSH_PASS` 这样的名字，值一律写 `<env>`。
- **脱敏是唯一出口**。`util.redact()` 从 `os.environ` 取已知凭据变量的值，长度 ≥ 6 的才替换成 `***`，空值与短值跳过。日志、manifest、供应记录、异常串都走它。
- **提交前门禁**。`bash tools/secret-scan.sh --gate` 扫描五类凭据：GitHub token、AWS/阿里云 key、私钥头、赋值型密码、禁用文件名。命中即 FAIL，退出 1。
- **自检**。`python3 tools/secret_scan.py --self-test` 用内存样本验证五条规则都还能命中，规则被改坏会立刻发现。
- **示例地址不等于真值**。文档里的 `10.11.63.24` 之类只作为示例出现，需按环境覆盖。

细节与凭据清单见 [docs/security.md](docs/security.md)。

## 回滚

本仓写入的东西只有四类：缓存目录、manifest 文件、供应落位文件、供应记录。每一类都能单独撤销，且全部先 dry-run：

```bash
# 1) 看缓存能清多少，再决定
bash bin/asset-tools cache prune --keep-days 30
bash bin/asset-tools cache prune --keep-days 30 --apply

# 2) 回退 manifest：用你自己的备份覆盖
cp ./assets.json.bak ./assets.json

# 3) 临时关闭某个来源：不设它需要的变量即可，probe 会报 BLOCKED
unset ARK_OSS_ACCESS_KEY_SECRET
```

供应记录是普通 JSON 文件，位于 `<dest>/.ark-supply/<asset_id>.json`，删除它不影响已落位的文件。完整步骤与影响面见 [docs/rollback.md](docs/rollback.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 分层、模块职责、供应数据流、失败模型 |
| [docs/contracts.md](docs/contracts.md) | 两份 schema 的字段表、三种旧 schema 的映射、merge 与幂等规则 |
| [docs/adapters.md](docs/adapters.md) | 六个适配器、email/nas 委托协议、`sshpass -e` 与 `base_dir` 边界 |
| [docs/cache.md](docs/cache.md) | 内容寻址布局、`_incoming`/`_quarantine`、校验状态、prune |
| [docs/security.md](docs/security.md) | 凭据清单（仅变量名）、`util.redact()`、`secret_scan.py` 五条规则与门禁 |
| [docs/rollback.md](docs/rollback.md) | 清缓存、回退 manifest、关闭适配器、删供应记录 |
| [docs/runbook.md](docs/runbook.md) | 日常巡检、取件失败排查、凭据缺失排查 |
| [docs/assets-not-in-git.md](docs/assets-not-in-git.md) | 不进 Git 的资产清单（pudCorntrol RAR 的路径/大小/SHA-256、厂商二进制与手册规则、NOAH-58 冻结仓库） |
| [MIGRATION.md](MIGRATION.md) | 从 `artifact-manifest.json` 与 `venusahisiburner` 清单迁移 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

## 许可

MIT，见 [LICENSE](LICENSE)。

---

版本 `v0.1.0`，发布于 2026-11-10。
