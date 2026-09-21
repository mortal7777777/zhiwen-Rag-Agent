按计划# Nginx 详解:概念、配置与项目实战

> 依据本项目 `frontend/nginx.conf`(生产反代)与 `frontend/Dockerfile`
> (多阶段构建)逐行讲解,附带完整配置语法教学与面试题。
> 本项目里 Nginx 的职责:托管 Vue 打包产物 + 把 `/api` 反向代理到 FastAPI
> 后端 + **SSE 流式特化配置**。

---

## 1. 概念:Nginx 是什么,能做什么

**Nginx = 高性能 Web 服务器 / 反向代理 / 负载均衡器**(C 语言,事件驱动,
单进程可扛十万级并发连接)。

| 能力 | 说明 |
|---|---|
| 静态文件服务 | 托管 HTML/JS/CSS/图片(本项目托管 Vue dist) |
| 反向代理 | 把请求转发给后端(本项目 `/api` → FastAPI) |
| 负载均衡 | upstream 多后端轮询/加权/一致性哈希 |
| SSL 终结 | HTTPS 证书卸载,后端跑 HTTP |
| 缓存 | 静态资源缓存、代理缓存 |
| 限流/访问控制 | 按 IP/连接数限流,白名单 |

**为什么它快**:多进程(master+worker)+ 事件驱动(epoll),worker 单线程
非阻塞处理大量连接,无"一连接一线程"的开销。

---

## 2. 配置组成:一个完整的 nginx.conf 解剖

```nginx
# 全局块:master 进程
user nginx;
worker_processes auto;              # 自动 = CPU 核数

events {
    worker_connections 1024;        # 每 worker 最大连接数
}

http {                              # http 块:所有 server 共享
    include /etc/nginx/mime.types;  # 文件类型映射
    sendfile on;                    # 零拷贝发送文件
    keepalive_timeout 65;

    server {                        # server 块:一个站点(域名/端口)
        listen 80;
        server_name _;              # 任意主机名

        root /usr/share/nginx/html; # 静态文件根
        index index.html;

        location /api/ {            # location 块:路径匹配规则
            proxy_pass http://backend:8000;
        }
        location / {
            try_files $uri $uri/ /index.html;   # SPA 路由回退
        }
    }
}
```

**块层级:全局 → events → http → server → location**,逐级嵌套,内层覆盖外层。
**location 匹配优先级**:`=`(精确)> `^~`(前缀,不再查正则)> `~`/`~*`(正则,
顺序匹配)> 普通前缀(最长匹配)。

---

## 3. 项目实战:frontend/nginx.conf 逐行讲解

### 3.1 项目中的完整配置

```nginx
server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    # Agent 流式问答是 SSE:必须关缓冲、放宽读超时,否则打字机效果失效
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;                     # SSE 需要 HTTP/1.1
        proxy_set_header Host $host;                # 透传 Host
        proxy_set_header X-Real-IP $remote_addr;    # 客户端真实 IP
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;                        # ★ 关闭缓冲,SSE 逐块转发
        proxy_cache off;                            # 动态接口不缓存
        proxy_read_timeout 3600s;                   # ★ Agent 长任务(审批等待)放宽到 1 小时
    }

    location / {
        try_files $uri $uri/ /index.html;           # Vue Router 历史模式回退
    }
}
```

### 3.2 关键决策逐条解读

| 配置 | 为什么 |
|---|---|
| `proxy_pass http://backend:8000` | Docker 内网服务名解析:frontend 容器经 compose 网络直连 backend 容器(不暴露公网) |
| `proxy_http_version 1.1` | SSE/长连接依赖 HTTP/1.1(1.0 默认关连接) |
| **`proxy_buffering off`** | 默认 Nginx 会缓冲上游响应,**SSE 事件被攒着不推** → 打字机效果失效、心跳超时。关闭后逐块透传,事件实时到浏览器 |
| `proxy_read_timeout 3600s` | Agent 工具执行/审批等待可能长达几分钟,默认 60s 超时会断流 |
| `try_files $uri $uri/ /index.html` | SPA 前端路由:直接访问 /runs 等路径时,文件不存在 → 回退 index.html 由 Vue Router 接管 |

### 3.3 前端 Dockerfile 的多阶段构建

```dockerfile
FROM node:20-alpine AS build          # 阶段 1:构建
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build                     # 产出 dist/

FROM nginx:1.27-alpine                # 阶段 2:运行(只带产物,镜像小)
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
```

**多阶段构建的好处**:最终镜像不含 node_modules(几百 MB → 几十 MB),
只有 nginx + 静态产物;`npm ci` 用 lock 文件保证依赖可复现。

---

## 4. 常用运维命令

```bash
nginx -t                    # 校验配置语法(改配置必跑)
nginx -s reload             # 平滑重载(不断连接,优雅)
nginx -s stop / quit        # 停止 / 优雅退出
# Docker 场景:
docker compose logs frontend | tail -50     # 看 nginx 日志
docker exec -it <容器名> nginx -t           # 容器内校验
```

---

## 5. 进阶主题(面试常考)

### 5.1 反向代理 vs 正向代理
- 正向代理:代理**客户端**(翻墙软件、公司上网代理);隐藏客户端;
- 反向代理:代理**服务器**(用户访问 Nginx,由它转发给后端);隐藏服务器、
  统一入口、负载均衡、SSL 终结。

### 5.2 负载均衡 upstream

```nginx
upstream backend_pool {
    least_conn;                     # 最少连接(默认轮询;ip_hash 可会话粘滞)
    server backend1:8000 weight=2;  # weight 权重
    server backend2:8000;
    server backend3:8000 backup;    # 备份节点,主节点全挂才启用
}
server {
    location /api/ { proxy_pass http://backend_pool; }
}
```

### 5.3 为什么本项目前端不能直连后端
- 浏览器 CORS 跨域 + 后端只监听 Docker 内网 → 同源反代是标准解法:
  浏览器只访问前端域名,`/api` 由 Nginx 转发,天然同源,无 CORS;
- 生产部署时公网只暴露 Nginx(80/443),后端/MySQL/OpenSearch 全部内网。

### 5.4 常见排错
| 现象 | 原因 | 处理 |
|---|---|---|
| 前端页面 502 | 后端容器没起来/没健康 | `docker compose ps`、看 backend 日志 |
| SSE 不流式、一次全出 | 忘关 buffering | `proxy_buffering off` + http 1.1 |
| 长任务中途断流 | read_timeout 太短 | 放宽 `proxy_read_timeout` |
| 404 刷新路由 | 没配 try_files 回退 | `try_files $uri $uri/ /index.html` |
| 页面样式 404 | mime.types 没 include | `include /etc/nginx/mime.types;` |

---

## 6. 面试题精讲

### Q1. Nginx 为什么能支撑高并发?
**答法**:master-worker 多进程 + **事件驱动模型**(epoll):worker 单线程
非阻塞轮询海量连接,回调处理就绪事件,无"一连接一线程"的线程开销;
加上 sendfile 零拷贝、静态文件直接内核态发送。
**加分**:对比 Apache 的进程/线程模型;提 C10K 问题就是事件驱动解决的。

### Q2. 反向代理和正向代理区别?
**答法**:正向代理代客户端(隐藏客户端、访问受限资源);反向代理代服务器
(统一入口、负载均衡、隐藏后端)。
**加分**:项目例子:Nginx 是后端集群的反向代理,浏览器视角只有一个域名。

### Q3. SSE 场景 Nginx 要配什么?为什么?
**答法**:`proxy_http_version 1.1` + `proxy_buffering off`(默认缓冲导致
事件被攒住不推)+ 放宽 `proxy_read_timeout` + 关 `proxy_cache`。
**加分**:讲清"缓冲 vs 流式"的矛盾是 SSE 反代的核心坑;本项目打字机
效果依赖这三条。

### Q4. SPA 前端路由 404 怎么解决?
**答法**:`try_files $uri $uri/ /index.html`——真实文件优先,否则回退入口
HTML 由前端路由接管。注意:API 路径要单独 location,不能一起回退
(否则 404 的 API 请求也返回 index.html,前端拿到 HTML 当 JSON 解析)。
**加分**:本项目 /api/ 与 / 是两个 location,正是这个原因。

### Q5. 负载均衡有哪些策略?
**答法**:轮询(默认)、加权轮询、least_conn(最少连接)、ip_hash(客户端 IP
哈希,会话粘滞)、url_hash(缓存友好)、一致性哈希(扩展性好)。
**加分**:区分四层(LVS/Nginx stream)与七层(Nginx http)负载均衡。

### Q6. 如何保证前端静态资源缓存与更新?
**答法**:文件名带哈希的产物(Vite 默认 contenthash)长缓存
(`Cache-Control: max-age=31536000, immutable`);index.html 不缓存或短缓存
(no-cache),保证发版后入口总能拿到新资源。
**加分**:本项目 Vite 构建产物 assets 带哈希,天然适配该策略。

---

## 速记卡

- 快的原因:多进程 + epoll 事件驱动 + sendfile;
- 块层级:http → server → location;匹配:精确 > 前缀^~ > 正则 > 最长前缀;
- SSE 三件套:http 1.1 + buffering off + read_timeout 放宽;
- SPA:try_files 回退 index.html,API 独立 location;
- 多阶段构建:build 阶段出产物,运行阶段只带 nginx+dist;
- 本项目职责:托管 Vue 产物 + /api 反代 FastAPI + SSE 特化。
