[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ProductionPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{7,40}$')]
    [string]$ExpectedCommit,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$WorktreeRoot = 'C:\PrimeBot2-DeployWorktrees',

    [Parameter()]
    [switch]$StartPausedDryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ExpectedOriginUrl = 'https://github.com/SamuelEto18/primebot-2.git'
$ScheduledTaskName = 'PrimeBot AutoStart'

try {
    $toolingRepositoryPath = [System.IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
    $expectedToolingRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $toolingRepositoryPath 'scripts')
    ).TrimEnd('\')
    $actualToolingRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')

    if (-not $actualToolingRoot.Equals(
        $expectedToolingRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Deploy-PrimeBot2.ps1 must be run from the scripts directory of RepositoryPath.'
    }
}
catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}

# Tooling is loaded only from the main deployment repository. The requested
# application commit is checked out only in a linked detached worktree.
Import-Module (Join-Path $PSScriptRoot 'PrimeBot2.Deployment.psm1') -Force

$DeploymentStateName = '.primebot2-deployment-state.json'
$backupDirectory = $null
$backupRecords = @()
$changesStarted = $false
$runtimeExisted = $false
$stateManifestExisted = $false
$runtimeBackupPath = $null
$stateManifestBackupPath = $null
$manifest = $null
$productionFullPath = $null
$repositoryFullPath = $null
$runtimePath = $null
$stateManifestPath = $null
$sourceCommit = $null
$filesCopied = 0
$filesRemoved = 0
$testResultText = 'Not run'
$runtimeSafetyEnforced = $false
$sourceWorktreePath = $null
$worktreeRootFullPath = $null
$repositoryInitialBranch = $null
$repositoryInitialHead = $null
$deploymentSucceeded = $false
$failureMessage = $null


function Read-JsonFileStrict {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Description
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description not found: $Path"
    }

    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "$Description is not valid JSON: $Path | $($_.Exception.Message)"
    }
}


function Write-JsonFileAtomic {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $directory = Split-Path -Parent $Path

    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $temporaryPath = Join-Path $directory ('.primebot2-' + [guid]::NewGuid().ToString('N') + '.tmp')

    try {
        $Value | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}


function ConvertTo-SafeRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = $Path.Replace('\', '/').Trim('/')

    if ([string]::IsNullOrWhiteSpace($normalized)) {
        throw 'Empty deployment-relative path is not allowed.'
    }

    if ([System.IO.Path]::IsPathRooted($Path) -or $normalized.Contains(':')) {
        throw "Rooted deployment path is not allowed: $Path"
    }

    foreach ($segment in $normalized.Split('/')) {
        if ($segment -eq '..' -or $segment -eq '.') {
            throw "Unsafe deployment-relative path: $Path"
        }
    }

    return $normalized
}


function Get-SafePathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $safeRelativePath = ConvertTo-SafeRelativePath $RelativePath
    $rootFullPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $candidate = [System.IO.Path]::GetFullPath(
        (Join-Path $rootFullPath $safeRelativePath.Replace('/', '\'))
    )
    $rootPrefix = $rootFullPath + '\'

    if (-not $candidate.StartsWith(
        $rootPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Path escapes deployment root: $RelativePath"
    }

    return $candidate
}


function Assert-NoReparsePointsUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $safeRelativePath = ConvertTo-SafeRelativePath $RelativePath
    $rootFullPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $rootItem = Get-Item -LiteralPath $rootFullPath -Force -ErrorAction Stop

    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Deployment root must not be a reparse point: $rootFullPath"
    }

    $current = $rootFullPath

    foreach ($segment in $safeRelativePath.Split('/')) {
        $current = Join-Path $current $segment

        if (-not (Test-Path -LiteralPath $current)) {
            break
        }

        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop

        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing deployment through a reparse point: $current"
        }
    }
}


function Test-ProtectedRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][object]$Manifest
    )

    $normalized = (ConvertTo-SafeRelativePath $RelativePath).ToLowerInvariant()
    $leaf = [System.IO.Path]::GetFileName($normalized)

    foreach ($root in @($Manifest.alwaysProtectedRoots)) {
        $protectedRoot = ([string]$root).Replace('\', '/').Trim('/').ToLowerInvariant()

        if ($normalized -eq $protectedRoot -or $normalized.StartsWith($protectedRoot + '/')) {
            return $true
        }
    }

    foreach ($name in @($Manifest.alwaysProtectedNames)) {
        if ($leaf -ieq [string]$name) {
            return $true
        }
    }

    foreach ($glob in @($Manifest.alwaysProtectedGlobs)) {
        if ($leaf -like [string]$glob) {
            return $true
        }
    }

    return $false
}


function Test-ManagedRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][object]$Manifest
    )

    $normalized = (ConvertTo-SafeRelativePath $RelativePath).ToLowerInvariant()

    foreach ($file in @($Manifest.managedRootFiles)) {
        if ($normalized -eq ([string]$file).Replace('\', '/').Trim('/').ToLowerInvariant()) {
            return $true
        }
    }

    foreach ($root in @($Manifest.managedRoots)) {
        $managedRoot = ([string]$root).Replace('\', '/').Trim('/').ToLowerInvariant()

        if ($normalized.StartsWith($managedRoot + '/')) {
            return $true
        }
    }

    return $false
}


function Invoke-GitCapture {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference

    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git.exe -C $Repository @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($exitCode -ne 0) {
        $details = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "Git command failed (git $($Arguments -join ' ')) with exit code $exitCode. $details"
    }

    return @($output | ForEach-Object { $_.ToString() })
}


function Assert-SourceIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][object]$Manifest
    )

    foreach ($check in @($Manifest.sourceIdentityChecks)) {
        $relativePath = ConvertTo-SafeRelativePath ([string]$check.path)
        Assert-NoReparsePointsUnderRoot -Root $Repository -RelativePath $relativePath
        $sourcePath = Get-SafePathUnderRoot -Root $Repository -RelativePath $relativePath

        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Production identity check '$($check.name)' is missing source file $relativePath."
        }

        $content = Get-Content -LiteralPath $sourcePath -Raw -Encoding UTF8

        if (-not [regex]::IsMatch($content, [string]$check.pattern)) {
            throw "Production identity mismatch: $($check.name)."
        }
    }
}


function Assert-ProductionMt5ServerIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][object]$Manifest
    )

    $identity = $Manifest.productionMt5Identity

    if (
        [string]$identity.implementation -cne 'core/mt5_status.py:get_server' -or
        [string]$identity.field -cne 'MetaTrader5.account_info().server' -or
        [string]$identity.expected -cne 'PUPrime-Live 6'
    ) {
        throw 'Deployment manifest MT5 server identity is inconsistent with the approved production identity.'
    }

    $probe = @'
import json
import sys

try:
    import MetaTrader5 as mt5
    if not mt5.initialize():
        sys.exit(20)
    account = mt5.account_info()
    server = getattr(account, 'server', None) if account is not None else None
    print(json.dumps({'server': server}))
finally:
    try:
        mt5.shutdown()
    except Exception:
        pass
'@
    $previousNativeErrorPreference = $ErrorActionPreference

    try {
        $ErrorActionPreference = 'Continue'
        $probeOutput = @(& $PythonExecutable -c $probe 2>&1)
        $probeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousNativeErrorPreference
    }

    if ($probeExitCode -ne 0) {
        throw "Unable to read the authoritative MT5 account server identity (probe exit code $probeExitCode)."
    }

    $jsonLine = @($probeOutput | ForEach-Object { $_.ToString() }) |
        Where-Object { $_.Trim().StartsWith('{') } |
        Select-Object -Last 1

    if ([string]::IsNullOrWhiteSpace($jsonLine)) {
        throw 'Authoritative MT5 server probe returned no valid identity result.'
    }

    try {
        $probeResult = $jsonLine | ConvertFrom-Json
    }
    catch {
        throw 'Authoritative MT5 server probe returned an invalid identity result.'
    }

    if ([string]$probeResult.server -cne [string]$identity.expected) {
        throw 'Production identity mismatch: MT5 server.'
    }
}


function Test-FilesEqual {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    if (-not (Test-Path -LiteralPath $Left -PathType Leaf)) {
        return $false
    }

    if (-not (Test-Path -LiteralPath $Right -PathType Leaf)) {
        return $false
    }

    return (Get-FileHash -LiteralPath $Left -Algorithm SHA256).Hash -eq
        (Get-FileHash -LiteralPath $Right -Algorithm SHA256).Hash
}


function Set-PausedDryRunRuntime {
    param([Parameter(Mandatory = $true)][string]$Path)

    $state = $null

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $state = Read-JsonFileStrict -Path $Path -Description 'Runtime state'
    }
    else {
        $state = [pscustomobject]@{}
    }

    if ($state -isnot [psobject]) {
        throw 'Runtime state must be a JSON object.'
    }

    if ($state.PSObject.Properties.Name -contains 'paused') {
        $state.paused = $true
    }
    else {
        $state | Add-Member -NotePropertyName paused -NotePropertyValue $true
    }

    if ($state.PSObject.Properties.Name -contains 'auto_execute') {
        $state.auto_execute = $false
    }
    else {
        $state | Add-Member -NotePropertyName auto_execute -NotePropertyValue $false
    }

    Write-JsonFileAtomic -Value $state -Path $Path
}


function Assert-PausedDryRunRuntime {
    param([Parameter(Mandatory = $true)][string]$Path)

    $state = Read-JsonFileStrict -Path $Path -Description 'Runtime state'

    if ($state.paused -ne $true -or $state.auto_execute -ne $false) {
        throw 'Runtime safety verification failed: paused must be true and auto_execute must be false.'
    }
}


function Copy-BackupFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $backupPath = Get-SafePathUnderRoot -Root $BackupRoot -RelativePath $RelativePath
    $backupParent = Split-Path -Parent $backupPath

    if (-not (Test-Path -LiteralPath $backupParent -PathType Container)) {
        New-Item -ItemType Directory -Path $backupParent -Force | Out-Null
    }

    Copy-Item -LiteralPath $Source -Destination $backupPath -Force
    return $backupPath
}


function Restore-DeploymentBackup {
    param(
        [Parameter(Mandatory = $true)][object[]]$Records,
        [Parameter(Mandatory = $true)][string]$ProductionRoot,
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][bool]$RuntimePreviouslyExisted,
        [Parameter(Mandatory = $true)][string]$RuntimeStatePath,
        [Parameter()][string]$RuntimeStateBackup,
        [Parameter(Mandatory = $true)][bool]$StatePreviouslyExisted,
        [Parameter(Mandatory = $true)][string]$DeploymentStatePath,
        [Parameter()][string]$DeploymentStateBackup
    )

    foreach ($record in @($Records)) {
        $relativePath = ConvertTo-SafeRelativePath ([string]$record.relativePath)

        if (Test-ProtectedRelativePath -RelativePath $relativePath -Manifest $Manifest) {
            throw "Rollback record unexpectedly targets protected path: $relativePath"
        }

        $destination = Get-SafePathUnderRoot -Root $ProductionRoot -RelativePath $relativePath

        if ([bool]$record.existed) {
            $backup = [string]$record.backupPath
            $destinationParent = Split-Path -Parent $destination

            if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
                New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
            }

            Copy-Item -LiteralPath $backup -Destination $destination -Force
        }
        elseif (Test-Path -LiteralPath $destination -PathType Leaf) {
            Remove-Item -LiteralPath $destination -Force
        }
    }

    if ($StatePreviouslyExisted) {
        Copy-Item -LiteralPath $DeploymentStateBackup -Destination $DeploymentStatePath -Force
    }
    elseif (Test-Path -LiteralPath $DeploymentStatePath -PathType Leaf) {
        Remove-Item -LiteralPath $DeploymentStatePath -Force
    }

    if ($RuntimePreviouslyExisted) {
        $runtimeParent = Split-Path -Parent $RuntimeStatePath

        if (-not (Test-Path -LiteralPath $runtimeParent -PathType Container)) {
            New-Item -ItemType Directory -Path $runtimeParent -Force | Out-Null
        }

        Copy-Item -LiteralPath $RuntimeStateBackup -Destination $RuntimeStatePath -Force
    }
    elseif (Test-Path -LiteralPath $RuntimeStatePath -PathType Leaf) {
        Remove-Item -LiteralPath $RuntimeStatePath -Force
    }

    Set-PausedDryRunRuntime -Path $RuntimeStatePath
    Assert-PausedDryRunRuntime -Path $RuntimeStatePath
}


try {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue

    if ($null -eq $git) {
        throw 'Git is required for deployment but is not installed or not on PATH.'
    }

    $manifestPath = Join-Path $PSScriptRoot 'PrimeBot2.DeploymentManifest.json'
    $manifest = Read-JsonFileStrict -Path $manifestPath -Description 'Deployment manifest'

    if ([int]$manifest.schemaVersion -ne 1) {
        throw "Unsupported deployment manifest schema: $($manifest.schemaVersion)"
    }

    if ([int]$manifest.minimumTestCount -lt 221) {
        throw 'Deployment manifest may not require fewer than 221 tests.'
    }

    if ([string]$manifest.expectedOriginUrl -cne $ExpectedOriginUrl) {
        throw "Deployment manifest origin must be exactly '$ExpectedOriginUrl'."
    }

    $repositoryFullPath = [System.IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
    $productionFullPath = [System.IO.Path]::GetFullPath($ProductionPath).TrimEnd('\')

    if (-not (Test-Path -LiteralPath $repositoryFullPath -PathType Container)) {
        throw "Repository path does not exist: $repositoryFullPath"
    }

    if (-not (Test-Path -LiteralPath (Join-Path $repositoryFullPath '.git') -PathType Container)) {
        throw "Repository path is not a Git working tree: $repositoryFullPath"
    }

    if (-not (Test-Path -LiteralPath $productionFullPath -PathType Container)) {
        throw "Production path does not exist: $productionFullPath"
    }

    if ($repositoryFullPath.Equals(
        $productionFullPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Repository and production paths must be different.'
    }

    $repositoryPrefix = $repositoryFullPath + '\'
    $productionPrefix = $productionFullPath + '\'

    if (
        $repositoryFullPath.StartsWith($productionPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        $productionFullPath.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw 'Repository and production paths must not contain one another.'
    }

    $repositoryInitialBranch = (
        Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
            'rev-parse', '--abbrev-ref', 'HEAD'
        )
    ) -join ''
    $repositoryInitialBranch = $repositoryInitialBranch.Trim()
    $repositoryInitialHead = (
        Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
            'rev-parse', 'HEAD'
        )
    ) -join ''
    $repositoryInitialHead = $repositoryInitialHead.Trim().ToLowerInvariant()

    $dirtyOutput = @(
        Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
            'status', '--porcelain', '--untracked-files=all'
        )
    )

    if ($dirtyOutput.Count -ne 0) {
        throw "Refusing deployment from a dirty repository. Status: $($dirtyOutput -join '; ')"
    }

    $originOutput = Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
        'config', '--get', 'remote.origin.url'
    )
    $origin = ($originOutput -join '').Trim()

    if ($origin -cne $ExpectedOriginUrl) {
        throw "Wrong origin URL. Expected '$ExpectedOriginUrl' but found '$origin'."
    }

    Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
        'fetch', '--prune', 'origin'
    ) | Out-Null

    Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
        'rev-parse', '--verify', 'refs/remotes/origin/main^{commit}'
    ) | Out-Null

    $resolvedCommitOutput = Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
        'rev-parse', '--verify', "$ExpectedCommit`^{commit}"
    )
    $resolvedCommit = ($resolvedCommitOutput -join '').Trim().ToLowerInvariant()

    if ($resolvedCommit -notmatch '^[0-9a-f]{40}$') {
        throw 'Requested commit did not resolve to one exact full commit.'
    }

    try {
        Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
            'merge-base', '--is-ancestor', $resolvedCommit, 'refs/remotes/origin/main'
        ) | Out-Null
    }
    catch {
        throw "Requested commit is not reachable from origin/main: $resolvedCommit"
    }

    $sourceCommit = $resolvedCommit
    $worktreeRootFullPath = [System.IO.Path]::GetFullPath($WorktreeRoot).TrimEnd('\')
    $worktreeName = '{0}-{1}-{2}' -f (
        $sourceCommit.Substring(0, 12),
        (Get-Date).ToString('yyyyMMdd-HHmmss'),
        [guid]::NewGuid().ToString('N').Substring(0, 8)
    )
    $sourceWorktreePath = [System.IO.Path]::GetFullPath(
        (Join-Path $worktreeRootFullPath $worktreeName)
    ).TrimEnd('\')

    foreach ($protectedRoot in @($repositoryFullPath, $productionFullPath)) {
        $protectedPrefix = $protectedRoot + '\'
        $worktreePrefix = $sourceWorktreePath + '\'

        if (
            $sourceWorktreePath.Equals($protectedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
            $sourceWorktreePath.StartsWith($protectedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
            $protectedRoot.StartsWith($worktreePrefix, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "Temporary worktree must be outside repository and production paths: $sourceWorktreePath"
        }
    }

    New-Item -ItemType Directory -Path $worktreeRootFullPath -Force | Out-Null
    Assert-NoReparsePointsUnderRoot `
        -Root $worktreeRootFullPath `
        -RelativePath $worktreeName

    Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
        'worktree', 'add', '--detach', '--', $sourceWorktreePath, $sourceCommit
    ) | Out-Null

    $worktreeHead = (
        Invoke-GitCapture -Repository $sourceWorktreePath -Arguments @(
            'rev-parse', 'HEAD'
        )
    ) -join ''
    $worktreeHead = $worktreeHead.Trim().ToLowerInvariant()
    $worktreeBranch = (
        Invoke-GitCapture -Repository $sourceWorktreePath -Arguments @(
            'rev-parse', '--abbrev-ref', 'HEAD'
        )
    ) -join ''
    $worktreeBranch = $worktreeBranch.Trim()

    if ($worktreeHead -cne $sourceCommit -or $worktreeBranch -cne 'HEAD') {
        throw "Detached worktree commit mismatch. Expected $sourceCommit but found $worktreeHead."
    }

    $pythonFullPath = [System.IO.Path]::GetFullPath(
        (Join-Path $productionFullPath '.venv\Scripts\python.exe')
    )

    if (-not (Test-Path -LiteralPath $pythonFullPath -PathType Leaf)) {
        throw "Python executable not found: $pythonFullPath"
    }

    Assert-ProductionMt5ServerIdentity `
        -PythonExecutable $pythonFullPath `
        -Manifest $manifest
    Assert-SourceIdentity -Repository $sourceWorktreePath -Manifest $manifest

    $trackedOutput = Invoke-GitCapture -Repository $sourceWorktreePath -Arguments @(
        'ls-tree', '-r', '--name-only', 'HEAD'
    )
    $managedFiles = @(
        $trackedOutput |
            ForEach-Object { ConvertTo-SafeRelativePath $_ } |
            Where-Object {
                (Test-ManagedRelativePath -RelativePath $_ -Manifest $manifest) -and
                -not (Test-ProtectedRelativePath -RelativePath $_ -Manifest $manifest)
            } |
            Sort-Object -Unique
    )

    foreach ($requiredFile in @($manifest.requiredManagedFiles)) {
        $required = ConvertTo-SafeRelativePath ([string]$requiredFile)

        if ($managedFiles -notcontains $required) {
            throw "Required managed file is absent from the approved commit: $required"
        }
    }

    $stateManifestPath = Join-Path $productionFullPath $DeploymentStateName
    Assert-NoReparsePointsUnderRoot `
        -Root $productionFullPath `
        -RelativePath $DeploymentStateName
    $oldManagedFiles = @()

    if (Test-Path -LiteralPath $stateManifestPath -PathType Leaf) {
        $oldState = Read-JsonFileStrict -Path $stateManifestPath -Description 'Previous deployment state'

        if ($oldState.PSObject.Properties.Name -notcontains 'managedFiles') {
            throw 'Previous deployment state has no managedFiles list.'
        }

        $oldManagedFiles = @(
            @($oldState.managedFiles) |
                ForEach-Object { ConvertTo-SafeRelativePath ([string]$_) } |
                Sort-Object -Unique
        )

        foreach ($oldManagedFile in $oldManagedFiles) {
            if (
                -not (Test-ProtectedRelativePath -RelativePath $oldManagedFile -Manifest $manifest) -and
                -not (Test-ManagedRelativePath -RelativePath $oldManagedFile -Manifest $manifest)
            ) {
                throw "Previous deployment state contains an unmanaged path: $oldManagedFile"
            }
        }
    }

    $filesToCopy = @()

    foreach ($relativePath in $managedFiles) {
        Assert-NoReparsePointsUnderRoot -Root $sourceWorktreePath -RelativePath $relativePath
        Assert-NoReparsePointsUnderRoot -Root $productionFullPath -RelativePath $relativePath
        $sourcePath = Get-SafePathUnderRoot -Root $sourceWorktreePath -RelativePath $relativePath
        $destinationPath = Get-SafePathUnderRoot -Root $productionFullPath -RelativePath $relativePath

        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Tracked source file is unavailable: $relativePath"
        }

        if (
            (Test-Path -LiteralPath $destinationPath) -and
            -not (Test-Path -LiteralPath $destinationPath -PathType Leaf)
        ) {
            throw "Managed production destination is not a file: $relativePath"
        }

        if (-not (Test-FilesEqual -Left $sourcePath -Right $destinationPath)) {
            $filesToCopy += $relativePath
        }
    }

    $filesToRemove = @()

    foreach ($relativePath in $oldManagedFiles) {
        if ($managedFiles -contains $relativePath) {
            continue
        }

        if (Test-ProtectedRelativePath -RelativePath $relativePath -Manifest $manifest) {
            Write-Warning "Protected path remains untouched despite old managed state: $relativePath"
            continue
        }

        Assert-NoReparsePointsUnderRoot -Root $productionFullPath -RelativePath $relativePath
        $candidate = Get-SafePathUnderRoot -Root $productionFullPath -RelativePath $relativePath

        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $filesToRemove += $relativePath
        }
    }

    $verificationRoot = Join-Path $sourceWorktreePath '.primebot2-verification'
    $testTempRoot = Join-Path $verificationRoot 'temp'
    New-Item -ItemType Directory -Path $testTempRoot -Force | Out-Null
    $oldPycachePrefix = $env:PYTHONPYCACHEPREFIX
    $oldDontWriteBytecode = $env:PYTHONDONTWRITEBYTECODE
    $oldPrimeBotLogFile = $env:PRIMEBOT_LOG_FILE
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    $env:PYTHONPYCACHEPREFIX = $null
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PRIMEBOT_LOG_FILE = Join-Path $verificationRoot 'primebot-tests.log'
    $env:TEMP = $testTempRoot
    $env:TMP = $testTempRoot

    try {
        Push-Location $sourceWorktreePath

        try {
            $compileTargets = @('config.py', 'control_bot.py', 'main.py', 'core', 'tests') |
                Where-Object { Test-Path -LiteralPath (Join-Path $sourceWorktreePath $_) }
            $previousNativeErrorPreference = $ErrorActionPreference

            try {
                $ErrorActionPreference = 'Continue'
                $compileOutput = @(& $pythonFullPath -m compileall -q @compileTargets 2>&1)
                $compileExitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousNativeErrorPreference
            }

            if ($compileExitCode -ne 0) {
                $compileDetails = ($compileOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
                throw "Python compilation failed with exit code $compileExitCode. $compileDetails"
            }

            try {
                $ErrorActionPreference = 'Continue'
                $testOutput = @(& $pythonFullPath -m unittest discover -s tests -v 2>&1)
                $testExitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousNativeErrorPreference
            }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        $env:PYTHONPYCACHEPREFIX = $oldPycachePrefix
        $env:PYTHONDONTWRITEBYTECODE = $oldDontWriteBytecode
        $env:PRIMEBOT_LOG_FILE = $oldPrimeBotLogFile
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }

    $testText = ($testOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    $testCountMatches = [regex]::Matches($testText, 'Ran\s+(\d+)\s+tests?')

    if ($testExitCode -ne 0) {
        $tail = @($testOutput | Select-Object -Last 60) -join [Environment]::NewLine
        throw "Unit tests failed with exit code $testExitCode. $tail"
    }

    if ($testCountMatches.Count -eq 0) {
        throw 'Unit test output did not contain a test count.'
    }

    $testCount = [int]$testCountMatches[$testCountMatches.Count - 1].Groups[1].Value

    if ($testCount -lt [int]$manifest.minimumTestCount) {
        throw "Only $testCount tests ran; at least $($manifest.minimumTestCount) are required."
    }

    $testResultText = "$testCount tests passed"

    $archiveRoot = Join-Path $productionFullPath 'deployment-archives'
    Assert-NoReparsePointsUnderRoot `
        -Root $productionFullPath `
        -RelativePath 'deployment-archives'
    $archiveName = '{0}-{1}-{2}' -f (
        (Get-Date).ToString('yyyyMMdd-HHmmss'),
        $sourceCommit.Substring(0, 12),
        [guid]::NewGuid().ToString('N').Substring(0, 8)
    )
    $backupDirectory = Join-Path $archiveRoot $archiveName
    $backupFilesRoot = Join-Path $backupDirectory 'files'
    New-Item -ItemType Directory -Path $backupFilesRoot -Force | Out-Null

    $runtimePath = Join-Path $productionFullPath 'data\runtime.json'
    Assert-NoReparsePointsUnderRoot `
        -Root $productionFullPath `
        -RelativePath 'data/runtime.json'
    $runtimeExisted = Test-Path -LiteralPath $runtimePath -PathType Leaf

    if ($runtimeExisted) {
        $runtimeBackupPath = Join-Path $backupDirectory 'protected\data\runtime.json'
        $runtimeBackupParent = Split-Path -Parent $runtimeBackupPath
        New-Item -ItemType Directory -Path $runtimeBackupParent -Force | Out-Null
        Copy-Item -LiteralPath $runtimePath -Destination $runtimeBackupPath -Force
    }

    $stateManifestExisted = Test-Path -LiteralPath $stateManifestPath -PathType Leaf

    if ($stateManifestExisted) {
        $stateManifestBackupPath = Join-Path $backupDirectory 'metadata\previous-deployment-state.json'
        $stateBackupParent = Split-Path -Parent $stateManifestBackupPath
        New-Item -ItemType Directory -Path $stateBackupParent -Force | Out-Null
        Copy-Item -LiteralPath $stateManifestPath -Destination $stateManifestBackupPath -Force
    }

    $recordList = New-Object System.Collections.Generic.List[object]

    foreach ($relativePath in $filesToCopy) {
        $destinationPath = Get-SafePathUnderRoot -Root $productionFullPath -RelativePath $relativePath
        $existed = Test-Path -LiteralPath $destinationPath -PathType Leaf
        $backupPath = $null

        if ($existed) {
            $backupPath = Copy-BackupFile `
                -Source $destinationPath `
                -BackupRoot $backupFilesRoot `
                -RelativePath $relativePath
        }

        $recordList.Add([pscustomobject]@{
            relativePath = $relativePath
            action = $(if ($existed) { 'replace' } else { 'create' })
            existed = [bool]$existed
            backupPath = $backupPath
        })
    }

    foreach ($relativePath in $filesToRemove) {
        $destinationPath = Get-SafePathUnderRoot -Root $productionFullPath -RelativePath $relativePath
        $backupPath = Copy-BackupFile `
            -Source $destinationPath `
            -BackupRoot $backupFilesRoot `
            -RelativePath $relativePath
        $recordList.Add([pscustomobject]@{
            relativePath = $relativePath
            action = 'remove'
            existed = $true
            backupPath = $backupPath
        })
    }

    $backupRecords = $recordList.ToArray()
    $backupIndex = [ordered]@{
        schemaVersion = 1
        sourceCommit = $sourceCommit
        createdAt = (Get-Date).ToString('o')
        productionPath = $productionFullPath
        runtimeExisted = $runtimeExisted
        deploymentStateExisted = $stateManifestExisted
        records = $backupRecords
    }
    Write-JsonFileAtomic -Value $backupIndex -Path (Join-Path $backupDirectory 'backup-index.json')

    $productionMainPath = Join-Path $productionFullPath 'main.py'
    $changesStarted = $true
    Stop-PrimeBotTargetProcesses -ProductionMainPath $productionMainPath | Out-Null

    Set-PausedDryRunRuntime -Path $runtimePath
    Assert-PausedDryRunRuntime -Path $runtimePath
    $runtimeSafetyEnforced = $true

    foreach ($relativePath in $filesToCopy) {
        $sourcePath = Get-SafePathUnderRoot -Root $sourceWorktreePath -RelativePath $relativePath
        $destinationPath = Get-SafePathUnderRoot -Root $productionFullPath -RelativePath $relativePath
        $destinationParent = Split-Path -Parent $destinationPath

        if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
            New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        }

        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
        $filesCopied += 1
    }

    foreach ($relativePath in $filesToRemove) {
        $destinationPath = Get-SafePathUnderRoot -Root $productionFullPath -RelativePath $relativePath

        if (Test-ProtectedRelativePath -RelativePath $relativePath -Manifest $manifest) {
            throw "Refusing to remove protected path: $relativePath"
        }

        Remove-Item -LiteralPath $destinationPath -Force
        $filesRemoved += 1
    }

    $deploymentState = [ordered]@{
        schemaVersion = 1
        sourceCommit = $sourceCommit
        deployedAt = (Get-Date).ToString('o')
        managedFiles = $managedFiles
    }
    Write-JsonFileAtomic -Value $deploymentState -Path $stateManifestPath

    Set-PausedDryRunRuntime -Path $runtimePath
    Assert-PausedDryRunRuntime -Path $runtimePath
    $runtimeSafetyEnforced = $true

    if ($StartPausedDryRun) {
        Start-ScheduledTask -TaskName $ScheduledTaskName -ErrorAction Stop
    }

    $runningProcessCount = Get-PrimeBotRunningProcessCount -ProductionMainPath $productionMainPath

    if (-not $StartPausedDryRun -and $runningProcessCount -ne 0) {
        throw "Default stopped-state verification failed: $runningProcessCount PrimeBot process(es) are running."
    }

    Write-Host "Source commit: $sourceCommit"
    Write-Host "Files copied: $filesCopied"
    Write-Host "Files removed: $filesRemoved"
    Write-Host "Backup directory: $backupDirectory"
    Write-Host "Test result: $testResultText"
    Write-Host "Verification working directory: $sourceWorktreePath"
    Write-Host 'Final runtime mode: paused=True; auto_execute=False (Dry Run)'
    Write-Host "Running process count: $runningProcessCount"
    $deploymentSucceeded = $true
}
catch {
    $failureMessage = "$($_.Exception.Message) | $($_.ScriptStackTrace)"

    if ($changesStarted -and $null -ne $manifest) {
        if ($null -ne $productionFullPath) {
            try {
                Stop-PrimeBotTargetProcesses `
                    -ProductionMainPath (Join-Path $productionFullPath 'main.py') |
                    Out-Null
            }
            catch {
                $failureMessage = "$failureMessage | FAILED TO CONFIRM BOT STOPPED: $($_.Exception.Message)"
            }
        }

        try {
            Restore-DeploymentBackup `
                -Records $backupRecords `
                -ProductionRoot $productionFullPath `
                -Manifest $manifest `
                -RuntimePreviouslyExisted $runtimeExisted `
                -RuntimeStatePath $runtimePath `
                -RuntimeStateBackup $runtimeBackupPath `
                -StatePreviouslyExisted $stateManifestExisted `
                -DeploymentStatePath $stateManifestPath `
                -DeploymentStateBackup $stateManifestBackupPath
            Write-Warning 'Deployment failed and the production backup was restored. Runtime remains paused and in Dry Run.'
        }
        catch {
            $failureMessage = "$failureMessage | ROLLBACK FAILED: $($_.Exception.Message)"
        }

        if ($null -ne $runtimePath) {
            try {
                Set-PausedDryRunRuntime -Path $runtimePath
                Assert-PausedDryRunRuntime -Path $runtimePath
                $runtimeSafetyEnforced = $true
            }
            catch {
                $runtimeSafetyEnforced = $false
                $failureMessage = "$failureMessage | RUNTIME SAFETY ENFORCEMENT FAILED: $($_.Exception.Message)"
            }
        }
    }

    if ($null -ne $sourceCommit) {
        Write-Host "Source commit: $sourceCommit"
    }
    else {
        Write-Host 'Source commit: not resolved'
    }

    Write-Host "Files copied before failure: $filesCopied"
    Write-Host "Files removed before failure: $filesRemoved"
    Write-Host "Backup directory: $backupDirectory"
    Write-Host "Test result: $testResultText"
    Write-Host "Verification working directory: $sourceWorktreePath"
    if ($runtimeSafetyEnforced) {
        Write-Host 'Final runtime mode: paused=True; auto_execute=False (Dry Run)'
    }
    elseif ($changesStarted) {
        Write-Host 'Final runtime mode: UNKNOWN - runtime safety enforcement failed'
    }
    else {
        Write-Host 'Final runtime mode: unchanged (production changes did not begin)'
    }
    if ($changesStarted -and $null -ne $productionFullPath) {
        try {
            $failureProcessCount = Get-PrimeBotRunningProcessCount `
                -ProductionMainPath (Join-Path $productionFullPath 'main.py')
            Write-Host "Running process count: $failureProcessCount"
        }
        catch {
            Write-Host 'Running process count: unknown (process inspection failed)'
        }
    }
    else {
        Write-Host 'Running process count: not inspected'
    }
}
finally {
    $cleanupFailures = New-Object System.Collections.Generic.List[string]

    if ($null -ne $repositoryFullPath -and $null -ne $sourceWorktreePath) {
        if (Test-Path -LiteralPath $sourceWorktreePath) {
            try {
                Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
                    'worktree', 'remove', '--force', '--', $sourceWorktreePath
                ) | Out-Null
            }
            catch {
                $cleanupFailures.Add("Temporary worktree removal failed: $($_.Exception.Message)")
            }
        }

        try {
            Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
                'worktree', 'prune'
            ) | Out-Null
        }
        catch {
            $cleanupFailures.Add("Git worktree prune failed: $($_.Exception.Message)")
        }

        if (Test-Path -LiteralPath $sourceWorktreePath) {
            $cleanupFailures.Add("Temporary worktree still exists: $sourceWorktreePath")
        }
        else {
            Write-Host "Temporary worktree removed: $sourceWorktreePath"
        }
    }

    if (
        $null -ne $repositoryFullPath -and
        $null -ne $repositoryInitialBranch -and
        $null -ne $repositoryInitialHead
    ) {
        try {
            $repositoryFinalBranch = (
                Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
                    'rev-parse', '--abbrev-ref', 'HEAD'
                )
            ) -join ''
            $repositoryFinalBranch = $repositoryFinalBranch.Trim()
            $repositoryFinalHead = (
                Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
                    'rev-parse', 'HEAD'
                )
            ) -join ''
            $repositoryFinalHead = $repositoryFinalHead.Trim().ToLowerInvariant()
            $repositoryFinalStatus = @(
                Invoke-GitCapture -Repository $repositoryFullPath -Arguments @(
                    'status', '--porcelain', '--untracked-files=all'
                )
            )

            if ($repositoryFinalBranch -cne $repositoryInitialBranch) {
                $cleanupFailures.Add(
                    "Deployment repository branch changed from $repositoryInitialBranch to $repositoryFinalBranch."
                )
            }

            if ($repositoryFinalHead -cne $repositoryInitialHead) {
                $cleanupFailures.Add(
                    "Deployment repository HEAD changed from $repositoryInitialHead to $repositoryFinalHead."
                )
            }

            if ($repositoryFinalStatus.Count -ne 0) {
                $cleanupFailures.Add('Deployment repository is not clean after deployment.')
            }

            Write-Host "Deployment repository branch: $repositoryFinalBranch"
            Write-Host "Deployment repository HEAD: $repositoryFinalHead"
            Write-Host "Deployment repository clean: $($repositoryFinalStatus.Count -eq 0)"
        }
        catch {
            $cleanupFailures.Add("Deployment repository invariant verification failed: $($_.Exception.Message)")
        }
    }

    if ($cleanupFailures.Count -ne 0) {
        $deploymentSucceeded = $false
        $cleanupMessage = $cleanupFailures -join ' | '

        if ([string]::IsNullOrWhiteSpace($failureMessage)) {
            $failureMessage = $cleanupMessage
        }
        else {
            $failureMessage = "$failureMessage | $cleanupMessage"
        }
    }
}

if ($deploymentSucceeded) {
    exit 0
}

Write-Error -Message $failureMessage -ErrorAction Continue
exit 1
