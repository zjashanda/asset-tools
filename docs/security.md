# 安全

这个仓库是公开的。所有安全设计围绕一条前提：**任何一次误提交都可能已经被别人看过**。所以凭据从不进仓库，连「看起来像凭据」的字符串也不进。

## 1. 凭据清单

仓库里只允许出现变量**名字**。下表是全量清单，值是 `<env>`，没有例外。

| 变量名 | 用途 | 由谁读取 | 本仓是否读取其值 |
|---|---|---|---|
| `ARK_STORAGE_SSH_PASS` | 1006 存储节点 SSH 口令 | `node-storage` 适配器，交给 `sshpass -e` | 是，只进子进程环境 |
| `ARK_OSS_ACCESS_KEY_ID` | OSS AccessKey ID | `oss` 适配器 | 是 |
| `ARK_OSS_ACCESS_KEY_SECRET` | OSS AccessKey Secret | `oss` 适配器 | 是 |
| `EMAIL_ACCOUNT` | 企业邮箱账号 | `qw-email-handle` 自己读 | 否 |
| `EMAIL_PASSWORD` | 企业邮箱口令 | `qw-email-handle` 自己读 | 否 |
| `NAS_ACCOUNT` | NAS 账号 | `fileball` 自己读 | 否 |
| `NAS_PASSWORD` | NAS 口令 | `fileball` 自己读 | 否 |
| `GITHUB_TOKEN` | 仓库访问令牌 | 外部工具（如 `git`、`gh`） | 否 |
| `ACCESS_KEY_ID` / `ACCESS_KEY_SECRET` | 通用对象存储密钥 | 外部工具 | 否 |
| `LSTTS_TTS_TOKEN` | 语音服务令牌 | 外部工具 | 否 |
| `STORAGE_HOST` / `STORAGE_USER` / `STORAGE_PASSWORD` | 通用存储端点与凭据 | 外部工具 | 否 |

「本仓是否读取其值」这一列很重要。`EMAIL_*` 与 `NAS_*` 本仓根本不去 `os.environ` 取值，因为邮件与 NAS 协议由被委托的 CLI 负责；本仓只需要知道「这个变量该存在」，用来在 `probe` 里提示缺失。

非凭据的配置类变量（可以出现在文档与示例里，值也只是示例）：`ARK_OSS_ENDPOINT`、`ARK_OSS_BUCKET`、`ARK_STORAGE_HOST`、`ARK_STORAGE_USER`、`ARK_STORAGE_PORT`、`ARK_STORAGE_BASE_DIR`、`ARK_ASSET_CACHE`、`ASSET_OSS_CLI`、`ASSET_EMAIL_CLI`、`ASSET_NAS_CLI`、`ASSET_HTTP_TIMEOUT`、`ASSET_ADAPTER_TIMEOUT`。

`config/env-keys.json` 只列变量名，值一律写 `<env>`。它有三个用途：给 `status` 提供「该有哪些变量」的判据、给文档提供统一名字、给扫描提供白名单依据。

## 2. 脱敏

`util.redact(text) -> str` 是全仓唯一的脱敏出口。

规则：

1. 从 `os.environ` 取已知凭据变量的值（上面第一张表里的那些）。
2. 值长度 `< 6` 跳过。太短的值替换会误伤正常文本，而且短口令本身不构成有意义的泄漏面。
3. 空值跳过。
4. 命中即替换成 `***`，全部替换，不只是第一次出现。

调用点：

| 位置 | 为什么 |
|---|---|
| `FetchResult.detail` | 子进程 stderr 可能带命令行回显 |
| `AdapterStatus.detail` | CLI 的报错可能带配置内容 |
| 供应记录写入前 | 记录会被复制、上传、贴进 issue |
| manifest 写盘前 | 同上 |
| 异常信息 | `OSError`、`subprocess` 异常可能带环境内容 |
| `status` 的人类可读输出 | 终端截图会流传 |

只脱敏「已知变量的值」有边界：一个从没出现在环境里的密钥当然脱不掉。所以脱敏是第二道防线，第一道是根本不把凭据传进可能被打印的位置。

## 3. `tools/secret_scan.py`

扫描仓库所有非二进制文件，五类规则：

| # | 规则 | 正则 / 判定 |
|---|---|---|
| 1 | GitHub token | `gh[pousr]_[A-Za-z0-9]{20,}`、`github_pat_[A-Za-z0-9_]{20,}` |
| 2 | 云厂商 key | `AKIA[0-9A-Z]{16}`、`LTAI[A-Za-z0-9]{12,}` |
| 3 | 私钥头 | `-----BEGIN (RSA\|OPENSSH\|EC\|PGP\|PRIVATE) ` |
| 4 | 赋值型密码 | `(?i)(password\|passwd\|secret\|token\|access_key)\s*[=:]\s*["']?[^\s"'$<{]{8,}` |
| 5 | 禁用文件名 | `.git-credentials`、`id_rsa`、`id_ed25519`、`*.pem`、`*.pfx`、`auth.json` |

输出 `PASS` 或 `FAIL`，命中时给出文件路径、行号与片段（片段本身过一遍截断，不整行回显）。

两个开关：

```bash
# 规则自检：用内存样本验证五条规则都还能命中
python3 tools/secret_scan.py --self-test

# 门禁模式：有命中就退出 1
bash tools/secret-scan.sh --gate
```

`--self-test` 的意义在于防止「规则被改坏之后扫描永远 PASS」。五条规则各造一个内存样本，必须全部命中，少一条就算自检失败。它不读磁盘，也不会把样本写出去。

退出码只有两个：`0` 干净，`1` 有命中或自检失败。

### 白名单

`tools/scan-allowlist.txt` 只放两类东西：

1. 变量名占位，例如 `EMAIL_PASSWORD=<env>`。注意 `<env>` 里的 `<` 本身就让规则 4 的 `[^\s"'$<{]` 匹配不上，这类占位天然安全。
2. 文档里必须出现的示例变量名。

白名单按行精确匹配，不做通配。往里面加东西必须写理由，因为它就是「允许这一处看起来像密钥」的名单。

## 4. 提交前门禁

本地与 CI 用同一套检查，顺序固定：

```bash
# 1) 先确认扫描器自身没坏
python3 tools/secret_scan.py --self-test

# 2) 再扫全仓
bash tools/secret-scan.sh --gate

# 3) 跑测试，run-all.sh 最后一步会自动再扫一次
bash tests/run-all.sh
```

三条都过再提交。`tests/run-all.sh` 把 secret scan 放在最后，是为了让扫描看到的是完整工作树，而不是某个中间状态。

人工复核清单，机器扫不到但人会犯的错：

- 文档里贴了真实内网地址却没标「示例」。本仓约定：`10.11.63.24`、`storage1006` 之类只作为示例出现，并且必须带「示例 / 需按环境覆盖」的说明。
- 截图、日志片段、错误堆栈里带出了回显的命令行。
- 供应记录被手工编辑后贴进 issue，里面带了 `source_ref` 的完整路径与主机名（路径不算凭据，但内网拓扑也不该公开）。

## 5. 万一泄漏了

按这个顺序处理，不要反过来：

1. **先轮换**。公开仓库的提交历史会被镜像、被爬虫抓取，删掉提交不等于没人拿到。先把口令、token 换掉。
2. **再清理**。把文件从工作树删除，重写历史并强推（这一步需要单独批准，且会影响所有克隆）。
3. **再扫描**。跑 `secret_scan.py --self-test` 与 `--gate`，确认工作树干净。
4. **留记录**。在 issue 里记：泄漏了什么变量名（只写名字）、发现时间、轮换时间。不写值。

顺序之所以是「先轮换」，因为清理历史需要时间，而轮换只要一分钟。

## 6. 本仓不做的安全事

- 不做访问控制。谁能读仓库由平台决定，本仓只管不往里放东西。
- 不管理凭据生命周期。轮换是人的事，本仓只负责不落盘。
- 不审计下游。`venusahisiburner`、`lstest` 怎么用这些文件，不在本仓范围。
- 不信任上游内容。sha256 校验是唯一的事实判定，来源说什么都不算数。
