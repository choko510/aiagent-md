$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  throw "cargo is not installed; NOT_RUN (no test has passed)"
}
cargo fmt --all -- --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
cargo clippy --workspace --all-targets --locked -- -D warnings
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
cargo test --workspace --locked
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
cargo check --workspace --locked
exit $LASTEXITCODE
