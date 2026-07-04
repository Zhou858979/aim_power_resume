#!/bin/bash
# install.sh - 一键部署断电续打插件
# 自动检测 Klipper 路径，复制文件到 extras 根目录，重启服务

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
EXTRAS_DIR="$HOME/klipper/klippy/extras"
LOG_FILE="$HOME/printer_data/logs/klippy.log"

echo "=== AIM Power Resume Deploy ==="

# 1. 自动检测 Klipper 路径
if [ ! -d "$EXTRAS_DIR" ]; then
    echo "ERROR: Klipper extras not found at $EXTRAS_DIR"
    echo "Please install Klipper first."
    exit 1
fi

# 2. 复制 .so + 壳文件到 extras 根目录
echo "[1/2] Copying to $EXTRAS_DIR ..."

# 复制 .so 文件
cp "$REPO_DIR"/aim_power_resume*.so "$EXTRAS_DIR/" 2>/dev/null || true

# 复制壳文件
cp "$REPO_DIR"/aim_power_resume.py "$EXTRAS_DIR/"

# 3. 清理缓存
echo "[2/2] Cleaning cache..."
rm -rf "$EXTRAS_DIR"/__pycache__/

# 4. 重启 Klipper
echo "Restarting Klipper..."
sudo systemctl restart klipper

# 5. 验证
sleep 3
if [ -f "$EXTRAS_DIR/aim_power_resume.cpython-310-aarch64-linux-gnu.so" ]; then
    echo ""
    echo "✅ Deploy successful!"
else
    echo ""
    echo "⚠️  Check logs: tail -n 20 $LOG_FILE"
fi

echo ""
echo "=== Done ==="
echo "Files in extras:"
ls -la "$EXTRAS_DIR"/aim_power_resume*.so "$EXTRAS_DIR"/aim_power_resume*.py 2>/dev/null || true