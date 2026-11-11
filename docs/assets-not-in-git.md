# 不进 Git 的资产清单

有些资产**故意不放进任何 Git 仓库**：体积大、含厂商二进制、或含明文凭据。它们由
`asset-tools` 的 manifest 引用，按需从节点存储取件，取件后校验哈希。本文记录这些
边界与已核验的指纹，避免"仓库里没有就等于不存在"。

规则：**manifest 里登记 `sha256` 与来源路径；仓库只记录路径、大小、哈希与用途。**

## pudCorntrol（PDU 电源控制原始包）

| 项 | 值 |
|---|---|
| 路径 | `1006:/srv/storage/ark/artifacts/comm-tools/pudCorntrol.rar` |
| 大小 | 185,754,459 B |
| SHA-256 | `76fe34431505d9dc676d5d8c5579c930d9846747ee49f6d4120f3e05a3c2a673` |
| 容器 | RAR5（`52 61 72 21 1a 07 01 00`），无加密、无分卷 |
| 解包后 | 74 个文件 / 18 个目录，196,614,741 B |
| 核验 | NOAH-58，只读挂载读取，未在存储节点写入 |

**不进 Git 的内容**：`pudCorntrol.rar` 本体、`dist/pduControl.exe`（PyInstaller）、
`build/`、`.idea/`、`__pycache__/`、二级压缩包、厂商手册与 Modbus 调试工具二进制。

**已进 Git 的内容**：只有行为与合约。适配器在 `device-io` 仓库
（`device_io/pdu/`），依据是 NOAH-58 的清单 `pudcorntrol-inventory.json`
（OID 表、读写编码、Modbus 帧、恢复流程）。厂商代码里的硬编码社区串**没有**被带过来。

取件与复核：

```bash
# 只读计划，不要 --apply
python3 -m asset_tools supply --manifest <manifest.json> --target <dir>
# 取回后核对
sha256sum pudCorntrol.rar   # 应为 76fe3443…a673
```

## 厂商二进制与手册的一般规则

| 类型 | 处理方式 |
|---|---|
| 安装包 / `exe` / `.msi` | 不进 Git；需要时从 manifest 取件，登记哈希 |
| 二级压缩包（`rar`/`7z`） | 不进 Git；先解包核验，再把"需要的部分"重写成源码或文档 |
| 厂商手册（PDF/Word） | 不进 Git；结论摘进 `docs/` 或 `ark-kb`，附页码与版本 |
| 明文口令 / 社区串 / token | 任何情况下都不进 Git；见 `docs/security.md` |

## 冻结的历史仓库（NOAH-58）

为避免"整合期间上游又改一版"，下列仓库在 NOAH-58 打了 `freeze-20261111` 标签，
README 加了冻结说明，远端已复核；没有删除任何内容：

- `polaris-device-validation`
- `listenai-laid-installer`
- `burn_bundle`
- `a4m-Jenkins`

解冻前请先确认下游已经切到 `device-io` 的接口。
