# 油羊毛 · 一键启动面板
# 用法: powershell -ExecutionPolicy Bypass -File start_panel.ps1 [-Port 8790] [-Host 0.0.0.0] [-NoBrowser]
param(
  [int]$Port = 8890,
  [string]$BindHost = "127.0.0.1",
  [switch]$NoBrowser
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillRoot = Split-Path -Parent $ScriptDir
$Server    = Join-Path $ScriptDir "oil_server.py"

if (-not (Test-Path $Server)) {
  Write-Host "[油羊毛] 找不到服务脚本: $Server" -ForegroundColor Red
  exit 1
}

# ---------- 1) 定位 Python ----------
function Find-Python {
  $cands = @()
  if ($env:OIL_PY) { $cands += $env:OIL_PY }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $cands += $cmd.Source }
  $cmd = Get-Command python3 -ErrorAction SilentlyContinue
  if ($cmd) { $cands += $cmd.Source }
  $cands += (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory -ErrorAction SilentlyContinue |
             Sort-Object Name -Descending |
             ForEach-Object { Join-Path $_.FullName "python.exe" })
  foreach ($c in $cands) {
    if ($c -and (Test-Path $c)) { return $c }
  }
  return $null
}

$py = Find-Python
if (-not $py) { $py = "python" }
Write-Host "[油羊毛] Python: $py"

# ---------- 2) 依赖自检 ----------
$hasQr = (& $py -c "import qrcode, PIL; print('ok')" 2>$null) -join ""
if ($hasQr -notmatch "ok") {
  Write-Host "[油羊毛] 缺少二维码依赖，正在安装 qrcode / pillow ..."
  & $py -m pip install --disable-pip-version-check -q qrcode pillow
}

# ---------- 3) 单实例探测 ----------
function Test-Panel($p) {
  try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$p/api/health" -TimeoutSec 1
    return ($r.owner -eq "oil-coupon")
  } catch { return $false }
}

$base = $Port
for ($i = 0; $i -lt 12; $i++) {
  $p = $base + $i
  if (Test-Panel $p) {
    $url = "http://127.0.0.1:$p/"
    Write-Host "[油羊毛] 面板已在运行: $url" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
  }
}

# ---------- 4) 后台启动服务 ----------
$sargs = @($Server, "--port", "$Port", "--host", "$BindHost", "--no-browser")
Start-Process -FilePath $py -ArgumentList $sargs -WindowStyle Hidden | Out-Null

# ---------- 5) 等待就绪 ----------
$url = $null
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 500
  for ($j = 0; $j -lt 12; $j++) {
    $p = $Port + $j
    if (Test-Panel $p) { $url = "http://127.0.0.1:$p/"; break }
  }
  if ($url) { break }
}

if (-not $url) {
  Write-Host "[油羊毛] 服务启动失败，请手动执行并查看报错：" -ForegroundColor Red
  Write-Host "  $py `"$Server`" --port $Port"
  exit 1
}

Write-Host "[油羊毛] 面板已就绪: $url" -ForegroundColor Green
if ($BindHost -eq "0.0.0.0") {
  $ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
          Select-Object -ExpandProperty IPAddress)
  foreach ($ip in $ips) { Write-Host "[油羊毛] 局域网访问: http://${ip}:$($url.Split(':')[-1])" }
}
if (-not $NoBrowser) { Start-Process $url }
exit 0


