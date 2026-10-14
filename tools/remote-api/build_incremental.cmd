@echo off
rem Incremental rebuild of the slicer: builds and installs into build\ElegooSlicer WITHOUT
rem re-running CMake's configure step. build_release_windows.bat slicer always configures
rem again, which regenerates headers and recompiles everything (~15 min); after a change to a
rem few .cpp files this takes a few minutes instead.
rem Needs a build\ tree configured once by "tools\remote-api\build.cmd slicer". Close the test app first
rem (the install step overwrites ElegooSlicer.dll). Same toolchain, priority and affinity
rem variables as tools\remote-api\build.cmd (BUILD_PRIORITY, BUILD_AFFINITY).
set "NoDefaultCurrentDirectoryInExePath="

if not defined VS_BUILDTOOLS set "VS_BUILDTOOLS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
if not defined PERL_BIN set "PERL_BIN=%~dp0..\..\..\tools\strawberry-perl-5.42.3.1\perl\bin"
for %%I in ("%~dp0..\..") do set "REPO=%%~fI"

call "%VS_BUILDTOOLS%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
    echo [build_incremental.cmd] vcvars64.bat failed
    exit /b 1
)
set "PATH=%VS_BUILDTOOLS%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PERL_BIN%;%PATH%"

if not defined BUILD_AFFINITY set "BUILD_AFFINITY=0xFFFFFF"
if not defined BUILD_PRIORITY set "BUILD_PRIORITY=belownormal"
echo [build_incremental.cmd] start: %DATE% %TIME% (priority %BUILD_PRIORITY%, affinity %BUILD_AFFINITY%)

cd /d "%REPO%\build"
start "" /b /wait /%BUILD_PRIORITY% /affinity %BUILD_AFFINITY% cmd /c "cmake --build . --config Release --target ALL_BUILD -- -m && cmake --build . --target install --config Release"
set "RC=%ERRORLEVEL%"
echo [build_incremental.cmd] end: %DATE% %TIME% (exit code %RC%)
exit /b %RC%
