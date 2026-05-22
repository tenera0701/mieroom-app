Dim WshShell, fso, appDir, pyCmd

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = appDir

' すでにサーバーが起動しているか確認
On Error Resume Next
Dim http
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://localhost:5000/", False
http.Send
Dim alreadyRunning
alreadyRunning = (http.Status = 200)
On Error GoTo 0

If Not alreadyRunning Then
    ' py コマンドを試す、なければ python
    Dim pyPath
    pyPath = ""

    Dim oExec
    Set oExec = WshShell.Exec("where py")
    WScript.Sleep 500
    If oExec.ExitCode = 0 Then
        pyPath = "py"
    Else
        Set oExec = WshShell.Exec("where python")
        WScript.Sleep 500
        If oExec.ExitCode = 0 Then
            pyPath = "python"
        End If
    End If

    If pyPath = "" Then
        MsgBox "Pythonが見つかりませんでした。" & vbCrLf & "Pythonをインストールしてください。", vbExclamation, "ルームピック"
        WScript.Quit
    End If

    ' サーバーをバックグラウンドで起動（ウィンドウ完全非表示）
    WshShell.Run pyPath & " """ & appDir & "\app.py""", 0, False

    ' 起動待ち（5秒）
    WScript.Sleep 5000
End If

' Chromeで開く
WshShell.Run "chrome ""http://localhost:5000/executive""", 1, False

Set WshShell = Nothing
Set fso = Nothing
