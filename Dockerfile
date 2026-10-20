# ChatAgent 前后端单镜像（注意：微信/钉钉连接器依赖 Windows 桌面环境，
# 容器内仅支持 mock 连接器与 API/前端；真实平台接入请在 Windows 主机运行 start.bat）
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev
COPY backend/ ./backend/
COPY --from=frontend /app/frontend/dist ./frontend/dist

WORKDIR /app/backend
ENV CHATAGENT_HOST=0.0.0.0
ENV CHATAGENT_ALLOWED_IPS=*
EXPOSE 8320
VOLUME ["/app/backend/logs"]
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8320", "--no-access-log"]
