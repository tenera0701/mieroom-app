@echo off
chcp 65001 > nul
echo ==========================================
echo   ルームピック管理ツール - 外部公開
echo ==========================================
echo.

REM ngrokがインストールされているか確認
where ngrok >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] ngrokが見つかりません。
    echo.
    echo 以下の手順でインストールしてください:
    echo 1. https://ngrok.com/download を開く
    echo 2. Windows版をダウンロード・解凍
    echo 3. ngrok.exe をこのフォルダに置く
    echo 4. https://ngrok.com で無料アカウント作成
    echo 5. ダッシュボードの "Your Authtoken" をコピーして
    echo    ngrok config add-authtoken [トークン] を実行
    echo.
    pause
    exit /b
)

REM Flaskサーバーが起動しているか確認
powershell -Command "try { (New-Object Net.WebClient).DownloadString('http://localhost:5000/') | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Flaskサーバーが起動していません。
    echo     先に「起動.bat」を実行してください。
    echo.
    pause
    exit /b
)

echo [OK] Flaskサーバー確認済み
echo.
echo [起動中] ngrokでインターネット公開します...
echo.
echo ※ 表示される "Forwarding" のURLをスタッフに共有してください
echo ※ このウィンドウを閉じると接続が切れます
echo.
ngrok http 5000
