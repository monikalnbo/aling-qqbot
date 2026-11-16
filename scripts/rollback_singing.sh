#!/usr/bin/env bash
# 一条命令回滚「唱歌模式」（插件 1.3.0 → 1.2.1）。
#
# 做的事：
#   1) 从 backup/singing_20261116_185547.tar.gz 解出改动前的插件文件；
#   2) 覆盖仓库里的 plugins/astrbot_plugin_aling_voice/ 和运行中的
#      astrbot/data/astrbot/plugins/astrbot_plugin_aling_voice/，并删掉 sing.py；
#   3) 还原插件配置（含唱歌前的状态）与 state.json；
#   4) 重启 AstrBot，打印插件版本确认。
#
# 用法：
#   bash scripts/rollback_singing.sh              # 正常回滚并重启
#   bash scripts/rollback_singing.sh --no-restart # 只换文件，不重启
#   bash scripts/rollback_singing.sh --root /path/to/repo --no-restart   # 演练用
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
RESTART=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-restart) RESTART=0 ;;
    --root) ROOT=$2; shift ;;
    -h|--help) sed -n 2,17p "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done

BACKUP="$ROOT/backup/singing_20261116_185547.tar.gz"
SRC="$ROOT/plugins/astrbot_plugin_aling_voice"
RUNTIME="$ROOT/astrbot/data/astrbot/plugins/astrbot_plugin_aling_voice"
CONF="$ROOT/astrbot/data/astrbot/config/astrbot_plugin_aling_voice_config.json"
STATE_DIR="$ROOT/astrbot/data/astrbot/plugin_data/astrbot_plugin_aling_voice"

[ -f "$BACKUP" ] || { echo "找不到备份：$BACKUP"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$BACKUP" -C "$TMP" --strip-components=1
echo "[1/4] 已解出备份到临时目录"

for f in main.py _conf_schema.json metadata.yaml; do
  [ -f "$TMP/$f" ] && cp -a "$TMP/$f" "$SRC/$f"
  [ -f "$TMP/installed_plugin/$f" ] && cp -a "$TMP/installed_plugin/$f" "$RUNTIME/$f"
done
rm -f "$SRC/sing.py" "$RUNTIME/sing.py"
rm -rf "$SRC/__pycache__" "$RUNTIME/__pycache__"
echo "[2/4] 插件代码已还原 1.2.1（sing.py 已移除）"

if [ -f "$TMP/voice_config.json" ]; then
  cp -a "$TMP/voice_config.json" "$CONF"
  chmod 600 "$CONF"
fi
mkdir -p "$STATE_DIR"
[ -f "$TMP/plugin_data/state.json" ] && cp -a "$TMP/plugin_data/state.json" "$STATE_DIR/state.json"
echo "[3/4] 插件配置与开关状态已还原"

if [ "$RESTART" = "1" ]; then
  if [ -f "$ROOT/astrbot/compose.yml" ]; then
    docker compose -f "$ROOT/astrbot/compose.yml" restart astrbot
  else
    docker restart astrbot
  fi
  sleep 15
  echo "[4/4] 已重启，当前插件版本："
  docker logs astrbot --since 3m 2>&1 | grep -o "astrbot_plugin_aling_voice ([0-9.]*)" | tail -1 || true
else
  echo "[4/4] 跳过了重启（--no-restart）"
fi
echo "完成。唱歌功能已关闭；日常语音不受影响。"
