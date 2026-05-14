#!/bin/bash
# IMA 增量更新定时任务安装脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_FILE="$SCRIPT_DIR/com.ima2obsidian.update.plist"
LAUNCH_AGENT_PATH="$HOME/Library/LaunchAgents/com.ima2obsidian.update.plist"

echo "================================"
echo "IMA 增量更新定时任务安装"
echo "================================"
echo ""

# 检查 cua-driver
if ! pgrep -f "cua-driver serve" > /dev/null; then
    echo "⚠️  cua-driver daemon 未运行"
    echo "   请先启动: cua-driver serve &"
    echo ""
fi

# 复制 plist 文件
echo "安装 launchd 配置..."
cp "$PLIST_FILE" "$LAUNCH_AGENT_PATH"

# 加载 launchd 任务
echo "加载定时任务..."
launchctl unload "$LAUNCH_AGENT_PATH" 2>/dev/null || true
launchctl load "$LAUNCH_AGENT_PATH"

echo ""
echo "✅ 安装完成！"
echo ""
echo "定时任务配置:"
echo "  - 运行时间: 每天 2:00"
echo "  - 日志文件: $SCRIPT_DIR/incremental_update.log"
echo "  - 错误日志: $SCRIPT_DIR/incremental_update_error.log"
echo ""
echo "管理命令:"
echo "  启动任务: launchctl start com.ima2obsidian.update"
echo "  停止任务: launchctl stop com.ima2obsidian.update"
echo "  卸载任务: launchctl unload $LAUNCH_AGENT_PATH"
echo "  查看日志: tail -f $SCRIPT_DIR/incremental_update.log"
echo ""
echo "测试运行:"
echo "  python3 $SCRIPT_DIR/ima_incremental_update.py --dry-run"
