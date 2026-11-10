# 回滚

本仓写入的东西只有四类：缓存、manifest、落位文件、供应记录。这四类互不依赖，可以单独撤销，也可以一起清掉。原则只有一条：**先 dry-run，再动手。**

## 0. 总原则

| 原则 | 说明 |
|---|---|
| 只动自己写的 | 不碰 `ark-archive` 的结果区、receipt、存储节点上的原始产物 |
| 先看再删 | 所有清理先用 dry-run 或 `ls` 确认清单，再执行 |
| 不删唯一数据 | 缓存和记录都能重建；落位文件可能被下游用过，删之前先确认 |
| 记录路径 | 改动前把要动的路径写进 issue，回滚时照着核对 |

回滚前后各跑一次 `status`，对比输出比任何说明都直观：

```bash
bash bin/asset-tools status --json > /tmp/ark-assets-before.json
# ... 回滚操作 ...
bash bin/asset-tools status --json > /tmp/ark-assets-after.json
```

`status` 只读，跑多少次都不改变状态。

## 1. 清缓存

缓存不含唯一数据，随时可清。先看清单：

```bash
# 默认 dry-run，只列候选与可回收空间
bash bin/asset-tools cache prune --keep-days 30

# 只想清更老的，把天数调大
bash bin/asset-tools cache prune --keep-days 90

# 结构化的清单，方便进脚本或粘进 issue
bash bin/asset-tools cache prune --keep-days 30 --json
```

确认后执行：

```bash
bash bin/asset-tools cache prune --keep-days 30 --apply
```

注意 dry-run 输出里 `_quarantine` 与 `_incoming` 是**单独计数**的。清之前先看一眼隔离区的体积：它通常是故障证据，不是垃圾。

要整体清空（比如换机器、换磁盘），直接删根目录也行，代价是一次全量重新下载：

```bash
# 先确认路径是缓存根，不是别的东西
bash bin/asset-tools status | grep -i cache
rm -rf ~/.cache/ark-assets          # 默认根；用了 --cache 的话换成实际路径
```

整目录删除没有 dry-run，所以务必先确认路径。用 `--cache` 指到过别处的机器，尤其要核对。

## 2. 回退 manifest

manifest 是普通 JSON 文件，回退就是换回备份。

```bash
# 改之前先备份，改完出问题时才有东西可退
cp ./assets.json ./assets.json.bak.20261110-193000

# 回退
cp ./assets.json.bak.20261110-193000 ./assets.json

# 回退后立刻校验，别让坏清单继续往下游走
bash bin/asset-tools manifest validate --manifest ./assets.json
```

如果备份也没了，用 `register` 重建关键条目，或者从旧 schema 重新转换（见 [MIGRATION.md](../MIGRATION.md)）。重建成什么样，用 `plan` 的输出核对：

```bash
bash bin/asset-tools plan --manifest ./assets.json
```

`plan` 会逐个资产列出缓存命中状态与来源顺序。条数、id、优先级对得上，说明清单基本正确。

## 3. 临时关闭某个来源

不需要改配置，也不需要改代码。**不设它需要的变量**就是关闭。

```bash
# 关掉 oss
unset ARK_OSS_ACCESS_KEY_SECRET

# 关掉 node-storage
unset ARK_STORAGE_SSH_PASS

# 关掉 email（让适配器找不到 CLI）
unset ASSET_EMAIL_CLI
```

效果：

| 位置 | 表现 |
|---|---|
| `status` | 该适配器 `available=false`，`reason="BLOCKED:ADAPTER_UNAVAILABLE"`，`missing_env` 列出缺的变量名 |
| `plan` | 该来源被跳过，其他来源照常参与排序 |
| `discover` | 该适配器返回空列表，不报错、不崩溃 |
| `supply` | 如果只剩这一个来源，最终退出 `3`（`NO_SOURCE`） |

恢复就是重新导出变量，或者在新的 shell 里重新登录。**不需要重启任何东西**：适配器每次执行都重新读环境。

这条路径也是排查手段：把怀疑的来源关掉再跑 `plan`，如果问题消失，锅就在那个来源上。

只关一会的话，建议在会话里 `export` 而不是写进 shell 配置，避免忘记改回来：

```bash
export ARK_STORAGE_SSH_PASS="$(cat /run/secrets/ark-storage-pass)"   # 从受控来源取，不落盘
```

上面这种写法要求口令本身存在受控文件里。不要把口令写进 `.bashrc`、`.profile` 或任何会被提交的文件。

## 4. 删除供应记录

供应记录在 `<dest>/.ark-supply/<asset_id>.json`（显式指定 `record_dir` 时在那边）。删它不影响已经落位的文件。

```bash
# 先看有哪些
ls -l ./inbox/.ark-supply/

# 只删某一条
rm -f ./inbox/.ark-supply/pvm1005-firmware.json

# 整个记录目录删掉（假设你不打算再追溯这批供应）
rm -rf ./inbox/.ark-supply
```

删记录等于放弃「这个文件是我放的」这条证据。如果只是记录内容过时，更好的做法是重新 `supply --apply` 覆盖它，路径固定，天然幂等：

```bash
bash bin/asset-tools supply --manifest ./assets.json --asset pvm1005-firmware --dest ./inbox --apply
```

## 5. 撤销 `manifest convert` 的产物

`convert` 输出的是新文件，不动输入。撤销就是删输出：

```bash
rm -f ./artifact-manifest.json          # --to ark-artifacts 的产物
rm -f ./assets.venusahisiburner.json    # --to venusahisiburner 的产物
```

输入清单原地未改，所以删掉输出不会造成任何信息丢失。

## 6. 回滚自检

```bash
bash bin/asset-tools status                 # 来源可见性与缓存统计符合预期
bash bin/asset-tools plan --manifest ./assets.json       # 每个资产都有可行来源
bash bin/asset-tools verify --manifest ./assets.json     # 缓存/落位摘要仍然一致
```

三步的顺序是有意的：先看环境（来源在不在），再看计划（清单对不对），最后验内容（字节对不对）。前面不对，后面查了也没意义。

## 7. 回滚时明确不做的事

- **不删存储节点上的产物**。`/srv/storage/ark/artifacts` 下的文件是上游资产，本仓只是读它。
- **不删 `ark-archive` 的结果与回执**。那是另一个仓的交付物，归属清楚，别越界。
- **不回滚 `_quarantine`**。隔离区里的副本已经确认和期望摘要不符，恢复它只会把问题带回来。要留证据就留着，要清理就按第 1 节走 `prune`。
- **不动 `~/.config/ark/mesh-sources.json`**。那是节点本地配置，本仓不读也不写。
