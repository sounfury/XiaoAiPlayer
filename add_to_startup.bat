@echo off
chcp 65001 >nul
echo 正在将 XiaoAiPlayer 添加到 Windows 开机自启...

set SCRIPT_DIR=%~dp0
set TARGET_VBS=%SCRIPT_DIR%start_silent.vbs
set SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\XiaoAiPlayer.lnk

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%TARGET_VBS%'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.Save()"

if exist "%SHORTCUT_PATH%" (
    echo [成功] 已成功添加到开机自启动！
    echo 电脑开机后将自动在后台无声运行，无需手动启动。
) else (
    echo [失败] 创建启动快捷方式失败，请手动将 start_silent.vbs 放入启动目录。
)
pause
