param([string]$InstallDir = "$env:ProgramData\ProjectLaddu", [switch]$KeepData)
$ErrorActionPreference='Continue'
Write-Host 'Uninstalling Project Laddu...' -ForegroundColor Cyan
if(Get-Service ProjectLaddu -ErrorAction SilentlyContinue){ Stop-Service ProjectLaddu -Force; sc.exe delete ProjectLaddu | Out-Null }
if(Get-ScheduledTask -TaskName ProjectLaddu -ErrorAction SilentlyContinue){ Unregister-ScheduledTask -TaskName ProjectLaddu -Confirm:$false }
if(Get-ScheduledTask -TaskName 'ProjectLaddu-AI-Training' -ErrorAction SilentlyContinue){ Unregister-ScheduledTask -TaskName 'ProjectLaddu-AI-Training' -Confirm:$false }
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ProjectLaddu*backend*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item ([Environment]::GetFolderPath('CommonDesktopDirectory') + '\Project Laddu.url') -Force -ErrorAction SilentlyContinue
Remove-Item ([Environment]::GetFolderPath('CommonDesktopDirectory') + '\Project Laddu Status.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item ([Environment]::GetFolderPath('CommonStartMenu') + '\Programs\Project Laddu.url') -Force -ErrorAction SilentlyContinue
if(!$KeepData){ Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue; Write-Host '[OK] Removed app and data.' -ForegroundColor Green } else { Write-Host '[OK] Removed service/shortcuts, preserved data.' -ForegroundColor Green }
