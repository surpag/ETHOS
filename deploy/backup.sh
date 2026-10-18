#!/bin/sh
# ETHOS 每日备份：热备 SQLite + 打包整个数据卷，默认保留 14 天。
#
# 用法：
#   chmod +x deploy/backup.sh
#   ./deploy/backup.sh                      # 手动跑一次
#   echo '0 3 * * * root /opt/ethos/ETHOS/deploy/backup.sh' > /etc/cron.d/ethos-backup
#
# 可用环境变量覆盖：
#   BACKUP_DIR      备份落盘目录，默认 /opt/backup
#   RETENTION_DAYS  保留天数，默认 14
#   VOLUME          数据卷名，默认 ethos_edith-data（= <compose name>_<volume key>）
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BACKUP_DIR=${BACKUP_DIR:-/opt/backup}
RETENTION_DAYS=${RETENTION_DAYS:-14}
VOLUME=${VOLUME:-ethos_edith-data}

cd "$PROJECT_DIR"
mkdir -p "$BACKUP_DIR"

if [ -z "$(docker compose ps -q backend 2>/dev/null)" ]; then
  echo "[backup] backend 容器未运行，跳过本次备份" >&2
  exit 1
fi

# 1) 热备 SQLite。backend 镜像基于 python:3.13-slim，没有 sqlite3 CLI，
#    所以用 Python 的 backup API —— 它走 SQLite 在线备份协议，能包含
#    WAL 里尚未 checkpoint 的数据（直接 cp 那个 .db 会丢掉最近的写入）。
docker compose exec -T backend python -c \
  "import sqlite3; s=sqlite3.connect('/app/data/edith-v2.db'); d=sqlite3.connect('/app/data/.snapshot.db'); s.backup(d); d.close(); s.close()"

# 2) 打包整个数据卷：DB 快照 + workspace-volumes + workspace-sync
docker run --rm -v "$VOLUME:/data" -v "$BACKUP_DIR:/backup" alpine \
  sh -c "cd /data && tar czf /backup/ethos-\$(date +%F_%H%M).tar.gz . && rm -f /data/.snapshot.db"

# 3) 清理过期备份
find "$BACKUP_DIR" -name 'ethos-*.tar.gz' -mtime +"$RETENTION_DAYS" -delete

echo "[backup] done -> $BACKUP_DIR"
