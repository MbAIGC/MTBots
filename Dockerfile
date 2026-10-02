# syntax=docker/dockerfile:1

# ---------- Stage 1: 静态 docker CLI + compose 插件（LDMG 原来就是这么干的） ----------
FROM alpine:3.20 AS docker-cli

ARG TARGETARCH
ARG DOCKER_VERSION=28.5.0
ARG COMPOSE_VERSION=v2.35.1

RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) CLI_ARCH=x86_64; COMPOSE_ARCH=x86_64 ;; \
        arm64) CLI_ARCH=aarch64; COMPOSE_ARCH=aarch64 ;; \
        arm)   CLI_ARCH=armhf; COMPOSE_ARCH=armv7 ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    apk add --no-cache curl; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${CLI_ARCH}/docker-${DOCKER_VERSION}.tgz" -o docker.tgz; \
    tar -xzf docker.tgz docker/docker; \
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}" -o docker-compose; \
    chmod +x docker-compose; \
    rm -f docker.tgz

# ---------- Stage 2: 运行时 ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    CONFIG_FILE=/app/data/config.json \
    LITEPAN_USERS_FILE=/app/data/litepan-users.json \
    LOG_DIR=/app/data/logs

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates openssh-client curl \
    && rm -rf /var/lib/apt/lists/*

# docker CLI 静态二进制 + compose 插件（另给一个 docker-compose 独立命令回退，兼容老环境）
COPY --from=docker-cli /docker/docker /usr/local/bin/docker
COPY --from=docker-cli /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
RUN ln -s /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mtbots ./mtbots
COPY README.md ./
# 多主机接入向导与示例（容器内可直接跑：docker compose exec mtbots sh /app/scripts/setup-remote-host.sh）
COPY scripts ./scripts
COPY docs/examples ./docs/examples

# 非 root 运行（uid 固定 10001，方便把宿主机挂载目录 chown 给它）
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin mtbots \
    && mkdir -p /app/data \
    && chown -R mtbots:mtbots /app
USER mtbots

VOLUME ["/app/data"]
# 用法：docker run ... mtbots --check / --health / 默认启动
ENTRYPOINT ["python", "-m", "mtbots"]
CMD []
