@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ==========================================================================
rem  朗读助手 · 登录窗口
rem
rem  知乎这类站没有登录态就只给一个 403 挑战页，光换 UA 是过不去的。
rem  这个脚本开一个"能被朗读助手接管"的浏览器窗口，把里面的登录态借过来。
rem
rem  为什么不用你日常那个浏览器？
rem    Chrome 136 起不允许在默认配置目录上开远程调试端口（防止别的程序
rem    悄悄接管你的浏览器）。所以必须用独立配置目录 —— 好处是互不干扰，
rem    你可以放心在这里登录。
rem
rem  只需要登录一次，cookie 就留在 browser-profile 目录里，之后随取随用。
rem ==========================================================================

set "PORT=9222"
set "PROFILE=%~dp0browser-profile"
set "HOME_PAGE=https://www.zhihu.com/"

set "EXE="
for %%p in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
  "%LocalAppData%\Microsoft\Edge\Application\msedge.exe"
) do if not defined EXE if exist %%p set "EXE=%%~p"

if not defined EXE (
  echo.
  echo   [x] 没找到 Chrome 或 Edge。
  echo       装一个 Chromium 系浏览器就行（Chrome / Edge / Brave 都可以）。
  echo.
  pause
  exit /b 1
)

echo.
echo   朗读助手 - 登录窗口
echo   --------------------------------------------------------------
echo   用独立配置目录打开浏览器，不会碰你日常那个窗口。
echo.
echo   接下来三步：
echo     1. 在弹出的窗口里登录知乎（顺手把掘金、小红书也登上更好）
echo     2. 回到朗读工作台，点「登录态」再点「从浏览器导入」
echo     3. 以后不用重复登录，导入一次能用很久
echo.
echo   浏览器   : %EXE%
echo   配置目录 : %PROFILE%
echo   调试端口 : %PORT%
echo   --------------------------------------------------------------
echo.

if not exist "%PROFILE%" mkdir "%PROFILE%"

start "" "%EXE%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --new-window "%HOME_PAGE%"

echo   浏览器已启动。
echo   登录完成后可以直接关掉这个命令行窗口 —— 浏览器别关，导入登录态要用到它。
echo.
timeout /t 8 >nul
endlocal
