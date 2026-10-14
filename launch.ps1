param([string]$PythonPath = '', [switch]$CheckOnly)
$ErrorActionPreference = 'Continue'
$logDirectory = Join-Path $env:LOCALAPPDATA 'FolderCSVTool'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ('startup_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [Guid]::NewGuid().ToString('N') + '.txt')
$utf8 = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($logPath, 'Folder CSV Tool startup diagnostics' + [Environment]::NewLine, $utf8)
function Write-Log([string]$Text) {
    [System.IO.File]::AppendAllText($logPath, $Text + [Environment]::NewLine, $utf8)
}
Write-Log ('Script: ' + $PSScriptRoot)
Write-Log ('PowerShell: ' + $PSVersionTable.PSVersion)
$candidates = New-Object 'System.Collections.Generic.List[string]'
function Add-Candidate([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    $Path = [Environment]::ExpandEnvironmentVariables($Path.Trim().Trim('"'))
    if ($Path -like '*\Microsoft\WindowsApps\*') { return }
    if (-not $candidates.Contains($Path) -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $candidates.Add($Path)
    }
}
if ($PythonPath) {
    Add-Candidate $PythonPath
    Write-Log ('Explicit interpreter: ' + $PythonPath)
} else {
    $configuration = Join-Path $PSScriptRoot 'python_path.txt'
    if (Test-Path -LiteralPath $configuration) {
        $configured = Get-Content -LiteralPath $configuration -Encoding UTF8 | Select-Object -First 1
        Add-Candidate $configured
    }
    $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyCommand) {
        $listing = & $pyCommand.Source -0p 2>&1
        Write-Log ('py -0p: ' + ($listing -join [Environment]::NewLine))
        foreach ($line in $listing) {
            if ([string]$line -match '([A-Za-z]:\\.*python(?:w|3)?\.exe)\s*$') { Add-Candidate $Matches[1] }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) { Add-Candidate $command.Source }
    }
    foreach ($registry in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem -LiteralPath $registry -ErrorAction SilentlyContinue)) {
            $key = Get-Item -LiteralPath (Join-Path $version.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($key) {
                Add-Candidate $key.GetValue('ExecutablePath')
                $install = $key.GetValue('')
                if ($install) { Add-Candidate (Join-Path $install 'python.exe') }
            }
        }
    }
    foreach ($pattern in @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe'), (Join-Path $env:ProgramFiles 'Python*\python.exe'))) {
        foreach ($file in @(Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue)) { Add-Candidate $file.FullName }
    }
}
$selected = $null
$probe = 'import sys, tkinter; assert sys.version_info.major == 3; r=tkinter.Tk(); r.withdraw(); r.destroy(); print(sys.executable); print(sys.version)'
foreach ($candidate in $candidates) {
    Write-Host ('Pythonを確認：' + $candidate)
    Write-Log ('Candidate: ' + $candidate)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $candidate
    $process.StartInfo.Arguments = '-c "' + $probe + '"'
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        [void]$process.Start()
        if (-not $process.WaitForExit(12000)) {
            $process.Kill()
            Write-Log 'Probe timed out (12 seconds).'
            continue
        }
        Write-Log $process.StandardOutput.ReadToEnd()
        Write-Log $process.StandardError.ReadToEnd()
        Write-Log ('Exit code: ' + $process.ExitCode)
        if ($process.ExitCode -eq 0) { $selected = $candidate; break }
    } catch { Write-Log $_.Exception.Message }
    finally { $process.Dispose() }
}
if (-not $selected) {
    Write-Host '画面を表示できるPython 3（tkinter / Tcl-Tk付き）が見つかりませんでした。'
    Write-Host '通常のWindows用PythonをTcl/Tk付きで導入するか、python_path.txtにpython.exeのフルパスを1行で指定してください。'
    Write-Host '診断ログには、検出したPythonと実際のエラーを記録しています。'
    Write-Host ('診断ログ：' + $logPath)
    exit 2
}
Write-Host ('使用Python：' + $selected)
Write-Host ('診断ログ：' + $logPath)
if ($CheckOnly) { exit 0 }
try {
    & $selected (Join-Path $PSScriptRoot 'folder_csv.py') 2>&1 | ForEach-Object {
        Write-Log ([string]$_)
        Write-Host ([string]$_)
    }
    $applicationExit = $LASTEXITCODE
    Write-Log ('Application exit code: ' + $applicationExit)
    if ($applicationExit -ne 0) { Write-Host ('起動または実行に失敗しました。診断ログ：' + $logPath) }
    exit $applicationExit
} catch {
    Write-Log $_.Exception.Message
    Write-Host ('起動に失敗しました。診断ログ：' + $logPath)
    exit 1
}
