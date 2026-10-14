@echo off
setlocal
for /f "tokens=2 delims==" %%%%a in ('findstr /b /i "%(tool)s_bin=" "%%~dp0shim.cfg"') do set T=%%%%a
if not defined T echo DevBox: %(tool)s 未选择版本，请先在面板中切换。& exit /b 1
set T=%%T:"=%%
rem 同步当前选中的 JDK 到 JAVA_HOME，使 mvn 等依赖 JAVA_HOME 的子进程使用同一版本
for /f "tokens=2 delims==" %%%%a in ('findstr /b /i "java_home=" "%%~dp0shim.cfg"') do set JH=%%%%a
if defined JH set JH=%%JH:"=%%
if defined JH set "JAVA_HOME=%%JH%%"
if defined JH set "PATH=%%JH%%\bin;%%PATH%%"
if not exist "%%T%%\%(exe)s" echo DevBox: 目标不存在 %%T%%\%(exe)s & exit /b 2
"%%T%%\%(exe)s" %%*
