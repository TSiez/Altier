#requires -Version 5.1
# One-off: generate a top-down pink wire basket via Kie.ai (nano-banana).
# Reads KIE_API_KEY from D2/.env. NOT Gemini.
$ErrorActionPreference = "Stop"
$OutDir = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "assets"
$Out    = Join-Path $OutDir "basket-wire.png"
$EnvPath = "C:\Users\acer\Desktop\Arca\D2\.env"

$apiKey = ((Get-Content $EnvPath -Raw) | Select-String -Pattern 'KIE_API_KEY=([^\s]+)').Matches[0].Groups[1].Value
if (-not $apiKey) { throw "KIE_API_KEY not found" }
$base = "https://api.kie.ai/api/v1/jobs"
$headers = @{ "Authorization" = "Bearer $apiKey"; "Content-Type" = "application/json" }

$prompt = "Top-down flat-lay product photo of a single empty rectangular wire mesh basket made of soft pastel pink coated metal wire. Fine even square grid pattern across the bottom, gently rounded corners, sloped wire walls, two small curled hook handles on each long side. Centered, shot straight from directly above, on a pure flat white background, soft even studio lighting, subtle soft shadow. Minimal, clean, no objects inside the basket, no text, no labels, no watermark."

$body = @{ model = "google/nano-banana"; input = @{ prompt = $prompt; output_format = "png"; image_size = "16:9" } } | ConvertTo-Json -Depth 6 -Compress
$resp = Invoke-RestMethod -Method Post -Uri "$base/createTask" -Headers $headers -Body $body -TimeoutSec 60
if ($resp.code -ne 200) { throw "createTask: $($resp.msg)" }
$taskId = $resp.data.taskId
Write-Host "submitted -> $taskId"

for ($i = 0; $i -lt 80; $i++) {
  Start-Sleep -Seconds 5
  $info = Invoke-RestMethod -Uri "$base/recordInfo?taskId=$taskId" -Headers $headers -TimeoutSec 30
  $state = $info.data.state
  if ($state -eq "success") {
    $url = ($info.data.resultJson | ConvertFrom-Json).resultUrls[0]
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $Out -TimeoutSec 120
    Write-Host "DONE -> $Out"
    break
  } elseif ($state -eq "fail") { throw "task failed: $($info.data.failMsg)" }
  else { Write-Host "  ...$state ($($i*5)s)" }
}
