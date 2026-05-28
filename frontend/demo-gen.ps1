$ErrorActionPreference = 'Stop'
# Read the kie.ai API key from the KIE_API_KEY environment variable.
# Set it once per session:   $env:KIE_API_KEY = 'your-kie-ai-key-here'
$KEY = $env:KIE_API_KEY
if (-not $KEY) { Write-Error "KIE_API_KEY environment variable is not set."; exit 1 }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PromptsPath = Join-Path $Root 'demo-prompts.json'
$ResultsPath = Join-Path $Root 'demo-results.json'

$data = Get-Content -Raw -LiteralPath $PromptsPath | ConvertFrom-Json

# Build the 12 jobs (6 concepts x 2 faces)
$jobs = New-Object System.Collections.ArrayList
foreach ($c in $data.concepts) {
  foreach ($face in @('front','back')) {
    $prompt = if ($face -eq 'front') { $c.frontPrompt } else { $c.backPrompt }
    [void]$jobs.Add([pscustomobject]@{
      conceptName = $c.name
      category    = $c.category
      face        = $face
      prompt      = $prompt
      taskId      = $null
      status      = 'queued'
      url         = $null
      error       = $null
      tries       = 0
    })
  }
}

Write-Host ("Dispatching {0} kie.ai jobs..." -f $jobs.Count) -ForegroundColor Cyan

# Dispatch all createTask
foreach ($j in $jobs) {
  try {
    $body = @{
      model = 'google/nano-banana'
      input = @{
        prompt       = $j.prompt
        output_format = 'png'
        image_size   = '16:9'
      }
    } | ConvertTo-Json -Depth 6 -Compress

    $resp = Invoke-RestMethod -Method POST `
      -Uri 'https://api.kie.ai/api/v1/jobs/createTask' `
      -Headers @{ 'Authorization' = "Bearer $KEY"; 'Content-Type' = 'application/json' } `
      -Body $body

    if ($resp.code -ne 200 -or -not $resp.data.taskId) {
      throw ("createTask response code={0} msg={1}" -f $resp.code, $resp.msg)
    }
    $j.taskId = $resp.data.taskId
    $j.status = 'polling'
    Write-Host ("  + {0,-16} {1,-5}  task={2}" -f $j.conceptName, $j.face, $j.taskId)
  } catch {
    $j.status = 'err'
    $j.error  = $_.Exception.Message
    Write-Host ("  x {0,-16} {1,-5}  ERR {2}" -f $j.conceptName, $j.face, $j.error) -ForegroundColor Red
  }
}

# Poll until all resolved or max iterations
$maxIter = 60
$iter = 0
while ($iter -lt $maxIter) {
  $pending = @($jobs | Where-Object { $_.status -eq 'polling' })
  if ($pending.Count -eq 0) { break }
  $iter++
  Start-Sleep -Seconds 5
  Write-Host ("[poll {0}] {1} pending..." -f $iter, $pending.Count) -ForegroundColor DarkGray

  foreach ($j in $pending) {
    try {
      $r = Invoke-RestMethod `
        -Uri ("https://api.kie.ai/api/v1/jobs/recordInfo?taskId={0}" -f $j.taskId) `
        -Headers @{ 'Authorization' = "Bearer $KEY" }
      $state = $r.data.state
      $j.tries += 1
      if ($state -eq 'success' -or $state -eq 'succeeded') {
        $rj = $r.data.resultJson
        if ($rj -is [string]) { $rj = $rj | ConvertFrom-Json }
        $url = $null
        if ($rj.resultUrls -and $rj.resultUrls.Count -gt 0) { $url = $rj.resultUrls[0] }
        if (-not $url -and $rj.url) { $url = $rj.url }
        if (-not $url) { throw 'success but no resultUrl' }
        $j.url = $url
        $j.status = 'done'
        Write-Host ("    OK {0,-16} {1,-5}" -f $j.conceptName, $j.face) -ForegroundColor Green
      } elseif ($state -eq 'fail' -or $state -eq 'failed') {
        $j.status = 'err'
        $j.error  = $r.data.failMsg
        Write-Host ("    FAIL {0,-16} {1,-5}  {2}" -f $j.conceptName, $j.face, $j.error) -ForegroundColor Red
      }
    } catch {
      # transient — keep polling
    }
  }
}

# Save results
$jobs | ConvertTo-Json -Depth 8 | Out-File -Encoding utf8 -LiteralPath $ResultsPath
$done = @($jobs | Where-Object { $_.status -eq 'done' }).Count
$err  = @($jobs | Where-Object { $_.status -eq 'err'  }).Count
$pend = @($jobs | Where-Object { $_.status -eq 'polling' }).Count
Write-Host ("`nDone. Saved -> {0}" -f $ResultsPath) -ForegroundColor Cyan
Write-Host ("  ok={0}  err={1}  pending={2}" -f $done, $err, $pend)
