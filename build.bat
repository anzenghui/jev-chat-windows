@echo off
setlocal
cd /d "%~dp0"

REM One-click local build. ASCII only: Chinese Windows cmd is GBK.
REM Output: dist\jev-chat-windows\jev-chat-windows.exe

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtualenv .venv ...
    python -m venv .venv || goto :fail
)
call ".venv\Scripts\activate.bat" || goto :fail

echo Installing dependencies ...
python -m pip install -r requirements.txt pyinstaller || goto :fail

echo Building ...
pyinstaller --noconfirm --clean jev.spec || goto :fail
if not exist "dist\jev-chat-windows\plugins\session_recognition" mkdir "dist\jev-chat-windows\plugins\session_recognition" || goto :fail
copy /Y "plugins\session_recognition\plugin.json" "dist\jev-chat-windows\plugins\session_recognition\" >nul || goto :fail
copy /Y "plugins\session_recognition\plugin.py" "dist\jev-chat-windows\plugins\session_recognition\" >nul || goto :fail
if not exist "dist\jev-chat-windows\plugins\contact_profile" mkdir "dist\jev-chat-windows\plugins\contact_profile" || goto :fail
copy /Y "plugins\contact_profile\plugin.json" "dist\jev-chat-windows\plugins\contact_profile\" >nul || goto :fail
copy /Y "plugins\contact_profile\plugin.py" "dist\jev-chat-windows\plugins\contact_profile\" >nul || goto :fail
if not exist "dist\jev-chat-windows\plugins\chat_media" mkdir "dist\jev-chat-windows\plugins\chat_media" || goto :fail
copy /Y "plugins\chat_media\plugin.json" "dist\jev-chat-windows\plugins\chat_media\" >nul || goto :fail
copy /Y "plugins\chat_media\plugin.py" "dist\jev-chat-windows\plugins\chat_media\" >nul || goto :fail
if exist "data\ahu_profile_cli.json" (
    if not exist "dist\jev-chat-windows\data" mkdir "dist\jev-chat-windows\data" || goto :fail
    copy /Y "data\ahu_profile_cli.json" "dist\jev-chat-windows\data\" >nul || goto :fail
)

echo.
echo Build OK.
echo   %cd%\dist\jev-chat-windows\jev-chat-windows.exe
echo Ship the whole dist\jev-chat-windows folder: the exe needs the files next to it.
pause
exit /b 0

:fail
echo.
echo Build FAILED. Scroll up for the error.
pause
exit /b 1
