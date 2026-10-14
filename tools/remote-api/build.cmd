@echo off
rem Build wrapper for ElegooSlicer MCP (see PORT_NOTES.md, section 7, and README.md here).
rem Sets up the toolchain Elegoo expects, then forwards all arguments to
rem build_release_windows.bat, e.g.:  build.cmd onlydeps   |   build.cmd dlweb slicer
rem
rem Why each piece:
rem  - VS 2022 Build Tools (v143): Elegoo's scripts and README target VS 2022.
rem  - CMake 3.31.6 bundled with the Build Tools: Elegoo's README asks for CMake < 4.0 on Windows,
rem    and the system CMake on PATH is 4.3.3. vcvars64 only APPENDS its CMake, so we prepend it.
rem  - Strawberry Perl (portable, perl\bin only): needed by the OpenSSL dependency.
rem    Never add Strawberry's c\bin (CMakeLists.txt refuses it before CMake on PATH).

rem The Claude Code shell sets NoDefaultCurrentDirectoryInExePath=1, which stops cmd from
rem running programs from the current directory. The Elegoo script and several deps
rem (vcvars' vswhere, Boost bootstrap, ...) rely on that, so clear it for this process only.
set "NoDefaultCurrentDirectoryInExePath="

rem Defaults: the Build Tools' standard location, and the portable Perl in the workspace's tools\
rem folder next to this checkout. Set VS_BUILDTOOLS or PERL_BIN first to use other locations.
if not defined VS_BUILDTOOLS set "VS_BUILDTOOLS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
if not defined PERL_BIN set "PERL_BIN=%~dp0..\..\..\tools\strawberry-perl-5.42.3.1\perl\bin"
for %%I in ("%~dp0..\..") do set "REPO=%%~fI"

call "%VS_BUILDTOOLS%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
    echo [build.cmd] vcvars64.bat failed
    exit /b 1
)
set "PATH=%VS_BUILDTOOLS%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PERL_BIN%;%PATH%"

echo [build.cmd] toolchain:
cmake --version | findstr /b "cmake"
perl -e "print qq{perl $^V\n}"
where cl
echo [build.cmd] start: %DATE% %TIME%

rem Victor works on this machine while it builds: run the whole build at below-normal priority
rem and pinned to the first 24 logical CPUs (12 physical cores), leaving 4 cores free.
rem Child processes (MSBuild nodes, cl.exe) inherit both. Override with e.g.
rem   set BUILD_AFFINITY=0xFFFF       (16 threads, when the machine is busy)
rem   set BUILD_AFFINITY=0xFFFFFFFF   (all 32 threads, e.g. overnight)
rem   set BUILD_PRIORITY=normal        (instead of belownormal, when nobody is using the machine)
if not defined BUILD_AFFINITY set "BUILD_AFFINITY=0xFFFFFF"
if not defined BUILD_PRIORITY set "BUILD_PRIORITY=belownormal"
echo [build.cmd] priority %BUILD_PRIORITY%, affinity %BUILD_AFFINITY%

cd /d "%REPO%"
start "" /b /wait /%BUILD_PRIORITY% /affinity %BUILD_AFFINITY% cmd /c build_release_windows.bat %*
set "RC=%ERRORLEVEL%"
echo [build.cmd] end: %DATE% %TIME% (exit code %RC%)
exit /b %RC%
