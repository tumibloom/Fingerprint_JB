@echo off
title jetbrainsreg Launcher
echo 正在启动 jetbrainsreg 模块...

:: 执行 python -m jetbrainsreg
py -m jetbrainsreg

:: 检查执行是否出错
if errorlevel 1 (
    echo 执行 jetbrainsreg 时发生错误（错误代码: %errorlevel%）
) else (
    echo jetbrainsreg 执行完成。
)

:: 暂停，以便用户查看输出
pause