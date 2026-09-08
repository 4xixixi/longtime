param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $SupervisorArguments
)

$ErrorActionPreference = 'Stop'
$controller = Join-Path $PSScriptRoot 'supervisor_ctl.py'
$candidates = @()
$previousPythonUtf8 = $env:PYTHONUTF8
$previousPythonIoEncoding = $env:PYTHONIOENCODING
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

foreach ($name in @('py', 'python3', 'python')) {
    foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
        $prefix = if ($name -eq 'py') { @('-3') } else { @() }
        $candidates += [pscustomobject]@{
            Path = $command.Source
            Prefix = $prefix
        }
    }
}

foreach ($candidate in $candidates) {
    & $candidate.Path @($candidate.Prefix) -c 'import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)' 2>$null
    if ($LASTEXITCODE -ne 0) {
        continue
    }
    & $candidate.Path @($candidate.Prefix) $controller @SupervisorArguments
    $exitCode = $LASTEXITCODE
    $env:PYTHONUTF8 = $previousPythonUtf8
    $env:PYTHONIOENCODING = $previousPythonIoEncoding
    exit $exitCode
}

$env:PYTHONUTF8 = $previousPythonUtf8
$env:PYTHONIOENCODING = $previousPythonIoEncoding
throw 'No working Python 3 interpreter was found for supervisor_ctl.py.'
