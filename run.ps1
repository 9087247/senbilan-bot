# PostgreSQL ishlamayotgan bo'lsa yoqadi va botni ishga tushiradi.
$pgRoot = "$env:LOCALAPPDATA\nakladnoy-bot"
$pgCtl = "$pgRoot\pgsql\bin\pg_ctl.exe"
& $pgCtl -D "$pgRoot\data" status *> $null
if ($LASTEXITCODE -ne 0) {
    & $pgCtl -D "$pgRoot\data" -l "$pgRoot\pg.log" -o "-p 5432 -c listen_addresses=localhost" -w start
}
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m nakladnoy
