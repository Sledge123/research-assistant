param(
    [ValidateSet('Repository')]
    [string]$Scope = 'Repository',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    $failures = @(
        foreach ($name in @('PROJECT_PLAN.md', 'AGENTS.md', 'README.md', 'TASKS.md')) {
            if (-not (Test-Path -LiteralPath $name)) {
                "$name : 不存在"
            } elseif (-not (Test-Path -LiteralPath $name -PathType Leaf)) {
                "$name : 不是文件"
            } else {
                try {
                    if ([string]::IsNullOrWhiteSpace((Get-Content -Raw -Encoding utf8 -LiteralPath $name -ErrorAction Stop))) {
                        "$name : 内容为空"
                    }
                } catch {
                    "$name : 无法读取"
                }
            }
        }
    )
    if ($failures.Count -gt 0) {
        $failures | ForEach-Object { Write-Output "FAIL $_" }
        exit 1
    }
    Write-Output 'PASS 四文件均存在、为文件且内容非空'
    if (-not $Python) {
        $candidates = if ($IsWindows) {
            @('./.venv-check/Scripts/python.exe', './.venv/Scripts/python.exe', './.venv-web/Scripts/python.exe')
        } else {
            @('./.venv-check/bin/python', './.venv/bin/python', './.venv-web/bin/python')
        }
        $Python = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $Python) { $Python = 'python' }
    }
    & $Python -B ./scripts/verify-repository.py --root .
    exit $LASTEXITCODE
} catch {
    Write-Output 'FAIL 公开验证未能启动，请检查项目内 Python 环境和验证脚本。'
    exit 1
} finally {
    Pop-Location
}
