[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = 'C:\PrimeBot2-Repo',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$ProductionPath = 'C:\PrimeBot',

    [Parameter()]
    [switch]$ValidationOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedOriginUrl = 'https://github.com/SamuelEto18/primebot-2.git'


function Fail-Bootstrap {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Error $Message
    exit 1
}


try {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue

    if ($null -eq $git) {
        Write-Host 'Git for Windows is required but is not installed or not on PATH.'
        Write-Host 'Install it manually from https://git-scm.com/download/win or run:'
        Write-Host '  winget install --id Git.Git -e'
        Write-Host 'Then rerun this bootstrap script. Git was not installed automatically.'
        exit 1
    }

    $repositoryFullPath = [System.IO.Path]::GetFullPath($RepositoryPath).TrimEnd('\')
    $productionFullPath = [System.IO.Path]::GetFullPath($ProductionPath).TrimEnd('\')
    $productionPrefix = $productionFullPath + '\'

    if (
        $repositoryFullPath.Equals(
            $productionFullPath,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        $repositoryFullPath.StartsWith(
            $productionPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'Repository path must not be C:\PrimeBot or any directory inside the production path.'
    }

    if (Test-Path -LiteralPath $repositoryFullPath) {
        if (-not (Test-Path -LiteralPath (Join-Path $repositoryFullPath '.git') -PathType Container)) {
            throw "Existing repository path is not a Git clone: $repositoryFullPath"
        }

        $origin = (& git.exe -C $repositoryFullPath config --get remote.origin.url 2>&1 | Out-String).Trim()

        if ($LASTEXITCODE -ne 0) {
            throw "Unable to read origin URL for $repositoryFullPath"
        }

        if ($origin -cne $ExpectedOriginUrl) {
            throw "Wrong origin URL. Expected '$ExpectedOriginUrl' but found '$origin'."
        }

        Write-Host "Repository already exists with approved origin: $repositoryFullPath"
    }
    elseif ($ValidationOnly) {
        Write-Host "Validation only: repository would be cloned to $repositoryFullPath"
    }
    else {
        Write-Host "Cloning approved repository into $repositoryFullPath"
        $previousErrorActionPreference = $ErrorActionPreference

        try {
            $ErrorActionPreference = 'Continue'
            & git.exe clone -- $ExpectedOriginUrl $repositoryFullPath
            $cloneExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }

        if ($cloneExitCode -ne 0) {
            throw "Git clone failed with exit code $cloneExitCode."
        }
    }

    if ($ValidationOnly) {
        Write-Host 'Validation-only mode completed. No files, services, or processes were changed.'
    }
    else {
        Write-Host 'Bootstrap completed. PrimeBot was not copied or started.'
    }

    Write-Host "Approved origin: $ExpectedOriginUrl"
    Write-Host "Repository path: $repositoryFullPath"
    Write-Host "Production path left untouched: $productionFullPath"
    exit 0
}
catch {
    Fail-Bootstrap -Message $_.Exception.Message
}
