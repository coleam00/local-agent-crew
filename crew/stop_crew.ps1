# Stop crew components started by start_crew.ps1 (only the PIDs it recorded, and their children).
#   powershell -ExecutionPolicy Bypass -File stop_crew.ps1                 # everything
#   powershell -ExecutionPolicy Bypass -File stop_crew.ps1 -Only dispatcher
# To let in-flight issues finish first, create data\STOP and wait for the dispatcher to exit.
param([string]$Only = "")
$Parts = @($Only -split "," | Where-Object { $_ } | ForEach-Object { $_.Trim() })
$Data = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "data"
$PidFile = Join-Path $Data "pids.json"
if (-not (Test-Path $PidFile)) { Write-Host "no pids.json"; exit 0 }
$pids = @{}; (Get-Content $PidFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $pids[$_.Name] = $_.Value }
foreach ($name in @($pids.Keys)) {
  if ($Parts.Count -and $Parts -notcontains $name) { continue }
  & taskkill.exe /PID $pids[$name] /T /F 2>$null | Out-Null
  Write-Host "stopped $name (pid $($pids[$name]))"
  $pids.Remove($name)
}
$pids | ConvertTo-Json | Set-Content -Encoding utf8 $PidFile
