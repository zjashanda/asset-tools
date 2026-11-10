# 操作手册

日常操作只有三类：看状态、验内容、清空间。加上两类排障：取件失败与凭据缺失。所有命令默认 dry-run，落盘一律显式 `--apply`。

## 1. 日常巡检

| 频次 | 动作 | 命令 |
|---|---|---|
| 每天 | 看来源可见性与缓存体积 | `bash bin/asset-tools status` |
| 每次供应后 | 校验落位结果 | `bash bin/asset-tools verify --manifest ./assets.json --dir ./inbox` |
| 每周 | 看缓存可回收空间 | `bash bin/asset-tools cache prune --keep-days 30` |
| 每周 | 全量校验缓存 | `bash bin/asset-tools cache verify` |
| 提交前 | 凭据门禁 | `bash tools/secret-scan.sh --gate` |

### 看状态

```bash
bash bin/asset-tools status
bash bin/asset-tools status --json
```

人读输出三段：适配器表（名字、可用性、缺什么变量）、缓存统计（条目数、总字节、根目录）、manifest 默认路径。

`status` 只读。跑多少次都不会改变任何东西，所以可以放心放进巡检脚本每五分钟跑一次。

### 验内容

```bash
# 整份清单
bash bin/asset-tools verify --manifest ./assets.json

# 单个资产
bash bin/asset-tools verify --manifest ./assets.json --asset pvm1005-firmware

# 校验已经供应出去的目录，而不是缓存
bash bin/asset-tools verify --manifest ./assets.json --dir ./inbox
```

输出逐条 `OK` / `MISSING` / `CORRUPT`。`MISSING` 不是错误，只是「这份还没取」。`CORRUPT` 是错误，要处理。

### 清空间

```bash
bash bin/asset-tools cache prune --keep-days 30          # dry-run
bash bin/asset-tools cache prune --keep-days 30 --apply  # 执行
bash bin/asset-tools cache list                          # 看条目
```

清之前先看 `_quarantine` 的单独计数。隔离区变大通常意味着上游在变或者网络在丢包，值得查一下再清。

### 一条命令自检

```bash
bash bin/asset-tools status && bash bin/asset-tools plan --manifest ./assets.json
```

`status` 说环境能用，`plan` 说这份清单能落地。两条都过，日常供应基本不会出问题。

## 2. 取件失败排查

先拿到失败原因。`plan` 与 `--json` 输出里都有，退出码也能区分大类：

| 退出码 | 原因串 | 含义 | 先查什么 | 下一步 |
|---|---|---|---|---|
| 3 | `NO_SOURCE` | 所有来源都拿不到，或对方明确说没有（4xx） | `plan` 里每个来源的尝试结果；对象 key、路径、附件 id 是否写错 | 换来源；确认上游还在发布这个版本 |
| 5 | `NETWORK` | 超时、连接失败、5xx、适配器超时 | 网络与端口；`ASSET_HTTP_TIMEOUT` / `ASSET_ADAPTER_TIMEOUT` 是否太小 | 退避重试；仍失败换来源 |
| 4 | `FORBIDDEN` | 路径越界或命中安全守卫 | `node-storage` 的 `ARK_STORAGE_BASE_DIR` 与实际路径；`local` 的路径是否折叠出界 | 修正路径；不要绕过边界 |
| 1 | `SHA256_MISMATCH` | 取到的字节与期望摘要不符 | manifest 里的 `sha256` 是否过期；上游是否换了同名文件 | 换来源；确认版本后更新清单 |
| 2 | `USAGE` | 参数错、未知适配器、清单里查不到该 id | `--asset` 的 id 拼写；`sources[].type` 是否拼成 `node_storage` | 改参数，不重试 |
| 不适用 | `BLOCKED:ADAPTER_UNAVAILABLE` | `probe` 判定适配器不可用 | `status` 里的 `missing_env` | 补变量或装 CLI，见第 3 节 |

### NO_SOURCE

最常见，也最容易被误判成「网络坏了」。区分方法：

```bash
bash bin/asset-tools plan --manifest ./assets.json --json
```

看每个资产下面列了几个来源、各自什么状态。全部来源都 `NO_SOURCE` 时，问题在清单（id 或定位写错），不在网络。只有一个来源失败、其他成功时，问题在那个来源。

### NETWORK

先确认是不是本机的问题：

```bash
bash bin/asset-tools status --json
```

适配器可用但连接全挂，通常是出口网络或 DNS。重试一次，还挂就换来源，不要在原地打转。

超时也要看一眼：`ASSET_HTTP_TIMEOUT` 默认 30s、`ASSET_ADAPTER_TIMEOUT` 默认 120s。大文件在慢链路上，30s 确实会断。

### FORBIDDEN

`node-storage` 的路径必须在 `base_dir` 之下，且折叠 `..` 之后仍在根里。检查：

```bash
echo "$ARK_STORAGE_BASE_DIR"     # 没设则是 config 里的默认 /srv/storage/ark/artifacts
```

清单里写的是绝对路径时，它必须以上面的根开头，且按路径分段比较。`/srv/storage/ark/artifacts-old/x` 不算在根下。

`FORBIDDEN` 不要靠放宽边界解决。边界是防误删和防越权的，绕过去之后下一次就会更麻烦。

### SHA256_MISMATCH

先分清是谁错：

```bash
bash bin/asset-tools fetch --manifest ./assets.json --asset pvm1005-firmware
bash bin/asset-tools cache verify
```

- 缓存里已有同名不同内容的条目，且 `cache verify` 报 `CORRUPT`：缓存被破坏，`cache prune --apply` 隔离后重取。
- 换了来源还是一样：manifest 的 `sha256` 过期，上游发了新版本但沿用了文件名。确认后更新清单，别改摘要去迁就文件。

### BLOCKED:ADAPTER_UNAVAILABLE

见下一节。

## 3. 凭据缺失排查

```bash
bash bin/asset-tools status --json
```

输出里每个不可用适配器都有 `missing_env`，只列需要补的变量名。

| 适配器 | 需要补的 | 备注 |
|---|---|---|
| `oss` | `ARK_OSS_ENDPOINT`、`ARK_OSS_BUCKET`、`ARK_OSS_ACCESS_KEY_ID`、`ARK_OSS_ACCESS_KEY_SECRET` | 四个缺一个就 BLOCKED，不降级到匿名访问 |
| `node-storage` | `ARK_STORAGE_SSH_PASS`，以及本机有 `sshpass`、`scp` | 口令只经环境传，用 `sshpass -e` |
| `email` | `ASSET_EMAIL_CLI`（或 PATH 里有 `qw-email-handle`） | `EMAIL_ACCOUNT` / `EMAIL_PASSWORD` 由该 CLI 自己读，本仓不传 |
| `nas` | `ASSET_NAS_CLI`（或 PATH 里有 `fileball`） | `NAS_ACCOUNT` / `NAS_PASSWORD` 由该 CLI 自己读 |
| `local` | 无 | 恒可用 |
| `http` | 无 | 恒可用，失败原因是网络或 4xx/5xx |

三类常见原因：

1. **变量在新 shell 里没继承**。`status` 看到的和 `supply` 看到的是同一个进程环境，所以先在同一条命令里确认：
   ```bash
   bash bin/asset-tools status
   ```
   显示 BLOCKED 就是真的没设，不是缓存了旧结果。
2. **CLI 装了但不在 PATH 里**。用绝对路径最稳：
   ```bash
   export ASSET_EMAIL_CLI=/opt/ark-tools/bin/qw-email-handle
   ```
3. **CLI 在但本机没有 `sshpass`**。`node-storage` 会明确报缺；装好之前它一直 BLOCKED，这是预期行为，不要改成手工 scp。

变量值本身的问题本仓看不到：邮箱登录失败、NAS 密码错、OSS AccessKey 被禁用，都表现为被委托 CLI 的非零退出，本仓按退出码映射成 `NO_SOURCE` 或 `NETWORK`。这时去那个 CLI 自己的日志里查，问题不在这一层。

**不要**为了排查把变量值打印出来。要确认变量有没有设，用：

```bash
test -n "$ARK_STORAGE_SSH_PASS" && echo "set" || echo "missing"
```

只输出 `set` 或 `missing`，不输出值。

## 4. 常见场景

### 供应到机台后下游说没文件

```bash
ls -l ./inbox/                                   # 文件在不在
bash bin/asset-tools verify --manifest ./assets.json --dir ./inbox
cat ./inbox/.ark-supply/pvm1005-firmware.json    # 记录里的 dest_path 是不是它要的
```

多半是 `install.material_rel` 与下游期望的目录不一致。`material_rel` 缺省用 `filename`，写了就按写的走。

### 转换后 id 重复

旧清单里两条同名不同版本，转换后 id 撞在一起。用 `merge` 的规则处理：同 id 同 sha256 合并来源，不同 sha256 按 `discovered_at` 保留新的，被替换的进 `provenance.note`（前缀 `superseded:`）。

```bash
bash bin/asset-tools manifest validate --manifest ./assets.json
```

`validate` 只报结构错误，不报语义重复。id 重复要在 `plan` 输出里看条数。

### 同一资产每次都要重新下载

缓存没命中，或者 `install.cache_rel` 与真实路径不一致。检查：

```bash
bash bin/asset-tools cache list
bash bin/asset-tools plan --manifest ./assets.json
```

`cache_rel` 必须是 `sha256/<前2>/<次2>/<sha256>/<filename>`。不一致的话，缓存永远算不中。

### 清缓存之后磁盘没变小

`_incoming` 里有残留（进程被杀留下的半截文件），或者删的是别处的缓存根。先 `status` 确认根目录，再 `cache prune` 看两个特殊目录的单独计数。
