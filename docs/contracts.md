# 契约

本仓库对外承诺四件事：两份 JSON 结构、一条时间口径、一套退出码。字段一旦发布，只能加不能改语义。加字段靠 `additionalProperties: true` 落地，不改语义靠这条规矩守住。

## 0. 时间与退出码

全仓统一北京时间（UTC+8）：

| 用途 | 格式 | 示例 |
|---|---|---|
| 人读 / manifest / 供应记录 | `YYYY-MM-DD HH:MM:SS`（`FMT_DISPLAY`，无时区后缀） | `2026-11-10 19:30:00` |
| 机器可读（需要带时区时） | `YYYY-MM-DDTHH:MM:SS+0800`（`FMT_RFC3339`） | `2026-11-10T19:30:00+0800` |
| 文件名 / 隔离区前缀 | `YYYYMMDD-HHMMSS`（`FMT_STAMP`） | `20261110-193000` |

时间只有一个来源：`util.now_beijing()`，实现是 `datetime.now(timezone.utc).astimezone(BEIJING)`。不读节点本地时区，因为节点跨时区和跨 `TZ` 设置时，本地时间无法排序与对齐。解析用 `util.parse_time()`，两种格式都接受，返回带时区的 `datetime`。

| 码 | 名称 | 触发 |
|---|---|---|
| 0 | `OK` | 成功 |
| 1 | `FAIL` | 未归类失败，含 `SHA256_MISMATCH` |
| 2 | `USAGE` | 参数错误、未知适配器、`supply` 在清单里查不到该 id |
| 3 | `NO_SOURCE` | 所有来源都不可用、HTTP 4xx、取不到该资产 |
| 4 | `FORBIDDEN` | 路径越界、命中安全守卫 |
| 5 | `NETWORK` | 超时、连接失败、5xx、适配器超时 |
| 6 | `NEEDS_ELEVATION` | 需要提权但不具备 |

`EXIT_CODES` 是这张表的机器可读版本，字符串与数值的对应关系不允许漂移。

## 1. `ark.asset-manifest.v1`

资产清单。`schemas/asset-manifest.v1.json` 是它的 JSON Schema（draft-07）。

```json
{
  "schema": "ark.asset-manifest.v1",
  "generated_at": "2026-11-10 19:30:00",
  "assets": [
    {
      "id": "pvm1005-firmware",
      "kind": "firmware",
      "name": "PVM1005 固件",
      "version": "1.2.3",
      "filename": "pvm1005-v1.2.3.zip",
      "bytes": 1048576,
      "sha256": "…64 位小写十六进制…",
      "project": "tcl-pvm1005",
      "sources": [
        {"type": "local", "path": "/data/fw/pvm1005-v1.2.3.zip", "priority": 5},
        {"type": "node-storage", "node": "storage1006", "path": "/srv/storage/ark/artifacts/tcl/pvm1005-v1.2.3.zip", "priority": 10},
        {"type": "oss", "key": "firmware/pvm1005-v1.2.3.zip", "priority": 20}
      ],
      "install": {"cache_rel": "sha256/ab/cd/<sha256>/pvm1005-v1.2.3.zip", "material_rel": "assets/pvm1005-v1.2.3.zip"},
      "provenance": {"origin": "node-storage:storage1006", "note": "首个可用来源", "discovered_at": "2026-11-10 19:00:00"},
      "status": "verified"
    }
  ]
}
```

### 1.1 顶层字段

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `schema` | string | 是 | 恒为 `ark.asset-manifest.v1` |
| `generated_at` | string | 否 | 生成本文件的时间，`FMT_DISPLAY` |
| `assets` | array | 是 | 资产数组，顺序不影响语义 |

### 1.2 资产字段

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `id` | string | 是 | 资产稳定标识，同清单内唯一；跨来源合并时以它为准 |
| `kind` | string | 是 | `firmware` / `tool` / `test_material` / `dataset` / `other` |
| `name` | string | 否 | 展示名，给人看的 |
| `version` | string | 否 | 版本串，允许非语义化（如 `unversioned-20261027`） |
| `filename` | string | 是 | 落盘文件名，缓存路径的最后一段 |
| `bytes` | integer | 否 | 字节数；校验以 `sha256` 为准，`bytes` 只作提示 |
| `sha256` | string | 是 | 64 位小写十六进制，schema 用 `^[a-f0-9]{64}$` 校验 |
| `project` | string | 否 | 所属项目，便于按项目过滤与归档 |
| `sources` | array | 否 | 候选来源，见 1.3；为空表示「知道有这个东西，但还不知道去哪取」 |
| `install` | object | 否 | 落位约定，见 1.4 |
| `provenance` | object | 否 | 溯源，见 1.5 |
| `status` | string | 否 | `verified` / `unverified` / `missing`，缺省含义等同于 `unverified` |

`status` 只回答「这条记录校验过没有」，不回答「现在能不能取到」。能不能取到要看 `sources` 和实时的 `probe` 结果，两者不要混为一谈。

### 1.3 `sources[]` 字段

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `type` | string | 是 | `local` / `http` / `oss` / `node-storage` / `email` / `nas` |
| `priority` | integer | 否 | 越小越先尝试，缺省 100 |
| `path` | string | 依类型 | `local` 的本机路径、`node-storage` 的远端路径（相对 `base_dir` 或绝对路径） |
| `url` | string | 依类型 | `http` 的直链，只允许 `http/https` |
| `key` | string | 依类型 | `oss` 的对象 key |
| `node` | string | 依类型 | `node-storage` 的节点名，例如 `storage1006` |

不同 `type` 用不同字段承载定位信息，多余字段忽略。取件失败的顺序与原因会体现在 `plan` 与 `--json` 输出里。

### 1.4 `install` 字段

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `cache_rel` | string | 否 | 缓存内相对路径，**必须**等于 `sha256/<前2>/<次2>/<sha256>/<filename>` |
| `material_rel` | string | 否 | 供应到目标目录时的相对路径；缺省用 `filename` |

`cache_rel` 是硬约束。内容寻址的意义在于「路径即校验和」，写成别的形状就等于放弃一致性。转换旧清单时如果旧 `cache_rel` 不满足这条规则，按 `sha256` 重算后写入。

### 1.5 `provenance` 字段

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `origin` | string | 否 | 首次发现来源，形如 `type:定位`，例如 `node-storage:storage1006` |
| `note` | string | 否 | 自由文本。`merge` 覆盖旧条目时以 `superseded:` 为前缀描述被替换方 |
| `discovered_at` | string | 否 | 首次发现时间，`FMT_DISPLAY`，`merge(prefer="newest")` 用它比较新旧 |

### 1.6 校验规则

`AssetManifest.validate()` 返回错误列表（不抛异常），`manifest validate` 把它打印出来：

1. `schema` 必须等于 `ark.asset-manifest.v1`。
2. `assets` 必须是数组（可以为空）。
3. 每条资产必须有 `id`、`kind`、`filename`、`sha256`。
4. `sha256` 必须匹配 `^[a-f0-9]{64}$`。
5. `kind`、`status`、`sources[].type` 必须是枚举内的值。
6. `additionalProperties: true`：未知字段保留、不报错，向前兼容靠这条。

## 2. `ark.supply-record.v1`

每次 `supply --apply` 在 `<record_dir 或 dest_dir/.ark-supply>/<asset_id>.json` 写一条记录。它证明「这个文件是我放到这里的，放完我又算了一遍哈希」。

```json
{
  "schema": "ark.supply-record.v1",
  "asset_id": "pvm1005-firmware",
  "version": "1.2.3",
  "filename": "pvm1005-v1.2.3.zip",
  "bytes": 1048576,
  "sha256": "…",
  "source_type": "node-storage",
  "source_ref": "storage1006:/srv/storage/ark/artifacts/tcl/pvm1005-v1.2.3.zip",
  "dest_path": "/data/inbox/pvm1005-v1.2.3.zip",
  "dest_sha256": "…",
  "dest_sha256_verified": true,
  "supplied_at": "2026-11-10 19:30:00",
  "node": "bszt14",
  "archive_owner": "ark-archive",
  "is_receipt": false
}
```

本仓写出的记录恒含上表全部字段。

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `schema` | string | 是 | 恒为 `ark.supply-record.v1` |
| `asset_id` | string | 是 | 对应 manifest 里的 `id` |
| `version` | string | 是 | 资产版本，空值写空串而不是省略 |
| `filename` | string | 是 | 落盘文件名 |
| `bytes` | integer | 是 | 实际字节数 |
| `sha256` | string | 是 | 期望摘要，来自 manifest |
| `source_type` | string | 是 | 实际生效的来源类型 |
| `source_ref` | string | 是 | 实际生效的定位，形如 `storage1006:/srv/...` 或 `oss:firmware/x.zip` |
| `dest_path` | string | 是 | 落位后的绝对路径 |
| `dest_sha256` | string | 是 | 落位后重算的摘要 |
| `dest_sha256_verified` | boolean | 是 | 落位摘要与 `sha256` 是否一致 |
| `supplied_at` | string | 是 | 供应时间，`FMT_DISPLAY` |
| `node` | string | 是 | 执行供应的机器名 |
| `archive_owner` | string | 是 | 恒为 `ark-archive` |
| `is_receipt` | boolean | 是 | 恒为 `false` |

`archive_owner` 与 `is_receipt` 是硬字段。它们的存在就是为了防止下游把供应记录当成归档 receipt：归档与回执归 `ark-archive`，本仓不产出、不冒充。

## 3. 旧 schema 映射

`load_any(path)` 读文件、看 `schema` 字段、自动转换。三种输入都走同一个出口 `AssetManifest`。

| 输入 `schema` | 处理 |
|---|---|
| `ark.asset-manifest.v1` | 原样读入 |
| `ark-artifact-manifest.v1` | 转换为 assets，`kind="other"` |
| `venusahisiburner-assets/v1` | 转换为 assets，`filename=file`，来源取 `oss.key` |

### 3.1 `ark-artifact-manifest.v1`（ark-mesh 产物清单）

旧结构：`artifacts:[{name, project, version, source_node, source_path, destination, bytes, sha256, ...}]`。

| 旧字段 | 新字段 | 规则 |
|---|---|---|
| `name` | `id` | 旧条目没有独立 id，转换时以 `name` 为基底派生，同清单内去重 |
| `name` / `destination` | `filename` | 取路径部分的文件名；`destination` 缺失时用 `name` |
| `project` | `project` | 原样 |
| `version` | `version` | 原样 |
| `bytes` | `bytes` | 原样 |
| `sha256` | `sha256` | 原样，转换后用 `validate` 复核格式 |
| `source_node` | `sources[0].node` | 与 `source_path` 一起组成 `{"type":"node-storage"}` 来源 |
| `source_path` | `sources[0].path` | 远端路径，取件时仍受 `base_dir` 边界约束 |
| `destination` | `install.material_rel` | 原样；`cache_rel` 按 `sha256` 重算 |
| 无 | `kind` | 固定 `"other"`：旧清单不区分固件与工具，不猜 |
| 无 | `status` | 转换结果标 `unverified`，除非后续 `verify` 通过 |

`id` 与 `filename` 的派生是转换约定，不是 schema 字段；转换后跑一次 `manifest validate` 并抽查 `plan` 输出即可确认这两列符合预期。

### 3.2 `venusahisiburner-assets/v1`

旧结构：`assets:[{id, kind, target_chip, version, file, bytes, sha256, oss{key,...}, install{material_rel,cache_rel}, provenance{source,note}}]`。

| 旧字段 | 新字段 | 规则 |
|---|---|---|
| `id` | `id` | 原样 |
| `kind` | `kind` | 原样；旧值 `tool` / `firmware` 都在新枚举内 |
| `file` | `filename` | 原样取用，规范里点名的映射 |
| `version` | `version` | 原样 |
| `bytes` | `bytes` | 原样 |
| `sha256` | `sha256` | 原样 |
| `oss.key` | `sources[0]` | `{"type":"oss","key":<oss.key>}`，按 `priority` 参与取件顺序 |
| `install.material_rel` | `install.material_rel` | 原样 |
| `install.cache_rel` | `install.cache_rel` | 旧值不满足内容寻址规则，按 `sha256` 与 `filename` 重算 |
| `provenance.source` | `provenance.origin` | 原样保留来源描述 |
| `provenance.note` | `provenance.note` | 原样 |
| `target_chip` | 无对应字段 | 新 schema 没有这个槽位；因为 `additionalProperties: true`，实现可以保留，但它不是契约字段，下游不要依赖 |
| 无 | `provenance.discovered_at` | 缺失就留空，不伪造时间 |

旧清单顶层的 `oss` 段（`endpoint_env`、`bucket_env`、`credential.*`）是配置说明，不是资产数据。转换时不复制到新清单：环境变量名由本仓自己的 `config/env-keys.json` 与适配器定义，重复一份只会漂移。

## 4. merge 与幂等

### 4.1 merge 规则

`AssetManifest.merge(other, prefer="newest")` 按 `id` 对齐：

| 情形 | 结果 |
|---|---|
| 只有一边有该 id | 直接取有的一边 |
| 两边同 id、`sha256` 相同 | 合并 `sources`：按 `(type, 定位)` 去重，同一定位保留 `priority` 较小者 |
| 两边同 id、`sha256` 不同 | 由 `prefer` 决定保留谁；`newest` 比较 `provenance.discovered_at`，晚者胜 |

被替换方的信息不丢，写进胜者的 `provenance.note`，前缀 `superseded:`，后接被替换方的 `sha256` 与来源。这样「为什么现在拿的是这一版」有据可查，不用去翻日志。

`prefer` 只影响同 id 不同内容的取舍，不影响 `sources` 合并。

### 4.2 幂等

| 操作 | 幂等表现 |
|---|---|
| `load_any` 转换 | 同一输入重复转换，输出逐字节相同 |
| `register` 同一文件 | 同 id 同 `sha256` 重复登记不新增条目，只更新可展示字段 |
| `merge(A, A)` | 等于 `A`；`sources` 去重后不变 |
| `cache.put` | 同一 `sha256` 与 `filename` 永远落在同一路径，重复 `put` 不产生副本 |
| `supply` 同一 asset 到同一 dest | 记录写在固定路径 `<asset_id>.json`，重复供应覆盖同一条记录 |
| `manifest convert` | 同一输入重复导出，输出稳定 |

幂等是这套流程能被脚本反复调用的前提：先 dry-run、再 `--apply`、失败重跑，不应该留下越来越多的中间态。

## 5. 与相邻契约的关系

- `ark-node-manifest.v1`（`ark-node` 的取件产物）：字段语义接近，但不互换。`ark-node` 描述「一次取件的尝试序列」，本仓描述「一份资产的来源集合与落位约定」。
- `ark-artifact-manifest.v1`：只作输入，不再产出。需要给老消费者时用 `manifest convert --to ark-artifacts` 现场生成。
- `venusahisiburner-assets/v1`：只作输入与导出目标，烧录侧继续用它，不会被本仓替换。

字段级说明到这里为止；写入路径与目录布局见 [cache.md](cache.md)，来源协议见 [adapters.md](adapters.md)。
