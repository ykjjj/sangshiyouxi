@echo off
chcp 65001 >nul
title GitHub 一键发布 - 体素丧尸生存游戏
cd /d "%~dp0"

echo ==============================================
echo    体素丧尸生存游戏 — GitHub 创建仓库并推送
echo ==============================================
echo.

REM ===== 配置：仓库名和可见性 =====
set REPO_NAME=sangshiyouxi
set VISIBILITY=--public

echo [1/4] 检查 GitHub CLI...
where gh >nul 2>nul
if errorlevel 1 (
    echo   未安装 gh，正在通过 winget 安装...
    winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
    echo.
    echo   安装完成后请关闭本窗口并重新运行本脚本。
    pause
    exit /b 1
)
echo   OK：gh 已安装。

echo.
echo [2/4] 检查 GitHub 登录状态...
gh auth status >nul 2>nul
if errorlevel 1 (
    echo   尚未登录，请按提示完成授权：
    echo   1. 记下下方显示的一次性授权码
    echo   2. 按回车后浏览器会打开 github.com/login/device
    echo   3. 在网页中输入授权码并点击 Authorize
    echo.
    gh auth login --hostname github.com --git-protocol https --web
    if errorlevel 1 (
        echo   登录失败，请重新运行本脚本重试。
        pause
        exit /b 1
    )
)
for /f "usebackq delims=" %%i in (`gh api user -q .login 2^>nul`) do set "GH_USER=%%i"
echo   OK：已登录 github.com/%GH_USER%

echo.
echo [3/4] 创建仓库 %REPO_NAME% 并推送代码...
git remote get-url origin >nul 2>nul
if errorlevel 1 (
    gh repo create %REPO_NAME% %VISIBILITY% --source . --remote origin --push
) else (
    echo   远程 origin 已存在，直接推送...
    git push -u origin main
)
if errorlevel 1 (
    echo   推送失败：若提示仓库已存在，请修改脚本顶部的 REPO_NAME 后重试。
    pause
    exit /b 1
)

echo.
echo [4/4] 发布成功！
for /f "usebackq delims=" %%i in (`gh repo view --json url -q .url 2^>nul`) do set "REPO_URL=%%i"
echo   仓库地址：%REPO_URL%
echo.
echo   即将在浏览器中打开仓库页面...
timeout /t 2 >nul
start "" %REPO_URL%
pause
exit /b 0
