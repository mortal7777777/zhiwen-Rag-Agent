# 启动后端：python run.py（监听 127.0.0.1:8000）
# MySQL 凭据由 run.py 从 backend/.env.local 读取（gitignored，不写死在脚本里）
Set-Location (Join-Path $PSScriptRoot 'backend')

if (-not (Test-Path (Join-Path $PSScriptRoot 'backend\.env.local'))) {
    Write-Host "提示：未找到 backend\.env.local，MySQL 凭据将用默认值（可能连接失败）。"
}

# 视觉识别（可选）：日日新 SenseNova 多模态模型
# 未设置时识图/扫描页 OCR 自动降级为不可用，其余功能不受影响。
if (-not $env:SENSENOVA_API_KEY) {
    Write-Host "提示：未检测到 SENSENOVA_API_KEY，图片识别/扫描件 OCR 已停用。"
    Write-Host "      设置方式：`$env:SENSENOVA_API_KEY='你的key' 后重新运行本脚本"
}

python run.py
