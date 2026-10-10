@echo off
chcp 65001 >nul
rem 双击启动足球分析工具：后台接口（端口 8787）+ 网页界面（端口 5173），并自动打开浏览器。
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 没有找到 Python 虚拟环境 .venv，请先按 README 创建。
  pause & exit /b 1
)
if not exist "web\node_modules" (
  echo 第一次运行，正在安装网页依赖……
  pushd web & call npm install --no-fund --no-audit & popd
)
start "足球分析工具-后台接口" /min cmd /k "cd /d "%~dp0api" && ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8787"
start "足球分析工具-网页界面" /min cmd /k "cd /d "%~dp0web" && npm run dev"
echo 正在等待服务启动……
powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest http://127.0.0.1:5173 -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0}catch{Start-Sleep 1}}; exit 1"
if errorlevel 1 (
  echo 60 秒内网页没有启动成功，请查看任务栏里两个最小化窗口中的报错。
  pause & exit /b 1
)
start "" http://127.0.0.1:5173
echo 已打开浏览器。要关闭工具，请双击 stop.bat。
timeout /t 3 >nul