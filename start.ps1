$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# すでに起動中か確認
$running = $false
try {
    $r = Invoke-WebRequest "http://localhost:5000/" -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $running = $true }
} catch {}

if (-not $running) {
    # Minimizedで起動（Hiddenより確実）
    Start-Process -FilePath "py" -ArgumentList "app.py" -WorkingDirectory $dir -WindowStyle Minimized
    Start-Sleep -Seconds 6
}

# Chromeで開く（複数パスを試す）
$chromePaths = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$opened = $false
foreach ($cp in $chromePaths) {
    if (Test-Path $cp) {
        Start-Process $cp "http://localhost:5000/executive"
        $opened = $true
        break
    }
}
if (-not $opened) {
    Start-Process "chrome" "http://localhost:5000/executive"
}
