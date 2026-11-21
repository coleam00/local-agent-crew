# Start the whole local crew as detached, hidden processes that survive the terminal (and any
# agent session) that started them. Logs: data\logs\<component>.log  PIDs: data\pids.json
#
#   powershell -ExecutionPolicy Bypass -File start_crew.ps1 -Agents 4 -Model flash-next
#   powershell -ExecutionPolicy Bypass -File start_crew.ps1 -Only telemetry,dashboard
#
# Stop everything with stop_crew.ps1 (it only stops the PIDs this script recorded).
param(
  [int]$Agents = 4,
  [int]$PerSlot = 131072,
  [string]$Model = "flash-next",
  [string]$Only = "model,telemetry,dashboard,mirror,sync,dispatcher",
  [int]$Seed = 0
)
$ErrorActionPreference = "Stop"
$Parts = @($Only -split "," | ForEach-Object { $_.Trim() })   # -File passes "a,b" as one string; $Only stays [string]
$Crew   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Data   = Join-Path (Split-Path -Parent $Crew) "data"
$Logs   = Join-Path $Data "logs"
New-Item -ItemType Directory -Force $Logs | Out-Null
$Py     = "C:\Users\ASUS\AppData\Roaming\uv\python\cpython-3.13-windows-aarch64-none\python.exe"
$Bash   = "C:\Program Files\Git\bin\bash.exe"
$Bun    = "C:\Users\ASUS\AppData\Local\Microsoft\WinGet\Packages\Oven-sh.Bun_Microsoft.Winget.Source_8wekyb3d8bbwe\bun-windows-aarch64"
$Llama  = "C:\Users\ASUS\proart-crew\bin\llama"
$env:Path = "$Bun;$Llama;C:\Program Files\GitHub CLI;C:\Program Files\Git\cmd;$env:Path"
$env:CREW_BUN = Join-Path $Bun "bun.exe"
$env:PYTHONUNBUFFERED = "1"
$PidFile = Join-Path $Data "pids.json"
$pids = @{}
if (Test-Path $PidFile) { (Get-Content $PidFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $pids[$_.Name] = $_.Value } }

function Start-Component($name, $exe, $argList, $workDir) {
  if ($pids[$name] -and (Get-Process -Id $pids[$name] -ErrorAction SilentlyContinue)) {
    Write-Host "$name already running (pid $($pids[$name]))"; return
  }
  $log = Join-Path $Logs "$name.log"
  $p = Start-Process -FilePath $exe -ArgumentList $argList -WorkingDirectory $workDir -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru
  $pids[$name] = $p.Id
  Write-Host "started $name (pid $($p.Id)) -> $log"
}

if ($Parts -contains "model") {
  $env:AGENTS = "$Agents"; $env:PER_SLOT = "$PerSlot"; $env:MODEL = $Model
  Start-Component "model" $Bash @("serve_model.sh") $Crew
  Write-Host "waiting for the model to load..."
  for ($i = 0; $i -lt 180; $i++) {
    try { if ((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9931/health -TimeoutSec 3).Content -match "ok") { break } } catch {}
    Start-Sleep 5
  }
}
if ($Parts -contains "telemetry")  { Start-Component "telemetry"  $Py @("telemetry.py", "--every", "10") $Crew }
if ($Parts -contains "dashboard")  { Start-Component "dashboard"  $Py @("dashboard.py", "--port", "8765") $Crew }
if ($Parts -contains "mirror")     {
  $margs = @("mirror_issues.py", "--watch", "300"); if ($Seed -gt 0) { $margs += @("--seed", "$Seed") }
  Start-Component "mirror" $Py $margs $Crew
}
if ($Parts -contains "sync")       { Start-Component "sync" $Bash @("sync_fork.sh") $Crew }
if ($Parts -contains "dispatcher") { Start-Component "dispatcher" $Py @("dispatcher.py", "--agents", "$Agents") $Crew }

$pids | ConvertTo-Json | Set-Content -Encoding utf8 $PidFile
