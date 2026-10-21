# syntax=docker/dockerfile:1
# 生产镜像：前端构建产物 + Python Agent 运行时
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# 图片源可选：把 VITE_DIANPING_ASSET_ORIGIN 指向 go-api 的公网域名（Railway 无共享卷时使用）。
ARG VITE_DIANPING_ASSET_ORIGIN=""
ENV VITE_DIANPING_ASSET_ORIGIN=$VITE_DIANPING_ASSET_ORIGIN
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY xiangkou ./xiangkou
COPY scripts ./scripts
COPY --from=web /web/dist ./frontend/dist
ENV XIANGKOU_DATA_PATH=/data/xiangkou.sqlite3
RUN mkdir -p /data
EXPOSE 8090
# 单 worker：Agent 的会话/取消状态在进程内；反代后靠 --proxy-headers 还原 https/origin
# 端口优先取平台注入的 $PORT（Railway/Render 等），本地默认 8090。
CMD ["sh", "-c", "exec python -m uvicorn xiangkou.api:app --host 0.0.0.0 --port ${PORT:-8090} --proxy-headers --forwarded-allow-ips=*"]
