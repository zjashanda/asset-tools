# 1006 TTS 共享音频库（asset-tools）

- 状态：v1.0.0 已实现（NOAH-66）
- 时间：2026-11-12 17:05:00
- 依据：`result/tts-library-proposal.md` 规格 v1.0.0
- 规范入口：`asset-tools tts-library {key,lookup,fetch,publish,verify,index,status}`

## 1. 责任边界

TTS 共享音频库的 canonical 实现落在 `asset-tools`。其它仓库只消费、不重建。

| 仓库 / 组件 | 职责 | 明确不做 |
|---|---|---|
| `asset-tools` | 缓存键、查找、取件复制、发布、锁、ACL、索引、校验、outbox | 不合成音频、不做派生处理、不存凭据 |
| `tts-runtime` | 唯一 TTS 合成入口；只在 cache miss 时合成 | 不写 1006、不建库、不判断复用 |
| `audio-runtime` | 在任务副本上做重采样 / 增益 / 降噪 / 裁剪 | 不碰库原件 |
| `lstest` | 薄适配：消费任务目录副本 | 不对共享库读写 |
| `ark-kb` | 只登记可搜索资产索引 | 不存音频本体、不复制全文 |
| 1006 | 不可变 canonical 存储 | 不做原地修改、不做未经审批的删除 |

硬规则：**先查库、miss 才合成**。责任仓库是 `asset-tools`；合成负责人是
`tts-runtime`。任务目录只放库副本与派生结果，任何派生产物都不能回写库。

## 2. 目录布局（1006）

```text
/srv/storage/ark/artifacts/audio/tts-library/
├── index.json                     # 可搜索索引，原子重写
├── locks/
│   └── tts-<key16>.lock            # 每资产一把锁
├── _incoming/
│   └── tts-<key16>/                # 上传暂存，成功后才 rename
├── _quarantine/
│   └── tts-<key16>-<stamp>/        # 损坏对象隔离区
└── tts-<key16>/                    # 一个资产一个目录，发布后只读
    ├── manifest.json               # ark.tts-asset.v1
    └── <audio_sha256>.wav          # 文件名由内容哈希决定
```

同一 asset_id 出现不同音频 SHA 时，新目录为 `tts-<key16>-v2`，旧目录保留，
新 manifest 的 `supersedes` 指向旧目录名。删除、GC、配额策略一律单独审批。

## 3. 缓存键（确定性）

参与计算的字段（未设置的参数显式写 `null`，不允许省略）：

| 字段 | 说明 |
|---|---|
| `normalized_text` | 规范化后的文本 |
| `language` | 大写语言码，例如 `ZH` / `EN` / `ZH-CN` |
| `voice.speaker` | 音色名；不允许写路径 |
| `voice.voice_ref_sha256` | 参考音频内容哈希；与 speaker 二选一 |
| `engine` | 引擎名，例如 `lstts` |
| `model` | 模型名，例如 `IndexTTS` |
| `model_version` | 模型版本 |
| `synthesis_params` | `rate` / `pitch` / `style` / `emotion` / `speed` |
| `output` | `container` / `codec` / `sample_rate` / `channels` / `bit_depth` |
| `normalization_profile` | 文本规范化档位；未声明按 `none` |

文本规范化顺序：Unicode NFKC → 删除零宽字符 → 去首尾空白 → 连续空白折叠为
单个半角空格。语言统一大写。`voice` 二选一，绝对路径永远不进键。

确定性序列化：

```python
json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

`cache_key = sha256(canonical_json)`（64 位小写十六进制）；
`asset_id = "tts-" + cache_key[:16]`。换模型、换参数、换音色都会得到新键。

## 4. 任务流程（唯一顺序）

1. 由完整规格生成 cache key 与 asset_id。
2. `lookup` 查 1006 索引；本地任务缓存次之。
3. 命中 → 校验 manifest 与音频 SHA → `fetch` 复制到
   `<task>/assets/tts/original/<asset_id>/`。
4. 未命中 → `tts-runtime synth` 输出到 `<task>/work/tts/<asset_id>/`。
5. 用 `audio-runtime` 或 `tts-runtime inspect` 做质检。
6. `publish`：暂存 → 锁 → compare-and-create → 原子 rename → 重建索引 → query-back。
7. 失败 → 写 outbox（pending）；任务可继续用本地结果，但不得声称公共库已更新。
8. 任务内二次处理只写 `<task>/work/audio/` 或 `<task>/result/audio/`，并记录
   `derived_from_asset_id` 与 `processing_chain`。

`fetch` 必须复制，禁止硬链接、符号链接和可写共享挂载；落盘后默认置只读。
`publish` 必须带 `--task-dir`，用于 outbox 与审计。

## 5. `ark.tts-asset.v1`

必需字段见 `schemas/tts-asset.v1.json`。核心约束：

- `status ∈ {active, stale, superseded}`；
- `quality ∈ {formal, candidate, evidence_gap}`；
- `scope.visibility ∈ {public, project, restricted}`；
- `project` 必须有非空 `projects`；`restricted` 必须有 `projects` 或 `nodes`；
- `audio.filename` 必须等于 `<audio.sha256>.wav`，且 SHA 与文件实测一致；
- `text.raw_sha256` 只存原始文本哈希；manifest 不保存原始敏感全文；
- 派生资产默认不发布；如登记则 `derived_from_asset_id` 不得为空；
- 资产目录发布后不可原地修改；变更只能产生新版本目录。

## 6. 命中与拒绝矩阵

自动复用必须同时满足：`quality=formal`、`status=active`、`shareable=true`、
完整 cache key 相等、SHA 校验通过、ACL 允许。

| 条件 | 默认行为 | 例外 |
|---|---|---|
| 完整 cache key 相等 | 命中 | — |
| 仅文本 / 音色相似 | 只返回候选，不构成命中 | 需 `--allow-candidate --confirm` 才可作为任务输入 |
| `quality=candidate` / `evidence_gap` | 拒绝 | 同上 |
| `status=stale` / `superseded` | 拒绝自动复用 | `--include-stale` 仅人工核查 |
| SHA 不符 / 不可读 | 拒绝并隔离 | 重新合成后可发布新版本 |
| `shareable=false` | 发布拒绝，退出码 4 | 无 |
| ACL 越权 | 拒绝，退出码 4 | 无 |
| 1006 不可达 | 退出码 3，`unreachable=true` | 不得输出「库里没有」 |

候选搜索只按 `language` + 规范化文本哈希 + `voice` 匹配，返回
`match="candidate"`；候选永远不会被标记为 `hit`。

## 7. ACL 与作用域

| visibility | 读取条件 |
|---|---|
| `public` | 任何人可读，可跨项目复用 |
| `project` | 调用方 `--project` 命中 `projects` |
| `restricted` | 调用方 `--project` 或 `--node` 命中白名单 |

越权读取与越权取件都返回 `FORBIDDEN`（退出码 4），且不泄漏候选列表。

## 8. 锁、幂等与并发

- 锁文件：`<root>/locks/<asset_id>.lock`，`O_CREAT|O_EXCL` 创建。
- 持锁超过 600 秒视为陈旧锁，可回收并记录 `stale_lock_reclaimed`。
- compare-and-create：拿到锁后重读目标目录。
  - 不存在 → rename 发布，`created=true`；
  - 存在且 SHA 相同 → 幂等成功，`created=false`，不写第二份；
  - 存在但 SHA 不同 → 发布 `-v2`，旧目录不动。
- `idempotency_key = sha256(asset_id + "|" + audio.sha256)`。
- `_incoming` 残留不参与 lookup；重放时先清理再重试。
- 发布后 query-back：重读 manifest 与音频并重算 SHA；不一致则
  `query_back=fail`、outbox 留 pending、退出码 1。

## 9. outbox

任何发布失败都会写 `<task>/receipts/outbox/<idempotency_key>.json`：

```json
{
  "schema": "ark.tts-outbox.v1",
  "idempotency_key": "<64hex>",
  "asset_id": "tts-<key16>",
  "status": "pending",
  "reason": "SENSITIVE_DENIED",
  "attempts": 1,
  "created_at": "2026-11-12 17:05:00",
  "updated_at": "2026-11-12 17:05:00"
}
```

同一 idempotency_key 重放时 `attempts` 累加；成功发布后状态改为 `resolved`。
1006 恢复后可直接重放，不会产生第二个资产。

## 10. 退出码

| 码 | 名称 | TTS 库场景 |
|---|---|---|
| 0 | OK | 命中 / 发布 / 校验成功（含幂等成功） |
| 1 | FAIL | SHA 不符、query-back 失败、outbox 重放失败 |
| 2 | USAGE | 参数缺失或规格不完整（例如缺 `--task-dir`） |
| 3 | NO_SOURCE | 库不可达、未命中、资产不存在 |
| 4 | FORBIDDEN | 敏感发布、ACL 越权、路径越界、只读库写入 |
| 5 | NETWORK | 1006 / 远端传输失败（含超时） |
| 6 | NEEDS_ELEVATION | 需要提权（当前版本不触发） |

## 11. 索引与受控位置

`index.json` 只登记元数据与相对库根的受控位置，不存音频、不存完整文本：

- `asset_id`、`cache_key`、`version`、`dir`；
- `audio.sha256`、`audio.bytes`、`audio.path`；
- `status`、`quality`、`shareable`、`visibility`；
- `language`、`text_normalized_sha256`、`voice`、`engine`；
- `policy.rule` 固定为「先查库、miss 才合成」，`responsible_repo` 为
  `asset-tools`。

调用方拿到的是相对路径；取件必须经 `asset-tools fetch`，禁止依赖裸绝对路径。
重建索引时先写同目录临时文件再 `os.replace`，条目按 `asset_id` 稳定排序。

## 12. 命令示例

计算键：

```bash
asset-tools tts-library key --spec spec.json --json
```

查询：

```bash
asset-tools tts-library lookup --spec spec.json --project NOAH-66 --json
asset-tools tts-library lookup --spec spec.json --allow-candidate --confirm --json
```

取件：

```bash
asset-tools tts-library fetch --spec spec.json \
  --task-dir /home/zhsh/workspace/tasks/NOAH-66 --json
```

发布：

```bash
asset-tools tts-library publish --staging /home/zhsh/workspace/tasks/NOAH-66/work/tts/tts-0123456789abcdef \
  --task-dir /home/zhsh/workspace/tasks/NOAH-66 --json
```

巡检：

```bash
asset-tools tts-library status --root /srv/storage/ark/artifacts/audio/tts-library --json
asset-tools tts-library verify --root /srv/storage/ark/artifacts/audio/tts-library --json
asset-tools tts-library index  --root /srv/storage/ark/artifacts/audio/tts-library --json
```

## 13. 验收对照

| # | 用例 | 覆盖测试 |
|---|---|---|
| 1 | 同规格两个任务先后取件 | `test_publish_twice_same_spec_is_idempotent` |
| 2 | 五个关键字段各改一项 | `test_cache_key_fields_differ` |
| 3 | 任务副本处理不影响库原件 | `test_fetch_copies_and_does_not_mutate_library` |
| 4 | 并发同 key 发布 | `test_concurrent_publish_single_canonical_asset` |
| 5 | 发布中断 + outbox 重放 | `test_publish_failure_writes_outbox_and_replay_is_idempotent` |
| 6 | 损坏对象隔离与重发 | `test_corrupt_asset_rejected_quarantined_and_republishable` |
| 7 | 1006 不可达 | `test_unreachable_root_exit_3` |
| 8 | 敏感文本发布 | `test_sensitive_publish_denied_exit_4` |
| 9 | ACL / 作用域 | `test_acl_project_and_node` |
| 10 | candidate / stale | `test_candidate_and_stale_policy` |
| 11 | 索引与受控位置 | `test_index_rebuild_is_stable_and_has_controlled_paths` |
| 12 | 入口文案与责任仓库 | `test_index_policy_text_and_docs` |

## 14. 明确不做

- 不实现 TTS 合成，不调用任何引擎。
- 不实现 1006 上传凭据管理；凭据仍由环境变量提供。
- 不执行 GC / 配额 / 保留策略；只登记需求，另行审批。
- 不做任务内派生处理；派生只发生在任务副本。
- 不修改 Multica Server / daemon 后端。
