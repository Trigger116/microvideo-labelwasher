@echo off
chcp 65001 >nul
REM ============ microvideo-labelwasher 打包脚本 ============
REM 产物: dist\microvideo-labelwasher\microvideo-labelwasher.exe (onedir)
REM 使用说明: 双击 exe 启动; workspaces/ 与 logs/ 生成在 exe 同级目录
cd /d "%~dp0"

echo [1/3] 清理旧产物...
if exist build rmdir /s /q build
if exist dist\microvideo-labelwasher rmdir /s /q dist\microvideo-labelwasher

echo [2/3] PyInstaller 打包...
python -m PyInstaller microvideo-labelwasher.spec --noconfirm
if errorlevel 1 (
    echo 打包失败!
    pause
    exit /b 1
)

echo [3/3] 生成发布包...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\microvideo-labelwasher' -DestinationPath 'dist\microvideo-labelwasher.zip' -Force"

echo.
echo ============================================
echo 打包完成: dist\microvideo-labelwasher\microvideo-labelwasher.exe
echo 发布包:   dist\microvideo-labelwasher.zip
echo ============================================
pause
