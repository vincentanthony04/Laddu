param(
  [string]$InstallDir = "$env:ProgramData\ProjectLaddu",
  [int]$Port = 8086
)
$ErrorActionPreference = 'Stop'
Import-Module ScheduledTasks -ErrorAction Stop
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 6)

$definitions = @(
  @{ Name='ProjectLaddu-First-Useful-Mode'; Script=(Join-Path $InstallDir 'run_first_useful_mode.ps1'); Arguments="-InstallDir `"$InstallDir`" -Port $Port -MaxMinutes 240 -CohortSize 96 -BatchSize 12"; Trigger=(New-ScheduledTaskTrigger -Daily -At '4:30 AM') },
  @{ Name='ProjectLaddu-Premarket-Learning'; Script=(Join-Path $InstallDir 'run_learning_cycle.ps1'); Arguments="-InstallDir `"$InstallDir`" -Port $Port -Cycle premarket"; Trigger=(New-ScheduledTaskTrigger -Daily -At '8:40 AM') },
  @{ Name='ProjectLaddu-PostClose-Settlement'; Script=(Join-Path $InstallDir 'run_learning_cycle.ps1'); Arguments="-InstallDir `"$InstallDir`" -Port $Port -Cycle settlement"; Trigger=(New-ScheduledTaskTrigger -Daily -At '3:35 PM') },
  @{ Name='ProjectLaddu-NSE-Official-Data'; Script=(Join-Path $InstallDir 'run_nse_official_data_cycle.ps1'); Arguments="-InstallDir `"$InstallDir`""; Trigger=(New-ScheduledTaskTrigger -Daily -At '6:00 PM') },
  @{ Name='ProjectLaddu-AI-Training'; Script=(Join-Path $InstallDir 'train_ai_model.ps1'); Arguments="-InstallDir `"$InstallDir`" -Port $Port"; Trigger=(New-ScheduledTaskTrigger -Daily -At '6:30 PM') },
  @{ Name='ProjectLaddu-Model-Governance'; Script=(Join-Path $InstallDir 'run_model_governance_cycle.ps1'); Arguments="-InstallDir `"$InstallDir`""; Trigger=(New-ScheduledTaskTrigger -Daily -At '6:50 PM') },
  @{ Name='ProjectLaddu-Brand-Assets'; Script=(Join-Path $InstallDir 'run_brand_asset_refresh.ps1'); Arguments="-InstallDir `"$InstallDir`""; Trigger=(New-ScheduledTaskTrigger -Daily -At '5:30 AM') },
  @{ Name='ProjectLaddu-Weekend-Research'; Script=(Join-Path $InstallDir 'run_learning_cycle.ps1'); Arguments="-InstallDir `"$InstallDir`" -Port $Port -Cycle weekend"; Trigger=(New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '10:00 AM') }
)
foreach($definition in $definitions){
  if(!(Test-Path $definition.Script -PathType Leaf)){ throw "Required research task script is missing: $($definition.Script)" }
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"{0}`" {1}" -f $definition.Script,$definition.Arguments)
  Register-ScheduledTask -TaskName $definition.Name -Action $action -Trigger $definition.Trigger -Principal $principal -Settings $settings -Force | Out-Null
  Enable-ScheduledTask -TaskName $definition.Name | Out-Null
}
$failed = @()
foreach($definition in $definitions){
  $task = Get-ScheduledTask -TaskName $definition.Name -ErrorAction SilentlyContinue
  if($null -eq $task -or $task.State -eq 'Disabled'){ $failed += $definition.Name }
}
if($failed.Count -gt 0){ throw ('Required Quant/AI scheduled tasks are unavailable: ' + ($failed -join ', ')) }
try { Start-ScheduledTask -TaskName 'ProjectLaddu-First-Useful-Mode' -ErrorAction Stop } catch { throw ('Unable to start the first-useful-mode bootstrap: ' + $_.Exception.Message) }
try { Start-ScheduledTask -TaskName 'ProjectLaddu-Brand-Assets' -ErrorAction Stop } catch { Write-Warning ('Brand asset refresh will retry on schedule: ' + $_.Exception.Message) }
try { Start-ScheduledTask -TaskName 'ProjectLaddu-NSE-Official-Data' -ErrorAction Stop } catch { Write-Warning ('NSE official-data acquisition will retry on schedule: ' + $_.Exception.Message) }
Write-Host ('[OK] Authoritative Quant/AI lifecycle tasks registered: ' + (($definitions | ForEach-Object { $_.Name }) -join ', ')) -ForegroundColor Green
