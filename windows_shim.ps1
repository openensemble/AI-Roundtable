# Safe bridge for npm's generated PowerShell CLI shims on native Windows.
# Provider arguments arrive as JSON in this child process's environment, never as source text.
$ErrorActionPreference = 'Stop'

try {
    if ([string]::IsNullOrWhiteSpace($env:AICONVO_SHIM_PATH)) {
        throw 'Missing PowerShell shim path.'
    }
    if ($null -eq $env:AICONVO_SHIM_ARGS_JSON) {
        throw 'Missing PowerShell shim arguments.'
    }

    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8

    $shimPath = $env:AICONVO_SHIM_PATH
    $decodedArgs = ConvertFrom-Json -InputObject $env:AICONVO_SHIM_ARGS_JSON
    [string[]] $shimArgs = @()
    if ($null -ne $decodedArgs) {
        $shimArgs = @($decodedArgs | ForEach-Object { [string] $_ })
    }

    $global:LASTEXITCODE = 0
    & $shimPath @shimArgs
    exit $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
