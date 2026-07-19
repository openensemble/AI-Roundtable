# Safe bridge for npm's generated PowerShell CLI shims on native Windows.
# Provider arguments arrive through PowerShell's -File argv binding, never as source text.
$ErrorActionPreference = 'Stop'

try {
    if ($args.Count -lt 1) {
        throw 'Missing PowerShell shim path.'
    }

    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8

    $shimPath = $args[0]
    [string[]] $shimArgs = @()
    if ($args.Count -gt 1) {
        $shimArgs = $args[1..($args.Count - 1)]
    }

    $global:LASTEXITCODE = 0
    & $shimPath @shimArgs
    exit $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
