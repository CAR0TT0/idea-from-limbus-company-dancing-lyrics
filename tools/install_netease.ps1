param(
    [string]$ClientDirectory = 'C:\Program Files\NetEase\CloudMusic',
    [string]$ProfileDirectory = 'C:\betterncm',
    [string]$FrameworkDll = '',
    [switch]$Experimental,
    [switch]$Restore
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskEngine = Join-Path $taskRoot 'dist\FloatingLyrics\FloatingLyrics.exe'
if (-not (Test-Path -LiteralPath $taskEngine)) { throw '请先运行 build.ps1 构建完整成品。' }
$taskAction = if ($Restore) { '--restore-netease' } else { '--install-netease' }
$taskArguments = @($taskAction, '--client-directory', $ClientDirectory, '--profile-directory', $ProfileDirectory)
if ($FrameworkDll) { $taskArguments += @('--framework-dll', $FrameworkDll) }
if ($Experimental) { $taskArguments += '--experimental-netease' }
& $taskEngine @taskArguments
if ($LASTEXITCODE -ne 0) { throw '联动窗口退出异常，请检查界面提示。' }
