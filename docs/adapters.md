# 适配器

适配器是「一个来源」的全部实现。它只回答三个问题：这来源现在可用吗、它有哪些候选、怎么把一个候选取到本地。主流程不认识具体来源，只认这三个问题的答案。

## 1. 数据契约

`asset_tools/adapters/base.py` 定义四个类型，所有适配器共用。

### `AdapterStatus`

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | string | 适配器名，与 `ADAPTERS` 的键一致 |
| `available` | bool | 是否可用 |
| `reason` | string | 可用为 `OK`；不可用为 `BLOCKED:ADAPTER_UNAVAILABLE` |
| `detail` | string | 人读补充说明，已脱敏 |
| `missing_env` | list | 缺失的环境变量**名字**，只列名不列值 |

### `AssetRef`

| 字段 | 类型 | 含义 |
|---|---|---|
| `type` | string | 来源类型，与适配器 `kind` 一致 |
| `filename` | string | 建议落盘名 |
| `ref` | string | 定位串：路径 / URL / object key / 邮件附件 id |
| `sha256` | string | 期望摘要，可空 |
| `bytes` | int | 期望字节数，0 表示未知 |
| `priority` | int | 越小越先，缺省 100 |
| `extra` | dict | 适配器私有信息，不参与排序 |

### `FetchResult`

| 字段 | 类型 | 含义 |
|---|---|---|
| `ok` | bool | 是否成功 |
| `path` | string | 落盘的本地路径 |
| `sha256` | string | 实际摘要 |
| `bytes` | int | 实际字节数 |
| `reason` | string | `OK` 或失败原因串 |
| `detail` | string | 人读说明，已脱敏 |
| `exit_code` | int | 与全仓退出码一致 |

### 三个方法

```python
def probe(self) -> AdapterStatus        # 绝不抛异常
def discover(self, query: dict) -> list[AssetRef]
def fetch(self, ref: AssetRef, dest_dir: Path) -> FetchResult
```

硬性要求：

1. `probe()` 绝不抛异常。缺凭据、缺 CLI 一律 `available=False`、`reason="BLOCKED:ADAPTER_UNAVAILABLE"`，并列出 `missing_env`。
2. `discover()` 在不可用时返回 `[]` 并记录 `status.reason`，绝不崩溃。空列表表示「没找到或没法找」，不是异常。
3. `fetch()` 校验 sha256：`ref.sha256` 非空时必须匹配，不匹配返回 `ok=False, reason="SHA256_MISMATCH", exit_code=EXIT_FAIL`。
4. 所有输出与异常都过 `util.redact()`。

## 2. registry

`asset_tools/adapters/__init__.py`：

```python
ADAPTERS = {
    "local": LocalAdapter,
    "http": HttpAdapter,
    "oss": OssAdapter,
    "node-storage": NodeStorageAdapter,
    "email": EmailAdapter,
    "nas": NasAdapter,
}
```

- `get_adapter(name, config=None)`：未知名字抛 `KeyError`，CLI 把它转成 `EXIT_USAGE`。
- `probe_all() -> list[AdapterStatus]`：按 `ADAPTERS` 的顺序逐个 `probe`，`status` 子命令直接用它。
- 名字是清单里 `sources[].type` 的取值，两边必须逐字一致。`node-storage` 带连字符，写成 `node_storage` 会被当成未知名字。

## 3. local

最朴素的来源：本机文件。

- `probe()`：恒 `available=True, reason="OK"`。
- `discover(query)`：`query` 里的 `path` 直接转成 `AssetRef`。
- `fetch(ref, dest_dir)`：读 `ref` 指向的文件，边读边算 sha256，写到 `dest_dir/<filename>`。

规则：

- `path` 必须存在且可读，否则 `EXIT_NO_SOURCE`。
- 越界路径返回 `FORBIDDEN`。越界指规范化后不在允许范围内，`..` 折叠之后仍然逃出边界的情况一律拒绝。
- 不做任何网络访问，因此离线机器上这条链路总是可用。

## 4. http

直链来源。

| 关注点 | 规则 |
|---|---|
| 协议 | 只允许 `http` 与 `https`；重定向后仍校验 scheme，跳到 `file:` 之类直接拒绝 |
| 超时 | `ASSET_HTTP_TIMEOUT`，默认 30s |
| 4xx | `EXIT_NO_SOURCE`（对方说没有，换来源） |
| 5xx / 连接错误 / 超时 | `EXIT_NETWORK`（可能是抖动，值得退避重试） |
| `Content-Length` | 与实际字节数不符时告警，**不失败**；以 sha256 为准 |
| 落盘 | 先写临时文件，校验通过再改名，失败不留半截文件 |

`Content-Length` 不参与判定是刻意的：中间可能有代理改写、可能有分块传输，长度只是个提示，摘要才是身份。

## 5. oss

对象存储来源。不引 SDK，走两条路。

需要四个变量，缺任一则 `BLOCKED` 并列出缺的名字：

| 变量 | 用途 |
|---|---|
| `ARK_OSS_ENDPOINT` | 服务端点 |
| `ARK_OSS_BUCKET` | 桶名 |
| `ARK_OSS_ACCESS_KEY_ID` | AccessKey ID |
| `ARK_OSS_ACCESS_KEY_SECRET` | AccessKey Secret |

取件顺序：

1. **委托 CLI**（优先）。配置了 `ASSET_OSS_CLI` 时调用：

   ```bash
   <cli> get --bucket <bucket> --key <key> --out <PATH>
   ```

   签名、临时凭证、私有桶都在 CLI 内部解决，本仓不参与。
2. **公开直链**（未配置 CLI 时）。用标准库 `urllib` 请求 `https://<bucket>.<endpoint>/<key>`，仅在对象公开或由 CLI 预签时可用。失败返回 `EXIT_NO_SOURCE`。

两条铁律：

- **绝不拼接、打印或落盘签名参数**。URL 里的 `Signature`、`OSSAccessKeyId`、`Expires` 一律不进日志、不进 manifest、不进供应记录。
- 变量缺失时不猜测、不降级到匿名访问，直接 `BLOCKED`，把缺失变量名交给 `status` 展示。

## 6. node-storage（1006 存储节点）

从存储节点取件。默认指向 `storage1006`（示例地址 `10.11.63.24:10027`，按环境覆盖）。

| 变量 | 必填 | 缺省 | 用途 |
|---|---|---|---|
| `ARK_STORAGE_SSH_PASS` | 是 | 无 | SSH 口令，只经环境交给 `sshpass` |
| `ARK_STORAGE_HOST` | 否 | `storage1006` | 主机 |
| `ARK_STORAGE_USER` | 否 | `administrator` | 用户 |
| `ARK_STORAGE_PORT` | 否 | `10027` | 端口 |
| `ARK_STORAGE_BASE_DIR` | 否 | `config/providers.example.json` 里的 `/srv/storage/ark/artifacts` | 允许访问的根 |

### 6.1 `sshpass -e` 规则

密码只能通过环境变量传进去，命令行走 `-e`：

```bash
export SSHPASS="$ARK_STORAGE_SSH_PASS"        # 只在这个进程的环境里
sshpass -e scp -P 10027 -o StrictHostKeyChecking=accept-new \
  administrator@storage1006:/srv/storage/ark/artifacts/tcl/pvm1005-v1.2.3.zip ./inbox/
unset SSHPASS
```

适配器内部同样用 `subprocess` + `sshpass -e`，把口令放在子进程环境里。

**禁止**用 `-p` 把口令写进命令行（`-p '<口令>'` 这种形态）。它会让口令明文出现在 `argv` 里，`ps`、`/proc/<pid>/cmdline`、shell history、崩溃日志都能拿到它。`-e` 从 `SSHPASS` 读，进程名里只有一个 `-e`。

配套要求：

- 缺 `sshpass` 或 `ARK_STORAGE_SSH_PASS` → `BLOCKED`，`missing_env` 里写变量名。
- `scp` 的 stderr 可能带主机指纹提示，输出前过 `util.redact()`。

### 6.2 `base_dir` 边界

远端路径必须在 `base_dir` 之下：

```
base_dir = /srv/storage/ark/artifacts

允许  /srv/storage/ark/artifacts/tcl/pvm1005-v1.2.3.zip
允许  tcl/pvm1005-v1.2.3.zip              （相对 base_dir）
拒绝  /srv/storage/ark/../env/env.json     （折叠后跳出）
拒绝  /etc/passwd                          （绝对路径，不在根下）
```

判定方式是先规范化（折叠 `..`、去掉重复分隔符），再比较前缀，比较时按路径分段而不是按字符串前缀。字符串前缀会把 `/srv/storage/ark/artifacts-evil/x` 误判成合法。

越界返回 `FORBIDDEN`，不返回 `NO_SOURCE`。这两者语义不同：`NO_SOURCE` 是「没有」，`FORBIDDEN` 是「不给」。

## 7. email（委托 `qw-email-handle`）

本仓不实现 IMAP/SMTP，也不读邮箱口令。它只调用一个外部 CLI。

### 7.1 定位与凭据

| 变量 | 说明 |
|---|---|
| `ASSET_EMAIL_CLI` | CLI 的绝对路径或 PATH 中的命令名，缺省尝试 `qw-email-handle` |
| `EMAIL_ACCOUNT` | 由被委托的 CLI 自己读取 |
| `EMAIL_PASSWORD` | 由被委托的 CLI 自己读取 |

**本仓只读取变量名，凭据由被委托的 CLI 自己读取。** `probe()` 只判断 CLI 是否存在且可执行；CLI 在不在、能不能登录，是它自己的事，本仓不替它传口令，也不解析它的错误。

### 7.2 协议

两条命令，没有第三条：`<cli> list --json` 与 `<cli> get <id> --out <DIR>`。

**列附件**

```bash
qw-email-handle list --json
```

stdout 是 JSON 数组：

```json
[
  {
    "id": "159",
    "subject": "【最新海思固件及APP版本同步】",
    "from": "fw-bot@example.com",
    "received_at": "2026-11-03 09:12:41",
    "filename": "HAS_TCL_CSK7_HI3863_LZX_20261103_TEST_V0.0.41.zip",
    "bytes": 52428800,
    "sha256": "…"
  },
  {
    "id": "148",
    "subject": "HISI firmware",
    "from": "fw-bot@example.com",
    "received_at": "2026-10-28 17:20:03",
    "filename": "V8-H363T20-WFMV206.111.1.16.bin",
    "bytes": 3694352
  }
]
```

`sha256` 是可选的：CLI 算过就带，没算过就省。省了不代表不能下载，只代表本仓在取件后要用 manifest 里的期望值兜底校验。

**取附件**

```bash
qw-email-handle get 159 --out /tmp/ark-assets-inbox
```

两种成功形态都算成功：

1. stdout 打印 `{"path": "/tmp/ark-assets-inbox/HAS_TCL_CSK7_....zip"}`；
2. 退出码 0，且 `--out` 目录里出现了同名文件。

适配器两种都认，优先用 stdout 的 `path`。

### 7.3 query 与失败

`discover(query)` 支持两个键：

| 键 | 作用 |
|---|---|
| `subject_contains` | 主题包含指定子串 |
| `filename_glob` | 附件名匹配 glob，例如 `*.zip` |

`ref` 用附件 `id`。

| 情形 | 结果 |
|---|---|
| CLI 不存在 / 不可执行 | `BLOCKED`，`detail` 里写尝试过的命令名 |
| CLI 超时 | `EXIT_NETWORK` |
| CLI 退出非零 | 按退出码映射，默认 `EXIT_NO_SOURCE` |

超时由 `ASSET_ADAPTER_TIMEOUT` 控制，默认 120s。附件大，给的时间比 HTTP 长。

## 8. nas（委托 `fileball`）

结构和 email 一样，只是命令与返回字段不同。

| 变量 | 说明 |
|---|---|
| `ASSET_NAS_CLI` | CLI 的绝对路径或命令名，缺省 `fileball` |
| `NAS_ACCOUNT` | 由 `fileball` 自己读取 |
| `NAS_PASSWORD` | 由 `fileball` 自己读取 |

NAS 地址也由 `fileball` 自己管理，本仓不关心它连的是 FileStation 还是别的。

**本仓只读取变量名，凭据由被委托的 CLI 自己读取。**

**列文件**

```bash
fileball list --json
```

```json
[
  {
    "path": "/fw/2026/pvm1005-v1.2.3.zip",
    "filename": "pvm1005-v1.2.3.zip",
    "bytes": 1048576,
    "sha256": "…"
  },
  {
    "path": "/tools/burn/1.0.10/Uart_Burn_Tool.exe",
    "filename": "Uart_Burn_Tool.exe",
    "bytes": 10892822
  }
]
```

**取文件**

```bash
fileball get /fw/2026/pvm1005-v1.2.3.zip --out /tmp/ark-assets-inbox
```

形如 `<cli> get <path> --out <DIR>`。

成功形态与 email 相同：stdout 打印 `{"path": "..."}`，或者退出 0 且目录里出现同名文件。

`ref` 用 `path`。`discover(query)` 认 `filename_glob`，语义与 email 的同名键一致；`query` 里认不出的键由适配器自行解释，不透传成本仓的契约。

## 9. 超时与原因串汇总

| 变量 | 默认 | 作用范围 |
|---|---|---|
| `ASSET_HTTP_TIMEOUT` | 30s | `http` 的单次请求 |
| `ASSET_ADAPTER_TIMEOUT` | 120s | 委托型适配器（`email`、`nas`）的整条 CLI 调用，也用于 `node-storage` 的传输上限 |

| 原因串 | 退出码 | 含义 | 下一步 |
|---|---|---|---|
| `OK` | 0 | 成功 | 无需处理 |
| `SHA256_MISMATCH` | 1 | 取到的内容与期望摘要不符 | 换来源；确认 manifest 里的 sha256 不是旧值 |
| `NO_SOURCE` | 3 | 对方没有这个对象、4xx、CLI 报找不到 | 换来源或修正 `ref` |
| `FORBIDDEN` | 4 | 路径越界、命中守卫 | 检查 `base_dir` 与路径是否被折叠出界 |
| `NETWORK` | 5 | 超时、连接失败、5xx | 退避重试；仍失败则换来源 |
| `BLOCKED:ADAPTER_UNAVAILABLE` | 不适用 | `probe` 报告适配器不可用 | 补 `missing_env` 里的变量，或装 CLI |

`BLOCKED:ADAPTER_UNAVAILABLE` 是 `probe` 的结论，不是 `fetch` 的返回值。`discover` 在不可用时返回空列表，`status` 负责把原因显示出来。

## 10. 加一个新来源

三步，不需要动主流程：

1. 在 `asset_tools/adapters/` 下新增一个类，继承 `Adapter`，实现 `probe` / `discover` / `fetch`。
2. 在 `ADAPTERS` 里登记，键名就是清单里 `sources[].type` 的取值。
3. 在 `config/env-keys.json` 里补上它需要的变量名（值写 `<env>`），让 `status` 与 secret scan 都认得。

如果新来源是「委托一个已有 CLI」，照 `email` / `nas` 的两条命令协议写，不要自己实现协议。委托的意思是责任分界清楚：本仓只读变量名，凭据由被委托的 CLI 自己读取。
