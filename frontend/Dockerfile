# 前端：多阶段构建，产物由 nginx 托管并反代 /api 到后端
# node:22 LTS（node 20 已于 2026-04 EOL）；nginx 1.28 stable
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
