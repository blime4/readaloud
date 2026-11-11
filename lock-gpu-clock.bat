@echo off
setlocal

rem ==========================================================================
rem   lock-gpu-clock.bat - keep the local TTS GPU at high clock (needs admin)
rem
rem   Why: CosyVoice runs at ~35%% GPU utilization, so NVIDIA's auto DVFS
rem   decides the card is idle and drops it to 400-900MHz. Same sentence:
rem       1800MHz -> 9.9s   (keeps up with playback)
rem        500MHz -> 22s    (more than twice as slow -> long text stutters)
rem   Locking the clock (or NVIDIA Control Panel -> Power management mode ->
rem   Prefer maximum performance) keeps it high.
rem
rem   Usage: right click -> Run as administrator.
rem          To undo: lock-gpu-clock.bat reset
rem ==========================================================================

net session >nul 2>&1
if errorlevel 1 goto needadmin

where nvidia-smi >nul 2>&1
if errorlevel 1 goto nosmi

if /i "%~1"=="reset" goto reset

echo.
echo   Current:
nvidia-smi --query-gpu=name,clocks.sm,clocks.max.sm,power.limit --format=csv
echo.
echo   Enabling persistence mode and locking clocks (min 1800MHz) ...
nvidia-smi -pm 1 >nul 2>&1
set MAXCLK=2100
for /f "tokens=1 delims=." %%a in ('nvidia-smi --query-gpu^=clocks.max.sm --format^=csv,noheader,nounits') do set MAXCLK=%%a
nvidia-smi -lgc 1800,%MAXCLK%
if errorlevel 1 goto lockfail
echo.
echo   Locked to 1800-%MAXCLK% MHz.  Undo with:  lock-gpu-clock.bat reset
echo.
pause
exit /b 0

:lockfail
echo.
echo   Lock failed. Use NVIDIA Control Panel instead:
echo     Manage 3D settings -^> Power management mode -^> Prefer maximum performance
echo.
pause
exit /b 1

:needadmin
echo.
echo   Administrator rights required: right click this file, Run as administrator.
echo.
pause
exit /b 1

:nosmi
echo.
echo   nvidia-smi not found. Check that the NVIDIA driver is installed.
echo.
pause
exit /b 1

:reset
echo.
echo   Restoring auto clocks and disabling persistence mode ...
nvidia-smi -rgc
nvidia-smi -pm 0 >nul 2>&1
echo   Done.
echo.
pause
exit /b 0
