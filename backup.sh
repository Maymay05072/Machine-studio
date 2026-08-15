#!/bin/bash
# 每日备份 tech_memory.db，保留最近 7 天
# 用法：crontab 里加一行
#   0 3 * * * /opt/tech-memory/backup.sh >> /opt/tech-memory/backup.log 2>&1

BACKUP_DIR="/opt/tech-memory/backups"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d)
cp /opt/tech-memory/tech_memory.db "$BACKUP_DIR/tech_memory_$DATE.db"
# 删除 7 天前的备份
find "$BACKUP_DIR" -name "tech_memory_*.db" -mtime +7 -delete
echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup done -> tech_memory_$DATE.db"
