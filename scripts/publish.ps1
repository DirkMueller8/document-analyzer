<#
.SYNOPSIS
  Publish the current state of the local main branch to GitHub as one snapshot commit.
.DESCRIPTION
  Local "main" keeps the full history. GitHub only ever receives the "public" branch: a linear
  series of snapshot commits whose trees equal main's tree at publish time. No local commit
  (or commit message) is ever pushed. The leak-guard pre-push hook checks the snapshot and
  blocks any other push to the remote.
.EXAMPLE
  .\scripts\publish.ps1 -Message "Phase 3: training extraction"
#>
param(
    [Parameter(Mandatory)] [string]$Message,
    [switch]$SkipTests,
    [switch]$ReplaceRemoteHistory   # only for the first publish: overwrite whatever origin/main holds
)
$ErrorActionPreference = "Stop"
Set-Location (git rev-parse --show-toplevel)

if (git status --porcelain) { throw "Uncommitted changes - commit them on main first." }
if ((git rev-parse --abbrev-ref HEAD) -ne "main") { throw "Run this from the main branch." }

if (-not $SkipTests) {
    Push-Location backend
    try { & .\.venv\Scripts\python.exe -m pytest -q; if ($LASTEXITCODE) { throw "Tests failed - not publishing." } }
    finally { Pop-Location }
}

$tree = git rev-parse "main^{tree}"
$parent = git rev-parse --verify -q refs/heads/public
if ($parent -and (git rev-parse "$parent^{tree}") -eq $tree) { Write-Host "Nothing new to publish."; return }

$commitArgs = @("commit-tree", $tree, "-m", $Message)
if ($parent) { $commitArgs += @("-p", $parent) }
$commit = git @commitArgs
git update-ref refs/heads/public $commit
if ($ReplaceRemoteHistory) {
    git fetch -q origin main
    git push --force-with-lease=main:origin/main origin public:main
} else {
    git push origin public:main
}
if ($LASTEXITCODE) { throw "Push failed (see leak-guard output above). First publish? Use -ReplaceRemoteHistory." }
Write-Host "Published $($commit.Substring(0,7)) to origin/main."
