<#
  XiangKou / xiangkou -- local end-to-end acceptance check.

  Walks the README acceptance path step by step and reports which link fails.
  Read-only: it never creates orders.

  Prerequisites:
    1) docker compose -f dev/docker-compose.yml up -d   (mysql, redis, go-api, nginx)
    2) the Python app is listening on 8090

  Usage:
    powershell -NoProfile -File scripts\verify_stack.ps1 -Email you@example.com

  Note: the login code is read straight from Redis (login:code:<email>) instead of
  parsing 既有服务 logs, so console encoding never breaks the lookup. That also means the
  check still passes when SMTP credentials are not configured: 既有服务 stores the code
  before it tries to send the mail.
#>
[CmdletBinding()]
param(
    [string]$Email = "user@example.com",
    [string]$App = "http://127.0.0.1:8090",
    [string]$既有服务 = "http://127.0.0.1:8081",
    [string]$ComposeFile = "dev/docker-compose.yml"
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo

$script:results = New-Object System.Collections.Generic.List[object]

function Record([string]$name, [bool]$ok, [string]$detail) {
    $label = "PASS"
    $color = "Green"
    if (-not $ok) { $label = "FAIL"; $color = "Red" }
    $script:results.Add([pscustomobject]@{ Step = $name; Result = $label; Detail = $detail })
    Write-Host ("[{0}] {1}" -f $label, $name) -ForegroundColor $color
    Write-Host ("       {0}" -f $detail)
}

function Step([string]$name, [scriptblock]$body) {
    try {
        $detail = & $body
        Record $name $true ([string]$detail)
        return $true
    } catch {
        Record $name $false $_.Exception.Message
        return $false
    }
}

Write-Host "== XiangKou local acceptance ==" -ForegroundColor Cyan
Write-Host ("email={0}  app={1}  go-api={2}" -f $Email, $App, $既有服务)
Write-Host ""

# ---- 1. 既有服务 business service ----
$go-apiOk = Step "既有服务 GET /shop-type/list" {
    $r = Invoke-RestMethod -Uri "$既有服务/shop-type/list" -TimeoutSec 15
    if ($r.success -ne $true) { throw "既有服务 answered but success != true" }
    "shop types = " + @($r.data).Count
}
if (-not $go-apiOk) {
    Write-Host ""
    Write-Host "既有服务 is not reachable. Everything downstream needs it. Check:" -ForegroundColor Yellow
    Write-Host ("  docker compose -f {0} ps" -f $ComposeFile) -ForegroundColor Yellow
    Write-Host ("  docker compose -f {0} logs --tail 100 go-api" -f $ComposeFile) -ForegroundColor Yellow
    Write-Host "  wait for 'Started XiangkouApplication' (first run downloads Maven deps, 5-10 min)" -ForegroundColor Yellow
    Write-Host ""
}

# ---- 2. Python app ----
Step "App GET /health" {
    $r = Invoke-RestMethod -Uri "$App/health" -TimeoutSec 10
    "service=" + $r.service + " mode=" + $r.llm.mode + " model=" + $r.llm.model
} | Out-Null

$readyOk = Step "App GET /ready" {
    $r = Invoke-RestMethod -Uri "$App/ready" -TimeoutSec 20
    "status=" + $r.status + " shop_types=" + $r.shop_types + " state=" + $r.state_storage
}
if (-not $readyOk) {
    Write-Host "/ready failed. Usual causes: 既有服务 not ready, MySQL not initialised, data dir not writable." -ForegroundColor Yellow
}

# ---- 3. Login (code comes from Redis) ----
# Windows PowerShell 5.1 posts -Body strings as ISO-8859-1, which turns any
# non-ASCII payload (the Chinese chat message) into question marks. Post UTF-8 bytes.
function PostJson([string]$uri, $payload, $webSession, [int]$timeoutSec) {
    $json = $payload | ConvertTo-Json -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    if ($webSession) {
        return Invoke-RestMethod -Uri $uri -Method Post -ContentType "application/json; charset=utf-8" -Body $bytes -WebSession $webSession -TimeoutSec $timeoutSec
    }
    return Invoke-RestMethod -Uri $uri -Method Post -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec $timeoutSec
}

Step "App POST /api/auth/code" {
    try {
        $r = PostJson "$App/api/auth/code" @{ email = $Email } $null 20
        "status=" + $r.status
    } catch {
        # 既有服务 stores the code in Redis before it sends the mail, so a missing SMTP
        # configuration must not abort the rest of the check.
        throw ("send failed: " + $_.Exception.Message + " -- if SMTP is not configured this is expected; the code is still in Redis, the check continues")
    }
} | Out-Null

$code = $null
Step "Redis get login:code:$Email" {
    $raw = (& docker compose -f $ComposeFile exec -T redis redis-cli --raw get "login:code:$Email" 2>$null | Out-String).Trim()
    if (-not $raw -or $raw -notmatch '^\d{6}$') {
        throw "no 6-digit code in Redis (got '" + $raw + "'). Is the redis container up?"
    }
    $script:code = $raw
    "code = " + $raw
} | Out-Null

$session = $null
if ($script:code) {
    Step "App POST /api/auth/login" {
        $json = (@{ email = $Email; code = $script:code } | ConvertTo-Json -Compress)
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $r = Invoke-RestMethod -Uri "$App/api/auth/login" -Method Post -ContentType "application/json; charset=utf-8" -Body $bytes -SessionVariable s -TimeoutSec 20
        $script:session = $s
        "user id=" + $r.user.id + " nick=" + $r.user.nickName
    } | Out-Null
}

if ($script:session) {
    Step "App GET /api/auth/me" {
        $r = Invoke-RestMethod -Uri "$App/api/auth/me" -WebSession $script:session -TimeoutSec 20
        "user id=" + $r.user.id
    } | Out-Null

    Step "App POST /api/chat (shop cards)" {
        $payload = @{
            message    = "$([char]0x627E)$([char]0x4EBA)$([char]0x5747)120$([char]0x4EE5)$([char]0x5185)$([char]0x3001)4$([char]0x5206)$([char]0x4EE5)$([char]0x4E0A)$([char]0x7684)$([char]0x706B)$([char]0x9505)$([char]0x5E97)"
            request_id = [guid]::NewGuid().ToString()
        }
        $r = PostJson "$App/api/chat" $payload $script:session 120
        $shopCount = @($r.shops).Count
        if ($shopCount -lt 1) { throw "no shop cards returned; answer=" + $r.answer }
        "shops=" + $shopCount + " session_id=" + $r.session_id
    } | Out-Null

    Step "App GET /api/my/orders" {
        $r = Invoke-RestMethod -Uri "$App/api/my/orders?page=1" -WebSession $script:session -TimeoutSec 20
        "total=" + $r.total
    } | Out-Null

    Step "App GET /api/catalog/shops?typeId=1" {
        $r = Invoke-RestMethod -Uri "$App/api/catalog/shops?typeId=1&page=1" -WebSession $script:session -TimeoutSec 20
        # This endpoint proxies the raw 既有服务 Result envelope: {"success":true,"data":[...]}
        $n = @($r.data | Where-Object { $_ }).Count
        if ($n -lt 1) { throw "catalog returned no shops (looked at .data; envelope keys: " + (($r.PSObject.Properties.Name) -join ",") + ")" }
        "shops=" + $n
    } | Out-Null
} else {
    Write-Host "Not logged in; skipped checks that need identity (chat / orders / catalog)." -ForegroundColor Yellow
}

# ---- summary ----
Write-Host ""
Write-Host "== summary ==" -ForegroundColor Cyan
$script:results | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Write-Host

$failed = @($script:results | Where-Object { $_.Result -eq "FAIL" })
if ($failed.Count -eq 0) {
    Write-Host "All checks passed. Remaining manual acceptance (script stays read-only):" -ForegroundColor Green
    Write-Host ("  open {0} -> browse shops -> pick a free voucher -> review the preview -> click confirm" -f $App) -ForegroundColor Green
    Write-Host "  then confirm the order shows up under My Orders / My Vouchers" -ForegroundColor Green
} else {
    Write-Host ("{0} check(s) failed. Fix the FIRST FAIL above; later ones usually cascade." -f $failed.Count) -ForegroundColor Red
}

Pop-Location
exit $failed.Count
