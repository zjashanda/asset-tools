<#
.SYNOPSIS
  asset-tools 启动器（Windows）：不安装也能直接跑。

.EXAMPLE
  .\bin\asset-tools.ps1 status --json

.NOTES
  需要 Python 3.8+。可用 $env:ARK_ASSET_TOOLS_PYTHON 指定解释器路径。
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:ARK_ASSET_TOOLS_PYTHON) { $env:ARK_ASSET_TOOLS_PYTHON } else { 'python' }

if (-not (Get-Command $python -ErrorAction SilentlyContinue)) {
    Write-Error "asset-tools: 找不到 $python，请安装 Python 3.8+ 或设置 `$env:ARK_ASSET_TOOLS_PYTHON"
    exit 2
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$root;$env:PYTHONPATH" } else { $root }
& $python -m asset_tools @Arguments
exit $LASTEXITCODE
