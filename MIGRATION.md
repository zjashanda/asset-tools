# 迁移

> 本仓库只包含脚本、schema、测试与文档，不含任何凭据；迁移过程只读旧清单、只写新清单，不搬运任何密钥材料。

本文件说明怎么把两份旧清单迁到 `ark.asset-manifest.v1`：ark-mesh 产出的 `artifact-manifest.json`，以及 venusahisiburner 的 `assets/manifest.json`。

迁移是**加法**：旧文件原地不动，新文件另存一份。所以回滚成本几乎为零，见最后一节。

## 0. 迁移会做什么

| 动作 | 说明 |
|---|---|
| 读旧清单 | `load_any` 看 `schema` 字段自动识别，不需要手工指定类型 |
| 转成新模型 | 旧字段映射到 `assets[]`，见 [docs/contracts.md](docs/contracts.md) 第 3 节 |
| 写新清单 | 一份 `ark.asset-manifest.v1`，路径由你定 |
| 校验 | `manifest validate` 检查结构与枚举 |
| 回读 | `plan` / `verify` 确认每条资产都有可行来源 |

不做的：不改旧文件、不删旧文件、不迁移缓存、不删除存储节点上的产物。

## 1. 准备

```bash
cd asset-tools

# 记下旧清单的摘要，迁移后用它证明「输入没被动过」
sha256sum /path/to/artifact-manifest.json
sha256sum /path/to/assets/manifest.json

# 顺手备份一份，放在工作目录而不是仓库里
cp /path/to/artifact-manifest.json ./artifact-manifest.json.orig
cp /path/to/assets/manifest.json   ./venusahisiburner-manifest.json.orig
```

备份文件不要提交。仓库是公开的，备份里可能带内网路径。

## 2. 先确认旧清单能被读进来

`load_any` 认三种 `schema`：

| 旧清单 | `schema` 字段 |
|---|---|
| ark-mesh `artifact-manifest.json` | `ark-artifact-manifest.v1` |
| venusahisiburner `assets/manifest.json` | `venusahisiburner-assets/v1` |
| 本仓 | `ark.asset-manifest.v1` |

用只读的 `plan` 试一次，能出计划就说明识别成功：

```bash
bash bin/asset-tools plan --manifest ./artifact-manifest.json.orig
bash bin/asset-tools plan --manifest ./venusahisiburner-manifest.json.orig
```

`plan` 不改任何东西。输出里会逐个列出转换后的 id、缓存命中状态与来源顺序。这一步就能看出映射对不对：id 是不是你预期的、来源数量对不对、`filename` 有没有落到目录名上。

## 3. 生成新清单

`manifest show` 读入后输出的是当前模型的 JSON，配 `--json` 重定向就得到 `ark.asset-manifest.v1`：

```bash
# ark-mesh 产物清单
bash bin/asset-tools manifest show --manifest ./artifact-manifest.json.orig --json > ./assets.from-mesh.json

# venusahisiburner 清单
bash bin/asset-tools manifest show --manifest ./venusahisiburner-manifest.json.orig --json > ./assets.from-venus.json
```

检查生成结果：

```bash
head -c 400 ./assets.from-mesh.json
```

第一行应该能看到 `"schema": "ark.asset-manifest.v1"`。看不到就说明输入没被识别成旧格式，回第 2 步核对 `schema` 字段的拼写。

如果两份旧清单描述的是同一批资产（很常见：固件在 mesh 清单里，工具在 venus 清单里），合并后的清单以 id 对齐，规则是：同 id 同 sha256 合并来源，同 id 不同 sha256 保留 `provenance.discovered_at` 较晚的那条，被替换方写进 `provenance.note`（前缀 `superseded:`）。

## 4. 校验

```bash
bash bin/asset-tools manifest validate --manifest ./assets.from-mesh.json
bash bin/asset-tools manifest validate --manifest ./assets.from-venus.json
```

`validate` 只报结构问题：`schema` 不对、缺必填字段、`sha256` 不是 64 位小写十六进制、`kind` / `status` / `sources[].type` 不在枚举内。它不报语义问题（比如 id 重复、来源不可达），那些靠下一步。

结构错误必须清零才能继续。常见两类：

| 报错 | 原因 | 处理 |
|---|---|---|
| `sha256` 格式不符 | 旧清单里写了短摘要、大写或占位符 | 用真实文件重算：`register --file` 会算 |
| 缺 `filename` | 旧条目的路径字段为空 | 手工补，或重新登记 |

## 5. 回读确认

```bash
bash bin/asset-tools plan --manifest ./assets.from-mesh.json
bash bin/asset-tools verify --manifest ./assets.from-mesh.json
```

`plan` 输出三件事：每个资产的缓存命中、需要取件的来源顺序、无来源的条目。无来源的条目要单独处理：旧清单只记了一个节点路径，那台节点下线之后就真的没有来源了。

`verify` 在没有缓存的新机器上会全是 `MISSING`，这是正常的。它的作用是确认每条的期望摘要都能算出来。

## 6. 反向导出做回归核对

迁移的验证不止「能读」，还要「能还原」。把新清单导回旧格式，与原始文件对比可以覆盖字段级差异：

```bash
# 新清单 → ark-mesh 认识的格式
bash bin/asset-tools manifest convert --to ark-artifacts --manifest ./assets.from-mesh.json

# 新清单 → venusahisiburner 认识的格式
bash bin/asset-tools manifest convert --to venusahisiburner --manifest ./assets.from-venus.json
```

先 dry-run 看目标 schema 与条目数，确认条目数一致后再加 `--apply` 落盘。

对比时只看两边都有的字段：`id`、`sha256`、`bytes`、`version`。新仓多出来的字段（`sources[].priority`、`provenance.discovered_at`、`status`）在旧格式里没有槽位，不必强求。

## 7. 字段级差异

详细映射表在 [docs/contracts.md](docs/contracts.md) 第 3 节。迁移时最需要留意的四处：

| 项 | 说明 |
|---|---|
| `install.cache_rel` | 旧值不满足内容寻址规则，转换时按 `sha256` 重算成 `sha256/<前2>/<次2>/<sha256>/<filename>` |
| `kind` | ark-mesh 清单没有这个概念，统一标 `other`；venus 清单的 `tool` / `firmware` 原样保留 |
| `target_chip` | 新 schema 没有这个槽位，不丢但也不是契约字段，下游不要依赖 |
| `status` | 转换结果标 `unverified`，跑过 `verify` 之后再改成 `verified` |

## 8. 迁移时要守住的两条

**`~/.config/ark/mesh-sources.json` 永远不提交。** 它是节点本地的来源配置，含内网拓扑与主机名。本仓不读它，迁移也不需要它。迁移完成后如果某台机器还靠它取件，那是旧流程的事，不要把它抄进新仓库当作示例。

**旧清单里的路径不是凭据，但也不是公开信息。** 迁移产出的新清单如果要提交到仓库，只放 schema 与示例，真实清单放节点本地或受控存储。`config/providers.example.json` 是给示例用的，路径与端点都标了「示例 / 需按环境覆盖」。

## 9. 回滚

迁移没有破坏任何东西，所以回滚就是删掉新文件：

```bash
rm -f ./assets.from-mesh.json ./assets.from-venus.json
```

旧清单原地未改，摘要和迁移前一致（用第 1 步记下的值核对）：

```bash
sha256sum /path/to/artifact-manifest.json
```

如果已经用新清单跑过 `supply`，落位文件与供应记录是独立产物，删不删由你决定：删记录只是放弃「谁放的」这条证据，删落位文件要确认下游没用过。清缓存见 [docs/rollback.md](docs/rollback.md)。

迁移过程中如果已经把新清单接入了下游，回滚时把下游指回旧文件即可。两套清单可以并存，本仓只负责读，不独占任何路径。
