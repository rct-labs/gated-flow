<#
.SYNOPSIS
  Install the workflow skills so one name works in every CLI, and put `flow` on PATH.

.DESCRIPTION
  One physical directory per skill, linked everywhere else:

    ~/.agents/skills/<name>/   canonical copy — read by Codex, Grok Build and Kimi Code
    ~/.claude/skills/<name>    an NTFS junction into it, read by Claude Code
    ~/.codex/skills/<name>     an NTFS junction into it, read by Codex

  Grok Build resolves ~/.agents/skills in preference to ~/.claude/skills, so a
  second physical copy would shadow the first. Junctions need no elevation and
  the skill scanners follow them. Where a junction cannot be created the
  installer falls back to a copy.

  The `flow` shim is written to ~/.local/bin (flow.cmd for cmd/PowerShell and
  a POSIX `flow` for Git Bash); add that directory to PATH once.

.EXAMPLE
  pwsh -File install.ps1
  pwsh -File install.ps1 -Skill flow,model-debate
  pwsh -File install.ps1 -Copy            # force the copy fallback
  pwsh -File install.ps1 -NoShim
  pwsh -File install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string[]]$Skill = @(),
    [switch]$Copy,
    [switch]$NoShim,
    [switch]$Uninstall,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$source     = Join-Path $root 'skills'
$agentsRoot = Join-Path $env:USERPROFILE '.agents\skills'
$claudeRoot = Join-Path $env:USERPROFILE '.claude\skills'
$codexRoot  = Join-Path $env:USERPROFILE '.codex\skills'
$binDir     = if ($env:FLOW_BIN_DIR) { $env:FLOW_BIN_DIR } else { Join-Path $env:USERPROFILE '.local\bin' }

function Get-Skills {
    if (-not (Test-Path $source)) { throw "no skills directory at $source" }
    $all = Get-ChildItem $source -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') }
    if ($Skill.Count -eq 0) { return $all }
    foreach ($name in $Skill) {
        if (-not ($all | Where-Object Name -eq $name)) { throw "no such skill: $name" }
    }
    return $all | Where-Object { $Skill -contains $_.Name }
}

function Remove-Target([string]$path) {
    if (-not (Test-Path $path)) { return }
    $item = Get-Item $path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $item.Delete()          # remove the link, never its target
    } else {
        Remove-Item $path -Recurse -Force
    }
}

function Link-Or-Copy([string]$name, [string]$canon, [string]$mirror) {
    Remove-Target $mirror
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $mirror) | Out-Null
    if (-not $Copy) {
        try {
            New-Item -ItemType Junction -Path $mirror -Target $canon -ErrorAction Stop | Out-Null
            "junction  $name -> $mirror"
            return
        } catch {
            "junction failed for $name ($($_.Exception.Message)); falling back to a copy"
        }
    }
    Copy-Item $canon $mirror -Recurse -Force
    "copy      $name -> $mirror"
}

if ($Uninstall) {
    foreach ($cap in Get-Skills) {
        foreach ($t in @((Join-Path $agentsRoot $cap.Name), (Join-Path $claudeRoot $cap.Name), (Join-Path $codexRoot $cap.Name))) {
            if (Test-Path $t) { Remove-Target $t; "removed  $t" }
        }
    }
    foreach ($f in @((Join-Path $binDir 'flow.cmd'), (Join-Path $binDir 'flow'))) {
        if (Test-Path $f) { Remove-Item $f -Force; "removed  $f" }
    }
    "done. Per-project files (.gate/, TASK_QUEUE.md, CONTEXT.md, .git/hooks/pre-commit) are untouched."
    return
}

New-Item -ItemType Directory -Force -Path $agentsRoot, $claudeRoot, $codexRoot | Out-Null

foreach ($cap in Get-Skills) {
    $name  = $cap.Name
    $canon = Join-Path $agentsRoot $name

    if ((Test-Path $canon) -and -not $Force) {
        $existing = Get-Item $canon -Force
        if (-not ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            "skip     $name  (already present at $canon; re-run with -Force to replace)"
            continue
        }
    }

    Remove-Target $canon
    Copy-Item $cap.FullName $canon -Recurse -Force
    "canonical $name -> $canon"
    Link-Or-Copy $name $canon (Join-Path $claudeRoot $name)
    Link-Or-Copy $name $canon (Join-Path $codexRoot $name)
}

if (-not $NoShim) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $py) {
        "python not found on PATH; skipping the flow shim (install Python 3.10+ and re-run)"
    } else {
        $existing = Get-Command flow -ErrorAction SilentlyContinue
        if ($existing -and -not $existing.Source.StartsWith($binDir, [StringComparison]::OrdinalIgnoreCase)) {
            "WARNING: another 'flow' is already on PATH at $($existing.Source) (Facebook's Flow type checker uses the same name)."
            "         Put $binDir ahead of it in PATH, or call: python `"$(Join-Path $root 'flow\flow.py')`""
        }
        New-Item -ItemType Directory -Force -Path $binDir | Out-Null
        $flowPy = Join-Path $root 'flow\flow.py'
        Set-Content -Path (Join-Path $binDir 'flow.cmd') -Value "@`"$($py.Source)`" `"$flowPy`" %*" -Encoding ascii
        $posixPy = ($py.Source -replace '\\', '/')
        $posixFlow = ($flowPy -replace '\\', '/')
        [IO.File]::WriteAllText((Join-Path $binDir 'flow'), "#!/bin/sh`nexec `"$posixPy`" `"$posixFlow`" `"`$@`"`n")
        "shim      $(Join-Path $binDir 'flow.cmd')"
        "shim      $(Join-Path $binDir 'flow')"
        if (-not (($env:PATH -split ';') -contains $binDir)) {
            "NOTE: add $binDir to PATH so that 'flow' resolves (User environment variables)."
        }
    }
}

""
"Invoke the same skill in each CLI (the name is identical; only the sigil differs):"
"  Claude Code   /flow        /flow-run        /run-queue        /model-debate"
"  Codex         `$flow        `$flow-run        `$run-queue        `$model-debate"
"  Kimi Code     /skill:flow  /skill:flow-run  /skill:run-queue  /skill:model-debate"
"  Grok Build    /flow        /flow-run        /run-queue        /model-debate"
""
"Per project, once:  flow init --repo <project> --verify-cmd `"<acceptance command>`""
"Start a new agent session if a skill does not appear yet."
