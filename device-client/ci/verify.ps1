$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  throw "cargo is not installed; NOT_RUN (no test has passed)"
}
cargo test -p aiagent-device-core
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
cargo test -p aiagent-device-desktop
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
cargo check -p aiagent-device-desktop
exit $LASTEXITCODE
