#!/bin/sh
# 入口：按 PUID/PGID 降权运行 bt169（LinuxServer.io 模式）。
#
# 为什么：NAS 上 bind mount 的数据目录属主千奇百怪，固定 UID 的容器用户
# 大概率写不进去。以 root 启动 → 建组/建用户（PUID/PGID）→ chown /data
# → gosu 降权 exec，root 只存活到 exec 之前。
#
# 用法（compose 或 docker run 注入）：
#   PUID=1000 PGID=1000  # 默认；必须为纯数字
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID$PGID" in
  *[!0-9]*) echo "entrypoint: PUID/PGID 必须是数字（得到 PUID=$PUID PGID=$PGID）" >&2; exit 1 ;;
esac

if [ "$(id -u)" != "0" ]; then
  # 已是普通用户（--user 或 rootless docker）：环境按原样放行，PUID/PGID 无效
  if [ ! -w /data ]; then
    echo "entrypoint: 当前非 root 且 /data 不可写 —— 请自行保证数据目录属主与" >&2
    echo "  容器 UID 一致（如 chown 1000 /data 或改用命名卷）；此时 PUID/PGID 不生效。" >&2
  fi
  exec "$@"
fi

# 组：PGID 已存在（常见：挂了宿主同名 GID）→ 复用；否则建 bt169 组
if ! getent group "$PGID" >/dev/null; then
  addgroup --system --gid "$PGID" bt169
fi
GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"

# 用户：同 GID 的用户已存在 → 复用；否则建 bt169 用户
if ! getent passwd "$PUID" >/dev/null; then
  adduser --system --uid "$PUID" --ingroup "$GROUP_NAME" --shell /usr/sbin/nologin bt169
fi
USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"

# 数据目录交给目标属主（个人规模图片量，秒级；每次启动幂等）
chown -R "$PUID:$PGID" /data

exec gosu "$USER_NAME:$GROUP_NAME" "$@"
