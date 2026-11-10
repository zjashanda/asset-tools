# 架构

## 问题

资产供应以前散在各处：固件在邮件附件里、工具在 OSS 上、测试材料在 NAS 上、镜像在 1006 存储节点上。每接一个项目就多一套「下载脚本 + 目录约定 + 校验方式」，同一个文件在不同机器上有三个名字、两种哈希口径、零个统一的落盘位置。

结果是三类反复出现的问题：

- **取错版本**。同一个文件名在不同来源上是不同内容，没人说得清现在机台上那份是哪一版。
- **校验走过场**。脚本打印「已下载」，但没人真的重算哈希，截断的固件一路走到烧录。
- **来源写死**。换一台机器就要改脚本，因为路径、主机名、凭据全都嵌在代码里。

`asset-tools` 把这件事收成一条链：**发现 → 清单 → 缓存 → 供应 → 记录**。链上每一段只依赖上一段留下的可复核证据。

## 分层

```
                     ┌──────────────────────────────────────┐
                     │           asset_tools.cli            │
                     │ status plan discover register verify │
                     │ fetch cache supply manifest          │
                     └───────────────┬──────────────────────┘
                                     │
        ┌────────────┬───────────────┼──────────────┬────────────────┐
        │            │               │              │                │
    status.py    manifest.py      cache.py      supply.py       adapters/
   (probe 汇总)  (模型/转换)    (内容寻址)    (编排 + 记录)    (六个来源)
        │            │               │              │                │
        └────────────┴───────────────┴──────────────┴────────────────┘
                                     │
                          util.py（时间 / 退出码 / sha256 / 脱敏）
```

`cli.py` 只做参数解析与分发，不含业务判断。业务判断分散在 `supply.py`（编排）与 `adapters/`（来源细节），两侧通过 `AssetRef` / `FetchResult` 两个数据类通信。

## 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `util.py` | 时间契约、退出码、`sha256_file` / `sha256_bytes`、`human_bytes`、`redact` | 不含业务分支 |
| `manifest.py` | `Asset` / `AssetManifest` 模型、`load_any` 三 schema 互转、`merge`、`validate`、两种导出 | 不读写缓存、不碰网络 |
| `cache.py` | 内容寻址布局、`put` / `has` / `verify` / `prune` / `list_entries`、路径边界 | 不决定从哪取 |
| `supply.py` | 缓存命中判断、按 priority 逐来源取件、落位、写供应记录 | 不实现具体协议 |
| `status.py` | 汇总 `probe_all()` 与缓存统计 | 不修改任何状态 |
| `cli.py` | 参数解析、`--json` 与表格输出、退出码映射 | 不含业务逻辑 |
| `adapters/base.py` | `AdapterStatus` / `AssetRef` / `FetchResult` / `Adapter` | 不含具体来源 |
| `adapters/*.py` | 各自的 `probe` / `discover` / `fetch` | 不跨来源兜底，不重试别家的失败 |

`manifest.py` 是唯一知道「三种 schema 长什么样」的地方。别的模块只面对 `AssetManifest`。

## 供应数据流

```
manifest（assets[]）
   │
   ├─ asset 不存在 ──► 退出 2（USAGE）
   │
   ▼
cache.has(asset) ── 命中且校验通过 ──┐
   │ 未命中                          │
   ▼                                 │
sources 按 priority 升序             │
   ├─ probe 不可用 ── 跳过，记原因    │
   ├─ fetch 成功 ──► cache.put ──────┤
   ├─ SHA256_MISMATCH ── 换下一个来源 ┤
   └─ 全部失败 ──► 退出 3 / 4 / 5     │
                                     ▼
                        复制到 dest/<material_rel 或 filename>
                                     │
                                     ▼
                        重算 dest sha256 并与期望比对
                                     │
                        ┌────────────┴────────────┐
                        ▼                         ▼
                   一致：写记录              不一致：失败并保留现场
                   <dest>/.ark-supply/<id>.json
```

三条不变量：

1. 缓存是唯一的下载落点。目标目录只从缓存复制，永远不直连来源。
2. 每次落位后都重算摘要，`dest_sha256_verified` 记录这个事实，不是假设。
3. 供应记录只在 `--apply` 时写。dry-run 不产生任何文件，包括记录。

## dry-run 与 apply

| 阶段 | dry-run（默认） | `--apply` |
|---|---|---|
| `fetch` | 打印将要从哪个来源取、落到哪个缓存路径 | 真的下载、校验、rename |
| `cache put` | 返回计划 | 写 `_incoming` → 校验 → rename |
| `cache prune` | 列候选与可回收字节 | 真删 |
| `supply` | 打印落位计划与记录路径 | 复制、校验、写记录 |
| `register` | 打印将写入的条目与是否重复 | 改 manifest 文件 |
| `manifest convert` | 打印目标 schema 与条目数 | 输出文件 |

dry-run 与 apply 走同一条代码路径，差别只在最后那一下写操作。这样计划与执行不会漂移：`plan` 说会取 3 个，`--apply` 就一定取 3 个。

JSON 输出里对应 `"applied": false/true`，脚本据此判断，不用去 parse 前缀字符串。

## 失败模型

失败分三类，对应三套处置：

| 类别 | 退出码 | 谁的问题 | 处置 |
|---|---|---|---|
| 用法问题 | `2` | 调用方 | 改参数，不重试 |
| 来源问题 | `3`、`5` | 上游或网络 | `3` 换来源，`5` 退避重试 |
| 边界问题 | `4` | 配置或安全守卫 | 人工介入，不重试 |

`6`（`NEEDS_ELEVATION`）在本仓很少出现，保留它是为了与 `ark-node` 的退出码表逐项一致：同一套调度器能把两个仓的输出一视同仁地分流。

失败信息里必须能看出三件事：试了哪个来源、卡在哪一步、下一步该干什么。`status` 与 `plan` 是排查入口，`--json` 输出给脚本。

## 为什么是这些设计

**为什么内容寻址。** 文件名会骗人，摘要不会。路径直接由 sha256 算出来，意味着「同一个路径 = 同一份字节」，去重是免费的，校验是顺带完成的。

**为什么 manifest 不存来源凭据。** 凭据会轮换、会按机器不同，写进清单就变成了要同步的状态。清单只描述「去哪找」，不描述「怎么证明身份」。

**为什么三种 schema 都认，但只产一种。** 输入侧要兼容历史（ark-mesh 与 venusahisiburner 的清单还在用），输出侧必须收敛，否则每个下游都在猜格式。`load_any` 负责收，`to_ark_artifacts` / `to_venusahisiburner` 负责按需放。

**为什么适配器必须有 `probe`。** 没有 `probe` 的适配器只能靠 `fetch` 试错，一次批量供应会打出几十条无意义的连接失败。`probe` 把「能不能用」和「有没有」分开，`status` 因此可以在一秒内回答「这台机器现在能连哪几个来源」。

**为什么默认 dry-run。** 写盘是不可逆的，读是。把不可逆动作放在显式开关后面，是这套工具唯一能保证「跑错一次不会毁掉环境」的办法。

## 平台差异

| 关注点 | Linux | Windows |
|---|---|---|
| 入口 | `bin/asset-tools`（bash，设 `PYTHONPATH` 后 `exec python3 -m asset_tools`） | `bin/asset-tools.ps1`（设 `$env:PYTHONPATH`，`python -m asset_tools`，透传 `$args` 与退出码） |
| 缓存默认 | `~/.cache/ark-assets` | `%USERPROFILE%\.cache\ark-assets` |
| 路径分隔符 | `/` | 清单与缓存相对路径一律 `/`，落盘时由 `Path` 转换 |
| `node-storage` | 需要 `sshpass` 与 `scp` | 需要 `sshpass` 与 `scp`（Git for Windows 或 WSL 提供），缺失即 `BLOCKED` |
| 文件原子替换 | `rename` 同文件系统原子 | `os.replace`，同一卷上原子 |

本仓不假设 Windows 上一定有 `sshpass`。没有就 `BLOCKED`，让 `status` 明说，而不是退化成手工拷文件。

## 目录与文件

代码目录结构、schema 文件、`config/` 与 `tests/` 的关系见 README 的目录结构一节。运行时产物只有四处：

| 产物 | 位置 | 谁写 |
|---|---|---|
| manifest | 用户指定（`--manifest`） | `register` |
| 缓存 | `<cache-root>/sha256/...`、`_incoming`、`_quarantine` | `cache put` / `fetch` |
| 落位文件 | `<dest>/<material_rel 或 filename>` | `supply` |
| 供应记录 | `<record_dir 或 dest>/.ark-supply/<asset_id>.json` | `supply --apply` |

四处都可独立清理，互不依赖。回滚步骤见 [rollback.md](rollback.md)。
