# Docker 精通指南（命令 → Dockerfile → Compose → 实战）

> 目标：学完达到简历要求的"熟练 Docker"——能看懂/编写 Dockerfile 与 compose、
> 会构建、调试、排错，理解镜像分层与多阶段构建。
> 结合本项目：backend 的 lite/full 双 profile Dockerfile、frontend 多阶段构建、
> 五服务 docker-compose 都是活教材。
> 方法：每节先概念再敲命令，最后做章末练习。前置：Docker Desktop 已装（本项目在用）。

---

## 1. 核心心智模型：镜像、容器、仓库

| 概念 | 类比 | 本质 |
|---|---|---|
| **镜像（Image）** | 光盘/安装包 | 只读的软件包（文件系统 + 配置 + 程序），不可变 |
| **容器（Container）** | 运行中的程序 | 镜像的"运行实例"：镜像只读层 + 一层可写层 |
| **仓库（Registry）** | 应用商店 | 存镜像的地方（Docker Hub、私有仓库） |

**一句话**：镜像是一次性构建、到处运行的软件包；容器是它的运行态。

```bash
docker images          # 本地镜像列表（你项目 backend 9GB full / frontend 79MB）
docker ps              # 运行中的容器
docker ps -a           # 全部容器（含已停止）
```

**镜像分层（面试必问）**：镜像由一层层只读层组成（每层 = Dockerfile 的一条指令）。
构建时**有层缓存**（层没变直接复用——你 rebuild backend 20 秒完成就是缓存命中）；
运行时所有只读层 + 一层可写层组成容器文件系统。

## 2. 容器生命周期命令（天天用）

```bash
# 创建并运行
docker run -d --name myapp -p 8000:8000 nginx
#   -d 后台  --name 命名  -p 宿主机:容器端口

docker ps                       # 运行中
docker logs -f myapp            # 看日志（-f 跟踪）
docker exec -it myapp bash      # 进容器（调试神器）
docker exec myapp python -c "..."   # 容器内执行命令（不回交互）
docker stop myapp && docker start myapp   # 停/启（保留容器状态）
docker rm myapp                 # 删容器（先停）
docker rm -f myapp              # 强制删
docker rmi nginx                # 删镜像
docker inspect myapp            # 查看容器配置/状态（JSON）
docker stats                    # 实时资源占用（内存/CPU）
```

**核心认知**：`docker exec` 是排错第一工具——容器出问题先进去手动跑命令复现。

## 3. 端口、环境变量、卷

### 3.1 端口映射

```bash
docker run -d -p 8000:8000 backend    # 宿主机 8000 → 容器 8000
docker run -d -p 127.0.0.1:5173:80 frontend  # 只绑本机回环（更安全）
```

> 你踩过的坑：本机 uvicorn 和容器同时想占 8000 → `bind 10048`。
> 排查：`docker ps` 先看是不是容器占着端口。

### 3.2 环境变量（配置注入的正确姿势——密钥绝不进镜像）

```bash
docker run -e DEEPSEEK_API_KEY=sk-xxx backend   # 单变量
docker run --env-file .env backend              # 从文件注入（compose 的做法）
```

> 密钥只通过环境变量/`.env` 注入，**永不写进 Dockerfile/镜像**——镜像会被分发，
> `docker history` 能翻出所有层里的明文。

### 3.3 卷（数据持久化的唯一方式）

```bash
docker run -v /宿主机绝对路径:/容器内路径   # bind mount：宿主机目录直接挂
docker run -v myvolume:/app/data          # 命名卷：docker 管理，路径在虚拟机里
docker volume ls
```

| 类型 | 位置 | 用途 | 本项目 |
|---|---|---|---|
| bind mount | 宿主机指定路径 | 开发/传文件 | full 模式挂 `../local_models:/models:ro` |
| 命名卷 | Docker 管理 | 生产数据 | MySQL/OpenSearch 数据卷 |

**关键区别（面试问"容器删了数据还在吗"）**：容器删除只丢可写层；**卷独立于容器生命周期**——`docker compose down` 保留卷，`down -v` 才删卷清数据。

## 4. Dockerfile：镜像怎么造出来

### 4.1 指令速查（面试默写）

| 指令 | 作用 | 注意 |
|---|---|---|
| `FROM` | 基础镜像（每个 Dockerfile 第一行） | alpine 小、slim 中、full 全 |
| `WORKDIR` | 工作目录（后续命令的相对基准） | 总是设置，别用裸 cd |
| `COPY` / `ADD` | 复制文件进镜像 | 优先 COPY（ADD 会自动解压 tar） |
| `RUN` | 构建时执行命令（装依赖/编译） | 每个 RUN 产生一层 |
| `ENV` / `ARG` | 环境变量 / 构建参数 | ARG 只在构建期，ENV 进镜像 |
| `CMD` | 容器启动命令（可被覆盖） | 只推荐 exec 形式 `["python","app.py"]` |
| `ENTRYPOINT` | 固定入口（不可覆盖或需 `--entrypoint`） | 常与 CMD 搭配 |
| `EXPOSE` | 声明容器监听端口（**纯文档**，不映射） | 真正映射靠 `-p`/compose ports |
| `HEALTHCHECK` | 健康检查 | compose/编排依赖它 |

**CMD 两种形式（经典面试题）**：
```dockerfile
CMD python app.py            # shell 形式：实际跑 /bin/sh -c "python app.py"
CMD ["python", "app.py"]     # exec 形式：直接执行（推荐，信号能直达进程）
```

### 4.2 本项目 Dockerfile 精读（backend，lite/full 双 profile）

```dockerfile
ARG PROFILE=lite                       # 构建参数：docker build --build-arg PROFILE=full
FROM python:3.11-slim AS base
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt    # 先 COPY 依赖清单再装：
# ↑ 这行是缓存优化的关键——代码变只重跑 COPY+RUN 之后，依赖层永远命中缓存

FROM base AS lite                      # 多阶段：lite 只要基础
FROM base AS full
RUN pip install torch sentence-transformers --index-url ...   # full 多装重型依赖

FROM ${PROFILE}                        # 用构建参数选最终阶段！
COPY backend/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**设计要点（面试讲这个）**：
- **多阶段构建**：lite/full 共享 base（依赖层缓存复用），只差最终阶段的选择
- **依赖与代码分离 COPY**：改代码不重装依赖（构建 20s vs 全装 10min）
- ARG PROFILE 驱动最终阶段——`docker compose -f docker-compose.full.yml build` 就是传 `PROFILE=full`

### 4.3 多阶段构建：最终镜像只留运行所需（frontend 案例）

```dockerfile
# 阶段 1：node 构建（体积巨大，用完即弃）
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# 阶段 2：nginx 只 COPY 构建产物（镜像 79MB 而非 1GB+）
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
```

**面试题"镜像怎么瘦身"答案三件套**：① 多阶段构建只保留产物 ② 选小基础镜像（alpine/slim）③ `.dockerignore` 排除无关文件（node_modules、.git、日志）。

### 4.4 .dockerignore（每个项目都要有）

```
node_modules
.git
__pycache__
*.log
dist
```
> 作用：`COPY . .` 时这些不进构建上下文——构建更快、镜像更干净。

## 5. docker compose：多容器编排

### 5.1 为什么需要 compose

一条 `docker run` 只能起一个容器；项目要 5 个服务（backend/frontend/mysql/opensearch/searxng）协同——compose 用 YAML 声明"一组服务 + 网络 + 卷"，`up -d` 一次全起。

### 5.2 核心字段

```yaml
services:
  backend:
    build: { context: ., args: { PROFILE: lite } }   # 从源码构建（或 image: xxx 拉取）
    environment:                                      # 环境变量（等价 -e）
      EMBEDDING_PROVIDER: api
    env_file: .env                                    # 从文件注入
    ports: ["8000:8000"]                              # 端口映射
    volumes: ["../local_models:/models:ro"]           # 卷（:ro 只读）
    depends_on:                                       # 启动顺序（不等就绪！）
      - mysql
      - opensearch
    healthcheck:                                      # 就绪探针
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
    restart: unless-stopped                           # 崩溃自动拉起
    deploy:                                           # 资源限制/GPU
      resources:
        reservations:
          devices: [{ driver: nvidia, capabilities: [gpu] }]
```

### 5.3 常用命令

```bash
docker compose up -d            # 启动（-d 后台）；--build 先构建
docker compose up -d --build backend   # 只构建+启动某服务
docker compose down             # 停并删容器（卷保留）
docker compose down -v          # 连数据卷一起删（数据清空！）
docker compose ps               # 服务状态
docker compose logs -f backend  # 某服务日志
docker compose config           # 校验并展开配置（改 yaml 后先跑这个）
docker compose -f docker-compose.yml -f docker-compose.full.yml up -d  # 覆盖文件叠加
```

**compose 覆盖文件**：第二个 `-f` 文件**合并覆盖**第一个——full.yml 只写差异
（PROFILE=full + 挂模型 + GPU），base 文件不用改。这是配置复用的标准姿势。

### 5.4 本项目 compose 网络拓扑（面试画图）

```text
                    ┌──────────┐
用户浏览器 ──5173──> │ frontend │  (nginx 反代 /api)
                    └────┬─────┘
                         │ backend:8000（compose 内部网络，服务名即域名）
                    ┌────▼─────┐   ┌─────────┐   ┌──────────┐
                    │ backend  │──>│ mysql   │   │ opensearch│
                    │ 8000:8000│   │ 3306(内)│   │ 9200:9200 │
                    └──────────┘   └─────────┘   └──────────┘
```
- compose 自动建一个 bridge 网络，服务用**服务名互相访问**（backend 里配
  `SEARXNG_BASE_URL=http://searxng:8888`——searxng 是主机名不是 IP）
- 只有声明了 `ports` 的服务暴露给宿主机

## 6. 调试与排错（对照你踩过的坑）

| 症状 | 排查 | 你项目的实例 |
|---|---|---|
| 容器启动即退出 | `docker logs 容器` 看报错 | OpenSearch code 64：安全插件配置重复 |
| 服务没起来但容器在 | `docker exec` 进去手动跑 | lite 缺 torch：`exec python -c "import torch"` 秒判 |
| 端口被占/连不上 | `docker ps` 看谁映射了端口 | 本机 uvicorn vs 容器抢 8000 |
| 上传失败 | 看 nginx 日志 | 413 = `client_max_body_size` 太小 |
| 改了代码不生效 | **忘了 rebuild**！`up -d` 用旧镜像 | 本项目多次踩 |
| 容器内路径找不到 | git-bash 会转换 `/路径` 为 Windows 路径 | 用 `MSYS_NO_PATHCONV=1` 或 `//` |

**"改了代码不生效"是最常见的坑**：`docker compose up -d` **不会重新构建**——
镜像还是旧的。必须 `docker compose up -d --build` 或先 `docker compose build`。

## 7. 最佳实践清单（面试"你怎么用 Docker"直接背）

1. **密钥不进镜像**：`.env`/环境变量注入；镜像可被任何人拉取查看
2. **依赖与代码分层 COPY**：缓存命中 = 秒级 rebuild
3. **多阶段构建**：构建环境与运行环境分离，产物最小化
4. **进程 1 原则**：一个容器一个主进程；CMD 用 exec 形式（信号/优雅退出）
5. **非 root 运行**（生产）：`USER appuser` 降低容器逃逸风险
6. **数据必须走卷**：容器可删可换，数据在卷里
7. **健康检查**：HEALTHCHECK/healthcheck 让编排知道服务"真就绪"
8. **镜像标签**：不用 latest 裸跑生产（不可复现）；用版本/commit 标签

## 8. 面试高频题

1. **镜像和容器区别**？→ 镜像只读软件包，容器 = 镜像 + 可写层 + 运行时态。
2. **docker run 发生了什么**（底层流程）？→ 找镜像（本地→仓库）→ 建容器层 →
   分配网络/文件系统 → 启动进程（Pause 容器 + runc 起主进程）。
3. **端口映射 -p 8000:8000 什么意思**？→ 宿主机 8000 转发容器 8000；容器间访问
   不走映射（走网络/服务名）。
4. **数据持久化怎么做**？→ 卷（命名卷/bind mount）；容器删除数据不丢（卷独立）。
5. **镜像怎么瘦身**？→ 多阶段 + 小基础镜像 + .dockerignore + 合并 RUN。
6. **ENTRYPOINT vs CMD**？→ ENTRYPOINT 固定入口，CMD 提供默认参数/可覆盖。
7. **compose 和 docker run 区别**？→ compose 声明式管理多容器（网络/卷/依赖自动），
   run 命令式单容器。
8. **一个容器能跑多个进程吗**？→ 能但不推荐（进程管理/日志/重启策略复杂），
   用 compose 拆服务（每个容器一个职责）。

## 9. 实战练习（对着做）

1. 用 `docker run -d -p 8080:80 nginx` 起 nginx，`exec` 进去改 `/usr/share/nginx/html/index.html` 再 curl 看效果——理解可写层
2. 用 `docker rm -f` 删掉，`docker volume` 场景：起 mysql 挂命名卷，写入数据 → down → up → 数据还在 → `down -v` → 没了
3. 给项目写一个"只跑 build_index"的一次性容器：`docker compose run --rm backend python build_index_v2.py`
4. 手动跑 `docker build -f Dockerfile --build-arg PROFILE=full -t zhiwen-backend-full .`，对比 lite/full 镜像大小（`docker images`）
5. 排错演练：故意把 compose 里端口改成 8001 再 up，观察 `docker compose ps` 与 `logs`，理解端口映射

## 10. 自测清单

- [ ] 能说出镜像分层与层缓存的原理
- [ ] 能手写一个多阶段 Dockerfile（node 构建 + nginx 运行）并解释每行
- [ ] 能解释项目 Dockerfile 的 lite/full 双 profile 设计（ARG + 多阶段选择）
- [ ] 知道密钥为什么不进镜像、数据为什么不进容器
- [ ] 能独立排错：容器起不来（logs→exec→手动复现）、端口冲突（ps→映射）
- [ ] 会 compose 覆盖文件（-f 叠加）和 `--build` 的必要性

## 11. 学习计划（3-4 天）

- Day 1：第 1-3 节（心智模型 + 命令 + 卷/网络），做完练习 1-2
- Day 2：第 4 节 Dockerfile（对照项目两个 Dockerfile 逐行读）
- Day 3：第 5-6 节（compose + 排错），用项目实际 up/down/rebuild 走一遍
- Day 4：第 7-8 节 + 自测清单，写一个自己的单服务 Dockerfile（任意小应用）
