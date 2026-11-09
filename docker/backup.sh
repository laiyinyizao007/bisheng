#!/usr/bin/env bash
# BISHENG 本地热备份。
#
# 用法：./backup.sh                      备份到 <项目外层>/backups/<时间戳>/，保留最近 7 份
#       KEEP=14 ./backup.sh              自定义保留份数
#       WITH_ENV=0 ./backup.sh           不备份 .env（默认备份，见下）
#       BACKUP_ROOT=/d/bk ./backup.sh    自定义备份根目录
#
# 备份内容（每份一个目录）：
#   mysql.sql.gz          MySQL 逻辑备份（bisheng + openfga 两个库）
#   minio-data.tar.gz     对象存储文件（知识库原文件、技能包、任务产出）
#   own-files.tar.gz      我们自己加的文件，清单见 own-files.list
#   official-config.tar.gz 官方配置的当前副本（compose、config.yaml、nginx 等），方便还原到同一版本
#   project-docs.tar.gz   外层 docs/ 笔记
#   host-config.tar.gz    项目外的机器级改动：.wslconfig、Docker 的 daemon.json 与 settings-store.json
#   env.backup            .env 的原样副本（含密钥！）。WITH_ENV=0 可关闭
#
# 备份存在项目外层目录而不是 bisheng/docker/ 里：bisheng/ 是官方仓库内容，
# 更新或重新拉取时可能被整体覆盖，放在里面的备份会和被保护的东西一起消失。
#
# 不包含（热备份无法保证一致性，需停机冷备）：Milvus / Elasticsearch 索引数据、etcd。
# 索引可由 MinIO 里的原文件重建，真正不能丢的是 MinIO 原文件 + MySQL。
#
# 恢复自有文件：  tar -xzf <备份>/own-files.tar.gz -C bisheng/docker/
# 恢复 MySQL：     zcat <备份>/mysql.sql.gz | docker exec -i -e MYSQL_PWD=<root口令> bisheng-mysql mysql -uroot
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
KEEP="${KEEP:-7}"
WITH_ENV="${WITH_ENV:-1}"
OUTER="$(cd ../.. && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-${OUTER}/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${BACKUP_ROOT}/${STAMP}"
mkdir -p "${DEST}"

echo "备份目录：${DEST}"

echo "[1/6] MySQL 备份 (bisheng + openfga) ..."
# MySQL root 口令与 docker-compose.yml 中 mysql 服务一致（官方默认值）
docker exec -e MYSQL_PWD=1234 bisheng-mysql \
  mysqldump -uroot --single-transaction --routines --triggers --databases bisheng openfga \
  2>/dev/null | gzip > "${DEST}/mysql.sql.gz"

echo "[2/6] 对象存储 (知识库原文件/图片) ..."
# bisheng 后端与 Milvus 共用 ./data/milvus-minio 这个 MinIO
tar -czf "${DEST}/minio-data.tar.gz" -C data milvus-minio

echo "[3/6] 自有文件 (own-files.list) ..."
# 取清单里的有效行；清单里列出的路径必须都存在，否则报错（避免漏备份却不知道）
mapfile -t OWN < <(grep -vE '^\s*(#|$)' own-files.list | sed 's/\r$//')
for p in "${OWN[@]}"; do
  [ -e "$p" ] || { echo "[ERROR] own-files.list 里的 '$p' 不存在" >&2; rm -rf "${DEST}"; exit 1; }
done
tar -czf "${DEST}/own-files.tar.gz" "${OWN[@]}"

echo "[4/6] 官方配置副本 + 项目笔记 ..."
tar -czf "${DEST}/official-config.tar.gz" docker-compose.yml bisheng/config bisheng/entrypoint.sh nginx redis mysql/conf deploy.sh
if [ -d "${OUTER}/docs" ]; then
  tar -czf "${DEST}/project-docs.tar.gz" -C "${OUTER}" docs
fi

echo "[5/6] 机器级配置 (.wslconfig / Docker 设置) ..."
APPDATA_U=""
if [ -n "${APPDATA:-}" ] && command -v cygpath >/dev/null 2>&1; then APPDATA_U="$(cygpath -u "$APPDATA")"; fi
HOST_FILES=()
for f in "${HOME}/.wslconfig" "${HOME}/.wslconfig".bak-* \
         "${HOME}/.docker/daemon.json" "${HOME}/.docker/daemon.json".bak-* \
         "${APPDATA_U}/Docker/settings-store.json" "${APPDATA_U}/Docker/settings-store.json".bak-*; do
  [ -f "$f" ] && HOST_FILES+=("${f#/}")
done
if [ "${#HOST_FILES[@]}" -gt 0 ]; then
  tar -czf "${DEST}/host-config.tar.gz" -C / "${HOST_FILES[@]}"
fi

echo "[6/6] .env ..."
if [ "${WITH_ENV}" = "1" ] && [ -f .env ]; then
  cp .env "${DEST}/env.backup"
  chmod 600 "${DEST}/env.backup" 2>/dev/null || true   # NTFS 上 chmod 基本无效，靠目录位置保护
  echo "      已备份 .env（含密钥）。不要把 ${BACKUP_ROOT} 同步到云盘或发给别人。"
else
  echo "      已跳过 .env"
fi

# ---- 校验：任何一项不通过就删除这份备份并报错 ----
fail() { echo "[ERROR] 备份校验失败：$1，已删除 ${DEST}" >&2; rm -rf "${DEST}"; exit 1; }

# 注意：这里不能用 `grep -q`，pipefail 下它提前退出会让上游收到 SIGPIPE，造成误报
DB_COUNT="$(zcat "${DEST}/mysql.sql.gz" | grep -c '^CREATE DATABASE' || true)"
gzip -t "${DEST}/mysql.sql.gz" || fail "mysql.sql.gz 无法解压"
[ "${DB_COUNT}" -ge 2 ] || fail "mysql 建库语句 ${DB_COUNT}/2"

OWN_LIST="$(tar -tzf "${DEST}/own-files.tar.gz")"
for must in docker-compose.local.yml backup.sh paddleocr-shim/shim.py own-files.list; do
  printf '%s\n' "${OWN_LIST}" | grep -cxF "${must}" >/dev/null || fail "own-files.tar.gz 缺少 ${must}"
done

if [ -f "${DEST}/env.backup" ]; then
  cmp -s .env "${DEST}/env.backup" || fail ".env 副本与原文件不一致"
fi

# 只保留最近 KEEP 份（只清理时间戳命名的目录）
ls -1d "${BACKUP_ROOT}"/[0-9]*-[0-9]*/ 2>/dev/null | sort | head -n "-${KEEP}" | xargs -r rm -rf

echo "完成：${DEST}"
du -sh "${DEST}"/* | sed 's/^/  /'
