@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "SCRIPT_DIR=%~dp0"
set "PY_SCRIPT=%SCRIPT_DIR%tools\import_soundboard_cue.py"
set "INTERACTIVE_MODE=1"

if "%~1"=="" goto :python_check
set "FIRST_ARG=%~1"
if "%FIRST_ARG:~0,1%"=="-" set "INTERACTIVE_MODE="

:python_check

call :resolve_python
if errorlevel 1 goto :exit_now

if not defined INTERACTIVE_MODE goto :passthrough

set "AUDIO_PATH="
set "MEDIA_PATH="
if not "%~1"=="" call :assign_prefill "%~f1"
if not "%~2"=="" call :assign_prefill "%~f2"

echo.
echo ========================================
echo Soundboard Cue Import
echo ========================================
echo.
echo Drag media or audio files onto this BAT to prefill the paths.
echo If your media is a video with built-in audio, you can leave Audio blank.
echo Run "%~nx0 --help" for advanced CLI usage.
echo.

:prompt_media
if defined MEDIA_PATH if not exist "%MEDIA_PATH%" (
    echo Media file not found: "%MEDIA_PATH%"
    set "MEDIA_PATH="
)
if defined MEDIA_PATH echo Prefilled media: "%MEDIA_PATH%"
set "MEDIA_INPUT="
set /p "MEDIA_INPUT=Media path (optional gif/image/video, Enter keeps current): "
if defined MEDIA_INPUT set "MEDIA_PATH=%MEDIA_INPUT%"
if defined MEDIA_PATH if not exist "%MEDIA_PATH%" goto :prompt_media

:prompt_audio
if defined AUDIO_PATH if not exist "%AUDIO_PATH%" (
    echo Audio file not found: "%AUDIO_PATH%"
    set "AUDIO_PATH="
)
if defined AUDIO_PATH echo Prefilled audio: "%AUDIO_PATH%"
set "AUDIO_INPUT="
set /p "AUDIO_INPUT=Audio file path (optional, Enter keeps current): "
if defined AUDIO_INPUT set "AUDIO_PATH=%AUDIO_INPUT%"
if defined AUDIO_PATH if not exist "%AUDIO_PATH%" goto :prompt_audio
if not defined AUDIO_PATH if not defined MEDIA_PATH (
    echo Please provide at least one audio or media file.
    echo.
    goto :prompt_media
)

:prompt_cue
set /p "CUE_ID=Cue id (letters/numbers/_/-): "
if not defined CUE_ID goto :prompt_cue

set /p "LABEL=Label (blank = use cue id): "

:prompt_hint
set /p "USAGE_HINT=Usage hint for MaiBot: "
if not defined USAGE_HINT goto :prompt_hint

:prompt_keywords
set /p "KEYWORDS=Keywords (comma-separated, optional): "

:prompt_volume
if not defined VOLUME set "VOLUME=1.0"
echo Current cue volume multiplier: %VOLUME%
set "VOLUME_INPUT="
set /p "VOLUME_INPUT=Per-cue volume multiplier (0.0-2.0, Enter keeps current): "
if defined VOLUME_INPUT set "VOLUME=%VOLUME_INPUT%"
if not defined VOLUME set "VOLUME=1.0"

echo.
echo Importing cue...
call :run_interactive
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo Import finished.
    echo.
    echo Cue assets were copied into data\soundboard\cues\%CUE_ID%\
    echo Cue config updated: data\soundboard\cues\%CUE_ID%\cue.toml
    echo.
    echo Remember to refresh the soundboard WebUI if it is already open.
    echo.
    pause
    exit /b 0
)

echo Import failed with exit code %EXIT_CODE%.
echo.
pause
exit /b %EXIT_CODE%

:passthrough
"%PYTHON_EXE%" %PYTHON_EXTRA% "%PY_SCRIPT%" %*
exit /b %ERRORLEVEL%

:run_interactive
powershell -NoProfile -ExecutionPolicy Bypass -Command "$py = $env:PYTHON_EXE; $extra = @(); if ($env:PYTHON_EXTRA) { $extra += $env:PYTHON_EXTRA }; $args = @($env:PY_SCRIPT, '--cue-id', $env:CUE_ID, '--label', $env:LABEL, '--usage-hint', $env:USAGE_HINT, '--keywords', $env:KEYWORDS, '--volume', $env:VOLUME); if ($env:AUDIO_PATH) { $args += @('--audio', $env:AUDIO_PATH) }; if ($env:MEDIA_PATH) { $args += @('--media', $env:MEDIA_PATH) }; & $py @extra @args; exit $LASTEXITCODE"
exit /b %ERRORLEVEL%

:assign_prefill
set "PREFILL_PATH=%~1"
set "PREFILL_EXT=%~x1"
if /I "%PREFILL_EXT%"==".mp4" goto :assign_media
if /I "%PREFILL_EXT%"==".webm" goto :assign_media
if /I "%PREFILL_EXT%"==".mov" goto :assign_media
if /I "%PREFILL_EXT%"==".m4v" goto :assign_media
if /I "%PREFILL_EXT%"==".gif" goto :assign_media
if /I "%PREFILL_EXT%"==".png" goto :assign_media
if /I "%PREFILL_EXT%"==".jpg" goto :assign_media
if /I "%PREFILL_EXT%"==".jpeg" goto :assign_media
if /I "%PREFILL_EXT%"==".webp" goto :assign_media
if /I "%PREFILL_EXT%"==".avif" goto :assign_media
if not defined AUDIO_PATH set "AUDIO_PATH=%PREFILL_PATH%"
exit /b 0

:assign_media
if not defined MEDIA_PATH (
    set "MEDIA_PATH=%PREFILL_PATH%"
) else if not defined AUDIO_PATH (
    set "AUDIO_PATH=%PREFILL_PATH%"
)
exit /b 0

:resolve_python
set "PYTHON_EXE="
set "PYTHON_EXTRA="
for /f "delims=" %%I in ('where py 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%~fI"
if defined PYTHON_EXE (
    set "PYTHON_EXTRA=-3"
    exit /b 0
)
for /f "delims=" %%I in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%~fI"
if defined PYTHON_EXE exit /b 0

echo Could not find py.exe or python.exe on PATH.
echo Install Python first, or run tools\import_soundboard_cue.py manually.
exit /b 1

:exit_now
if defined INTERACTIVE_MODE pause
exit /b 1
