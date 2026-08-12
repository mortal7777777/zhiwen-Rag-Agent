# 启动后端：python run.py（监听 127.0.0.1:8000）
Set-Location (Join-Path $PSScriptRoot 'backend')

# MySQL 对话记忆：未显式设置时使用本机默认凭据
if (-not $env:MYSQL_URL) {
    $env:MYSQL_URL = "mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/rag_assistant?charset=utf8mb4"
    Write-Host "已自动设置 MYSQL_URL（root@127.0.0.1/rag_assistant）"
}

# 视觉识别（可选）：日日新 SenseNova 多模态模型
# 未设置时识图/扫描页 OCR 自动降级为不可用，其余功能不受影响。
if (-not $env:SENSENOVA_API_KEY) {
    Write-Host "提示：未检测到 SENSENOVA_API_KEY，图片识别/扫描件 OCR 已停用。"
    Write-Host "      设置方式：`$env:SENSENOVA_API_KEY='你的key' 后重新运行本脚本"
}

python run.py
