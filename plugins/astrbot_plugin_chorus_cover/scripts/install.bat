@echo off
chcp 936 >nul
title RVC 翻唱插件 - 一键安装
cd /d "%~dp0.."
echo.
echo  正在打开安装器，请不要关这个窗口...
echo.
powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0installer.ps1" %*
echo.
pause
