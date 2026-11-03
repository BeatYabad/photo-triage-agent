param([int]$RepairPid,[string]$Run)
$ErrorActionPreference='Stop'
# Serialize the requested repairs; never overlap media-processing jobs.
if(Get-Process -Id $RepairPid -ErrorAction SilentlyContinue){
    Wait-Process -Id $RepairPid -Timeout 7200 -ErrorAction Stop
}
$progress=Get-Content -LiteralPath (Join-Path $Run 'failure-repair/progress.json') -Raw | ConvertFrom-Json
if($progress.status -ne 'finished'){throw 'Failure repair did not finish; review repair was not started.'}
$env:PHOTOS_CONFIG='C:/PhotoTools/photos/photos.local.json'
& C:/PhotoTools/runtime/Scripts/python.exe -u C:/PhotoTools/photos/repair_review_moves.py --run $Run --apply
exit $LASTEXITCODE
