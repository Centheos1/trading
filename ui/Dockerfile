# UI service — React/WebGL frontend served via nginx.
#
# Two-stage build:
#   1. node:22-alpine — install deps, build static bundle with Vite
#   2. nginx:alpine   — serve the static bundle with a reverse-proxy to
#                       the strategy service for /api and /stream
#
# Build:
#   docker build -t trading-ui ui/
#
# Local dev uses ``docker compose up`` which runs the Vite dev server
# (see docker-compose.yml's UI service override).

# ---------------------------------------------------------------------------
# Stage 1 — build
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* tsconfig.json vite.config.ts \
     index.html ./
RUN npm install

COPY src/ ./src/
COPY public/ ./public/

ENV NODE_ENV=production
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM nginx:alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# nginx default CMD is fine.
