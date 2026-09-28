# Public geocoding is for public test addresses only. No client data.
$env:GEOCODING_MODE = 'public_demo'
$env:ROUTING_MATRIX_MODE = 'input'
Push-Location (Join-Path $PSScriptRoot '..')
try { python -m backend.web } finally { Pop-Location }
