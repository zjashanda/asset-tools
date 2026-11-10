# 缓存

缓存是这套流程的中转站：取件先落到缓存，供应再从缓存复制出去。它按内容寻址，路径由 sha256 决定，所以同一份文件永远只有一个位置，不同版本永远不会互相覆盖。

## 1. 根目录

三级解析，前面的优先：

1. 命令行 `--cache PATH`
2. 环境变量 `ARK_ASSET_CACHE`
3. 默认 `~/.cache/ark-assets`

根目录不需要预先创建，第一次真正写盘时会建。`status` 会打印当前生效的根目录与它的占用。

## 2. 布局

```
<cache-root>/
├── sha256/
│   ├── ab/
│   │   └── cd/
│   │       └── abcdef…（完整 sha256）/
│   │           └── pvm1005-v1.2.3.zip
│   └── …
├── _incoming/                        # 落位前的暂存区
└── _quarantine/                      # 校验失败的副本
```

正式路径是：

```
<root>/sha256/<sha[:2]>/<sha[2:4]>/<sha>/<filename>
```

两层前缀目录（前 2 位、次 2 位）是为了避免单个目录里堆积几万个条目。现代文件系统对目录项数量有实际限制，256 × 256 的分桶足够用很久。

清单里的 `install.cache_rel` 必须和这个形状逐字一致，不带 `<root>` 前缀：

```
sha256/ab/cd/abcdef…/pvm1005-v1.2.3.zip
```

`_incoming` 与 `_quarantine` 以 `_` 开头，与 64 位十六进制的分桶目录天然区分，遍历时不会混淆。

## 3. `ContentCache` 接口

| 方法 | 返回 | 说明 |
|---|---|---|
| `path_for(asset)` | `Path` | 按 `sha256` 与 `filename` 算出正式路径，不做 IO |
| `has(asset)` | `bool` | 文件存在**且**重算 sha256 匹配 |
| `put(asset, src_path, apply=False)` | `PutResult` | dry-run 只返回计划；`apply=True` 才写 |
| `verify(asset)` | `str` | `OK` / `MISSING` / `CORRUPT` |
| `prune(keep_days=30, now=None, apply=False)` | `PrunePlan` | 按 mtime 列候选，`apply` 才删 |
| `list_entries()` | `list[CacheEntry]` | 遍历正式区，返回路径、大小、sha256 |

`has()` 会重算摘要，不是只 `exists()`。这一点开销换来的是「命中就是真的命中」，否则一个被截断的文件会被当成缓存命中，然后一路错到烧录台上。

## 4. 写入流程

`put(asset, src_path, apply=False)`：

```
dry-run（默认）
  └─ 算出目标路径 + 源大小 + 是否已存在 ──► PutResult，不动磁盘

apply=True
  1. 拷到 <root>/_incoming/<临时名>
  2. 在暂存文件上重算 sha256
  3. 匹配 ──► 原子 rename 到正式路径
     不匹配 ──► 删除暂存文件，返回失败（不进正式区，也不进隔离区）
```

为什么不直接写正式路径：写一半断电会留下一个「名字对、内容错」的文件，而内容寻址的承诺是「路径即校验和」。先暂存再 rename，正式区里出现的文件永远是完整的。

暂存文件用唯一名字，避免两个进程同时取同一个资产时互相踩。`rename` 在同一文件系统内是原子的，所以正式区不存在中间态。

## 5. 校验状态

```bash
bash bin/asset-tools verify --manifest ./assets.json
bash bin/asset-tools verify --manifest ./assets.json --asset pvm1005-firmware
bash bin/asset-tools verify --manifest ./assets.json --dir ./inbox
```

| 状态 | 含义 | 后续 |
|---|---|---|
| `OK` | 文件存在，重算 sha256 与期望一致 | 无 |
| `MISSING` | 正式路径上没有文件 | 用 `fetch` 取，或 `supply` 时自动取 |
| `CORRUPT` | 文件存在但摘要不符 | `--apply` 下移入 `_quarantine`，然后重新取件 |

`CORRUPT` 的处理刻意分成两步：先隔离，再重取。隔离区里的副本是证据，删掉就查不清是传输截断、磁盘损坏还是上游换了文件。

隔离区命名：

```
<root>/_quarantine/<YYYYMMDD-HHMMSS>-<filename>
```

时间戳在前，便于按时间排序；同名文件被隔离多次也不会互相覆盖。

`cache verify` 是同一套判定，作用范围是整份缓存而不是某份清单：

```bash
bash bin/asset-tools cache verify
```

## 6. 清理

```bash
# 默认 dry-run：只列候选与可回收空间
bash bin/asset-tools cache prune --keep-days 30

# 确认后删除
bash bin/asset-tools cache prune --keep-days 30 --apply
```

规则：

- 按 mtime 判断，超过 `--keep-days`（默认 30 天）的条目进候选。
- 输出总条数、总字节数（`util.human_bytes()` 渲染），`--json` 时给出结构化列表。
- `_quarantine` 与 `_incoming` 一并纳入候选，但**单独计数**，不混进正式区的数字里。原因很简单：这两个目录的体积不代表「有用缓存」，混在一起会让人误判回收收益。
- 没有 `--apply` 绝不删。删除是不可逆操作，默认必须安全。

```bash
# 看清单形态
bash bin/asset-tools cache list
```

`cache list` 输出每个条目的相对路径、大小与最后访问时间，`--json` 时可直接喂给脚本做容量统计。

## 7. 路径安全

所有写入路径都要过一遍边界检查：

| 输入 | 结果 |
|---|---|
| `sha256/ab/cd/<sha>/fw.img` | 允许 |
| `../env/env.json` | 拒绝 |
| `/etc/passwd` | 拒绝（绝对路径） |
| `<root>/../outside/x` | 拒绝（折叠后跳出根） |
| `sha256/ab/cd/<sha>/../../../../x` | 拒绝 |

判定顺序：先规范化（折叠 `..`、统一分隔符），再确认结果仍在 `<root>` 之下，比较时按路径分段而不是字符串前缀。越界返回 `FORBIDDEN`。

`filename` 只允许是最后一段，不含分隔符。清单里出现带路径的 `filename` 属于数据错误，写入时直接拒绝，不做静默修正。

## 8. 与供应的关系

`supply` 不直接下载到目标目录，而是：

```
缓存命中 ──► 复制到 dest ──► 重算 dest sha256 ──► 写供应记录
缓存缺失 ──► 按 sources 顺序 fetch 到缓存 ──► 再复制到 dest
```

好处是同一个资产供应到十个目录只下载一次，且每次都从同一份已验证的字节复制。代价是缓存要占空间，所以清理策略必须能跑，`prune` 就是为此存在的。

`fetch --manifest ./assets.json --asset pvm1005-firmware` 单独把某个资产预热进缓存，适合在批量供应前先跑一遍。

## 9. 容量建议

- 缓存目录放在本地盘，不要放网络盘。内容寻址依赖 `rename` 的原子性，跨文件系统或跨协议的网络盘不保证。
- 留出至少「单次批量供应体积 × 2」的空间：一份在缓存，一份在 `_incoming` 过渡。
- `_quarantine` 定期人工看一眼再清。它不是垃圾，是故障证据。
- 缓存可以随时整个删掉，代价只是一次全量重新下载。它不含唯一数据，也不含凭据。
