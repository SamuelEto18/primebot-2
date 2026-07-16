Set-StrictMode -Version Latest


function Get-PrimeBotTargetProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$ProcessRecords,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$ProductionMainPath
    )

    $expectedPath = [System.IO.Path]::GetFullPath($ProductionMainPath)
    $escapedPath = [regex]::Escape($expectedPath)
    $pattern = '(?i)(?:^|[\s"]){0}(?=$|[\s"])' -f $escapedPath

    return @(
        $ProcessRecords | Where-Object {
            $commandLine = [string]$_.CommandLine
            -not [string]::IsNullOrWhiteSpace($commandLine) -and
            [regex]::IsMatch($commandLine, $pattern)
        }
    )
}


function Get-PrimeBotProcessInventory {
    [CmdletBinding()]
    param()

    try {
        return @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    }
    catch {
        throw "Unable to inspect running processes safely: $($_.Exception.Message)"
    }
}


function Stop-PrimeBotTargetProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$ProductionMainPath
    )

    $targets = @(
        Get-PrimeBotTargetProcesses `
            -ProcessRecords (Get-PrimeBotProcessInventory) `
            -ProductionMainPath $ProductionMainPath
    )

    foreach ($target in $targets) {
        $processId = [int]$target.ProcessId

        if ($processId -eq $PID) {
            throw "Refusing to stop the deployment process itself (PID $processId)."
        }

        Stop-Process -Id $processId -Force -ErrorAction Stop
    }

    $remaining = @(
        Get-PrimeBotTargetProcesses `
            -ProcessRecords (Get-PrimeBotProcessInventory) `
            -ProductionMainPath $ProductionMainPath
    )

    if ($remaining.Count -ne 0) {
        throw "PrimeBot process shutdown verification failed; $($remaining.Count) target process(es) remain."
    }

    return $targets.Count
}


function Get-PrimeBotRunningProcessCount {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$ProductionMainPath
    )

    $targets = @(
        Get-PrimeBotTargetProcesses `
            -ProcessRecords (Get-PrimeBotProcessInventory) `
            -ProductionMainPath $ProductionMainPath
    )

    return $targets.Count
}


Export-ModuleMember -Function @(
    'Get-PrimeBotTargetProcesses',
    'Get-PrimeBotProcessInventory',
    'Stop-PrimeBotTargetProcesses',
    'Get-PrimeBotRunningProcessCount'
)
