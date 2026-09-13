<#
.SYNOPSIS
  启动 AIVoiceBench v0.4.0-alpha.6 预览版（独立容器名与独立数据卷，仅绑定本机）。

.DESCRIPTION
  - 从发布附件中的镜像包 docker load 镜像，不需要在本机重新编译源码。
  - 使用独立容器名 aivoicebench-preview 与独立数据卷，不影响任何既有版本的数据。
  - 默认仅绑定 127.0.0.1。
  - 不会自动删除已有容器或数据卷；端口或名称冲突时明确提示并退出。

.PARAMETER Tarball
  镜像包路径。默认取与本脚本同目录的 aivoicebench-v0.4.0-alpha.6.tar.gz。

.PARAMETER Port
  宿主机端口，默认 8000。被占用时脚本会提示改用其他端口。

.PARAMETER SkipLoad
  跳过 docker load（镜像已存在时使用）。

.EXAMPLE
  .\start-aivoicebench-preview.ps1
.EXAMPLE
  .\start-aivoicebench-preview.ps1 -Port 8001
#>
[CmdletBinding()]
param(
  [string]$Tarball,
  [int]$Port = 8000,
  [switch]$SkipLoad
)

# `docker` writes progress and warnings to stderr. Under $ErrorActionPreference='Stop',
# Windows PowerShell 5.1 turns that into a terminating NativeCommandError, so this
# script keeps the preference at 'Continue' and checks $LASTEXITCODE after every
# native call instead of relying on the preference.
$ErrorActionPreference = 'Continue'

$image       = 'aivoicebench:v0.4.0-alpha.6'
$container   = 'aivoicebench-preview'
$volume      = 'aivoicebench-preview-data'
$cacheVol    = 'aivoicebench-preview-cache'
$expected    = '0.4.0-alpha.6'
$tarballName = 'aivoicebench-v0.4.0-alpha.6.tar.gz'

function Fail($message, $hint) {
  Write-Host ""
  Write-Host "错误：$message" -ForegroundColor Red
  if ($hint) { Write-Host "处理建议：$hint" -ForegroundColor Yellow }
  Write-Host ""
  exit 1
}

# --- 前置检查 ---------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Fail "未找到 docker 命令。" "请先安装并启动 Docker Desktop，确认 'docker version' 可用。"
}

& docker info --format '{{.ServerVersion}}' 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Fail "Docker 引擎未运行。" "请启动 Docker Desktop，等待其就绪后重新运行本脚本。"
}

if (-not $Tarball) {
  $Tarball = Join-Path $PSScriptRoot $tarballName
}
if (-not $SkipLoad -and -not (Test-Path $Tarball)) {
  Fail "找不到镜像包：$Tarball" "请从 GitHub Release 下载 $tarballName 并与本脚本放在同一目录，或用 -Tarball 指定路径。"
}

# 已有同名容器：不自动删除，避免破坏你可能正在使用的数据。
$existing = & docker ps -a --filter "name=^/$container$" --format '{{.Names}}' 2>$null
if ($existing) {
  $state = & docker inspect -f '{{.State.Status}}' $container 2>$null
  if ($state -eq 'running') {
    Write-Host "预览容器已在运行：http://127.0.0.1:$Port" -ForegroundColor Green
    Write-Host "如需重启：docker restart $container"
    Write-Host "如需停止：docker stop $container   （数据卷保留）"
    exit 0
  }
  Fail "已存在名为 $container 的容器（状态：$state）。" "本脚本不会自动删除容器或数据。请先确认后自行处理，例如：docker start $container  或  docker rm $container"
}

# 端口占用检查：明确提示，不自动改用端口、不删除占用方。
$inUse = $null
try {
  $inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
             Select-Object -First 1
} catch { $inUse = $null }
if ($inUse) {
  Fail "本机端口 $Port 已被占用（PID $($inUse.OwningProcess)）。" "请改用其他端口，例如：.\start-aivoicebench-preview.ps1 -Port 8001"
}
$dockerUse = & docker ps --filter "publish=$Port" --format '{{.Names}}' 2>$null
if ($dockerUse) {
  Fail "端口 $Port 已被容器占用：$dockerUse" "请改用其他端口，例如：.\start-aivoicebench-preview.ps1 -Port 8001"
}

# --- 加载镜像 ---------------------------------------------------------------
if (-not $SkipLoad) {
  Write-Host "正在加载镜像（可能需要一两分钟）..." -ForegroundColor Cyan
  $load = & docker load -i $Tarball 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Host ($load | Out-String)
    Fail "docker load 失败。" "请确认镜像包完整（可用 SHA256SUMS.txt 校验）且磁盘空间充足。"
  }
} else {
  & docker image inspect $image 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Fail "本地不存在镜像 $image，且指定了 -SkipLoad。" "去掉 -SkipLoad 让脚本从镜像包加载。"
  }
}

# --- 版本一致性校验 ---------------------------------------------------------
$reported = (& docker run --rm --entrypoint python $image -c "from aivoicebench.version import VERSION; print(VERSION)" 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
  Fail "无法读取镜像内版本。" "镜像可能不完整；请重新下载并校验 SHA256 后重试。"
}
if ($reported -ne $expected) {
  Fail "镜像内版本为 '$reported'，与本发布脚本期望的 '$expected' 不一致。" "请确认下载的是 v$expected 的镜像包，不要与其他版本混用。"
}
Write-Host "镜像版本校验通过：$reported" -ForegroundColor Green

# --- 启动 -------------------------------------------------------------------
foreach ($v in @($volume, $cacheVol)) {
  & docker volume inspect $v 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { & docker volume create $v 2>&1 | Out-Null }
}
$volumeArgs = @(
  '-v', "${volume}:/data/output",
  '-v', "${cacheVol}:/data/cache"
)

$envArgs = @(
  '-e', 'AIVOICEBENCH_OUTPUT=/data/output',
  '-e', 'AIVOICEBENCH_CACHE=/data/cache'
)
foreach ($name in 'AIVOICEBENCH_AUDIO_PUT_URL','AIVOICEBENCH_AUDIO_GET_URL','AIVOICEBENCH_AUDIO_HOST',
                  'AIVOICEBENCH_LLM_PROVIDER','AIVOICEBENCH_LLM_MODEL','ARK_API_KEY','OPENAI_API_KEY') {
  $value = [Environment]::GetEnvironmentVariable($name)
  if ($value) { $envArgs += @('-e', "$name=$value") }
}

$cloudReady = [Environment]::GetEnvironmentVariable('AIVOICEBENCH_AUDIO_PUT_URL') -and
              [Environment]::GetEnvironmentVariable('AIVOICEBENCH_AUDIO_GET_URL') -and
              [Environment]::GetEnvironmentVariable('AIVOICEBENCH_AUDIO_HOST')

Write-Host "正在启动预览容器..." -ForegroundColor Cyan
$run = & docker run -d --name $container -p "127.0.0.1:${Port}:8000" @volumeArgs @envArgs --restart no $image 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Host ($run | Out-String)
  Fail "容器启动失败。" "运行 'docker logs $container' 查看原因。"
}

# --- 健康检查 ---------------------------------------------------------------
$ok = $false
foreach ($attempt in 1..30) {
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    if ($health.version -eq $expected) { $ok = $true; break }
  } catch { Start-Sleep -Seconds 1 }
}
if (-not $ok) {
  Write-Host "容器已启动，但健康检查未在预期时间内通过。" -ForegroundColor Yellow
  Write-Host "请查看日志：docker logs $container"
  exit 2
}

Write-Host ""
Write-Host "AIVoiceBench 预览版已就绪：http://127.0.0.1:$Port （版本 $expected）" -ForegroundColor Green
if (-not $cloudReady) {
  Write-Host "注意：未检测到云 ASR 的签名 URL 环境变量。" -ForegroundColor Yellow
  Write-Host "      录音导入、原始/标准化资产、阶段账本、历史、回放、报告仍可用；" -ForegroundColor Yellow
  Write-Host "      云端转写与说话人标签不可用。完整前置条件见 Release 说明第 2 节。" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "常用操作："
Write-Host "  查看日志：docker logs -f $container"
Write-Host "  停止（保留数据）：docker stop $container"
Write-Host "  重新启动：docker start $container"
Write-Host "  预览数据卷：${volume} / ${cacheVol}（与旧版本隔离）"
Write-Host ""
