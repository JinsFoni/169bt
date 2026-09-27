# 169bt 归档台 —— 生产镜像
#
# 本地构建：docker build -t 169bt .
# compose： docker compose up -d   （PUID/PGID/时区见 docker-compose.yml）
#
# 设计要点：
#   - ``pip install .`` 装包（migrations 随包）；前端 ui/ 在包外，单独
#     COPY 到 /app/ui 并用 BT169_UI_DIR 指向（config.py 的环境变量开关，
#     与 BT169_DATA_DIR 同款模式）。
#   - 数据（SQLite + 图片）一律经 BT169_DATA_DIR 落到 /data 卷；绝不上
#     下文里带 data/（.dockerignore 已排除——真实库里有加密密钥）。
#   - PUID/PGID 权限模式：以 root 启动 → docker/entrypoint.sh 按环境
#     变量建用户/组并 chown /data → gosu 降权 exec。bind mount 到 NAS
#     上任意属主的目录都能读写（docker-compose.yml 里改数字即可）。
#   - 镜像内无构建工具链（slim + 纯运行时依赖）。

FROM python:3.14-slim

LABEL org.opencontainers.image.title="169bt 归档台" \
      org.opencontainers.image.description="个人自用的 4K 帖归档与 ED2K 提取" \
      org.opencontainers.image.source="https://github.com/JinsFoni/169bt" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BT169_DATA_DIR=/data \
    BT169_UI_DIR=/app/ui \
    PUID=1000 \
    PGID=1000 \
    TZ=UTC

WORKDIR /app

# ① 先只拷 pyproject + 源码树装依赖层（改动 pyproject 才会失效缓存）
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir . \
    && rm -rf /root/.cache

# ② 前端静态资源（包外目录，config.BT169_UI_DIR 指向这里）
COPY ui/ ./ui/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# /data 预建：首次挂**命名卷**时 Docker 继承镜像内属主（root）；entrypoint
# 每次 chown 到 PUID:PGID，命名卷/bind mount 两种场景都正确。
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8899/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bt169", "serve", "--host", "0.0.0.0", "--port", "8899"]
