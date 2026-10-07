<#
Planes Overhead - do everything from PowerShell, no need to log in to the Pi.

  .\Update-Pi.ps1              copy the files to the Pi, redraw and push to TRMNL
  .\Update-Pi.ps1 -Image       the same, then fetch the picture and open it here
  .\Update-Pi.ps1 -RefreshMap  the same, but retry the coastline download now
  .\Update-Pi.ps1 -Install     copy the files and (re)install the services
  .\Update-Pi.ps1 -Health      how busy and how full the Pi is
  .\Update-Pi.ps1 -Logs        show the latest logger and renderer messages
  .\Update-Pi.ps1 -Stats       summary of the last 24 hours
  .\Update-Pi.ps1 -Commit "note"  copy the files and save them to the GitHub repo
  .\Update-Pi.ps1 -Run "..."   run any command on the Pi in the project folder
#>
param(
    [switch]$Image,
    [switch]$RefreshMap,
    [switch]$Install,
    [switch]$Health,
    [switch]$Logs,
    [switch]$Stats,
    [string]$Commit,
    [string]$Run
)

$Pi = 'navpi4'
$Remote = 'planes-overhead'
Set-Location $PSScriptRoot

function Invoke-Pi([string]$Command, [switch]$Terminal) {
    if ($Terminal) { ssh -t $Pi "cd $Remote && $Command" }
    else { ssh $Pi "cd $Remote && $Command" }
    if ($LASTEXITCODE -ne 0) { Write-Host "(the Pi reported an error - see above)" -ForegroundColor Yellow }
}

if ($Logs) {
    ssh $Pi "journalctl -u planelog -u planes-render -n 40 --no-pager"
    return
}
if ($Stats) { Invoke-Pi "python3 planelog.py --stats"; return }
if ($Run) { Invoke-Pi $Run -Terminal; return }

# copy the project files (scripts, config and repo files) to the Pi
$files = @(Get-ChildItem -File -Force | Where-Object {
        $_.Extension -in '.py', '.sh', '.yaml', '.md', '.ps1' -or $_.Name -eq '.gitignore' } |
    ForEach-Object { $_.Name })
if ($files.Count -eq 0) { Write-Host "No project files found in $PSScriptRoot" -ForegroundColor Red; return }
Write-Host "Copying to ${Pi}: $($files -join ', ')" -ForegroundColor Cyan
ssh $Pi "mkdir -p $Remote"
scp $files "${Pi}:$Remote/"
if ($LASTEXITCODE -ne 0) { Write-Host "Copy failed - nothing else was run." -ForegroundColor Red; return }

if ($Install) {
    Invoke-Pi "bash install.sh" -Terminal
    return
}
if ($Health) { Invoke-Pi "bash health.sh"; return }
if ($Commit) {
    $note = $Commit -replace "['`"``\\$]", ''
    Invoke-Pi "bash repo.sh --push '$note'" -Terminal
    return
}

$options = ''
if ($RefreshMap) { $options = ' --refresh-map' }
Write-Host "Drawing and pushing to TRMNL..." -ForegroundColor Cyan
Invoke-Pi "python3 planes_render.py$options"

if ($Image) {
    scp "${Pi}:$Remote/planes.png" "planes.png"
    if ($LASTEXITCODE -eq 0) { Invoke-Item "planes.png" }
}
