' Start XiaoAi control service silently in background (no console window)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "d:\open-source-projects\XiaoAiPlayer"
WshShell.Run "python main.py", 0, False
