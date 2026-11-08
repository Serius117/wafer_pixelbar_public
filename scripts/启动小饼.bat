@echo off
chcp 65001 >nul
title 小饼 - wafer-pixel 全家桶
cd /d "%~dp0.."

echo ============================================
echo   启动小饼（wafer-pixel 全家桶）
echo   三件套推理 + OLV + hub/渲染/编码桥
echo   就绪后自动打开浏览器 /app 页面
echo   关闭此窗口 = 停止全部服务
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv 虚拟环境，请先完成安装（见 README / INSTALL）
    pause
    exit /b 1
)

".venv\Scripts\python.exe" scripts\dev_stack.py start %*

echo.
echo ============================================
echo   服务已全部退出。
echo ============================================
pause
