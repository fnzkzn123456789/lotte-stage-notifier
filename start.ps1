$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$candidates = @(
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\pythonw.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe')
)
$pythonCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pythonCommand) { $candidates += $pythonCommand.Source }
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        $entry = Join-Path $PSScriptRoot 'app.py'
        Start-Process -FilePath $candidate -ArgumentList @('"' + $entry + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
        exit 0
    }
}
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show('Python 3.10 or newer with Tcl/Tk is required. Install Python, then run again.', 'Lotte Notifier') | Out-Null
exit 1
