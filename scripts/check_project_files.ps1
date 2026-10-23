param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$failures = @(
    foreach ($name in @('PROJECT_PLAN.md', 'AGENTS.md', 'README.md', 'TASKS.md')) {
        $candidate = Join-Path $ProjectRoot $name
        if (-not (Test-Path -LiteralPath $candidate)) {
            "$name : 不存在"
        } elseif (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            "$name : 不是文件"
        } else {
            try {
                if ([string]::IsNullOrWhiteSpace((Get-Content -Raw -Encoding utf8 -LiteralPath $candidate -ErrorAction Stop))) {
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
exit 0
