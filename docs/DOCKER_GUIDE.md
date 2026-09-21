# Docker 教学与使用手册(本项目专用)

> 面向完全没接触过 Docker 的读者。先讲概念,再教命令,最后落到本项目怎么跑。
> 跟着做就能跑起来;跑不起来时看文末「常见问题」。

---

## 1. Docker 是什么,为什么需要它

### 1.1 没 Docker 时的痛

本项目的运行依赖:Python 3.10+、Node 18+、OpenSearch、MySQL、本地模型(2.6G)。
换个新机器,面试官要跑你的项目,得依次装这些,还要处理版本冲突——光是环境就劝退一半人。

### 1.2 Docker 的思路

把"程序 + 它需要的所有依赖 + 配置"打包成一个**镜像(Image)**,镜像启动后就是一个
**容器(Container)**——一个与世隔绝的小房间,程序在房间里想装什么装什么,不会污染你的电脑。

用集装箱类比:

| 概念 | 类比 | 说明 |
|---|---|---|
| 镜像 (Image) | 集装箱的**设计图纸** | 只读模板,描述"里面有什么" |
| 容器 (Container) | 按图纸造的**一个集装箱** | 运行中的实例,可以启动/停止/删除 |
| 数据卷 (Volume) | 集装箱外的**仓库** | 容器删了数据还在,专门存数据库文件 |
| 端口映射 | 集装箱的**发货口** | 让外界的请求能进到容器里 |

### 1.3 镜像 vs 容器,一句话

- `docker build` 造图纸,**`docker run` / `docker compose up` 按图纸开箱**
- 同一张图纸可以开无数个箱子,互不影响
- 箱子(容器)删了,图纸(镜像)还在,随时能再开

---

## 2. 本项目的 Docker 结构

```text
                    ┌─────────────────────────────┐
                    │  前端容器 (nginx)  :5173    │  浏览器访问
                    │   - 托管打包好的前端页面    │
                    │   - /api 请求反向代理到后端 │
                    └─────────────┬───────────────┘
                                  │ /api
                    ┌─────────────▼───────────────┐
                    │  后端容器 (uvicorn) :8000   │  FastAPI + LangGraph + RAG
                    │   - 你自己的代码(backend/)  │
                    └──────┬──────────────┬───────┘
                           │              │
              ┌────────────▼───┐   ┌──────▼────────────┐
              │ OpenSearch 容器 │   │ MySQL 容器        │
              │  :9200 向量库   │   │ :3306 对话/记忆   │
              └────────────────┘   └───────────────────┘
```

五个服务之间通过 Docker 内部网络互相访问(用服务名 `backend`、`opensearch`、
`mysql`、`searxng` 互相通信),同时把必要的端口暴露给宿主机(你的电脑)供你访问。

---

## 3. 你需要装的东西

Windows 直接装 **Docker Desktop**:

1. 下载 https://www.docker.com/products/docker-desktop/
2. 一路下一步(勾选 WSL 2 后端)
3. 装完启动 Docker Desktop,等右下角鲸鱼图标变绿(表示引擎就绪)
4. 验证:`docker --version` 有输出即可

> 国内下载慢可用镜像站,或直接在网页下载慢就挂代理。

---

## 4. 常用命令教学(带本项目实例)

> 所有命令在项目根目录(`rag_knowledge_base/`)执行。`docker` 开头的都是 docker 命令,
> `docker compose` 是"多容器编排"命令,专门管本项目这一套。

### 4.1 看状态

```bash
docker ps                    # 正在运行的容器(名字/镜像/端口/状态)
docker ps -a                 # 全部容器(含已停止的)
docker images                # 本机所有镜像(占磁盘的东西)
docker volume ls             # 所有数据卷
```

### 4.2 启动 / 停止 / 删除

| 命令 | 作用 | 会不会删数据 |
|---|---|---|
| `docker compose up -d` | 启动整套(后台运行) | 不会 |
| `docker compose down` | 停止整套 | **不会**(数据卷保留) |
| `docker compose down -v` | 停止整套并删数据卷 | **会**(会话、索引全没) |
| `docker compose stop` | 暂停(不删容器) | 不会 |

> **区分**:`down` 删容器保留数据;`down -v` 连数据一起删。**平时只用 `down`。**

### 4.3 看日志 / 进容器

```bash
docker compose logs -f backend     # 实时看后端日志(排错第一手段)
docker compose logs backend | tail -50   # 看最后 50 行
docker exec -it rag_knowledge_base-backend-1 bash   # 进容器内部(exit 退出)
```

### 4.4 重新构建(改了代码/依赖后)

```bash
docker compose up -d --build    # 重新构建镜像并启动
```

> 改了 `backend/app/` 下的代码,重新 `up -d --build` 即可;只有改了
> requirements/Dockerfile 才需要完整重装依赖层。

---

## 5. 本项目两种跑法

### 5.1 lite 模式(默认,推荐先用这个)

特点:**不需要 GPU、不需要 2.6G 本地模型**。嵌入和重排走云端 API
(默认 SiliconFlow 免费档 bge-m3),对话走 DeepSeek。

```bash
# ① 复制环境变量模板(只需做一次)
cp .env.example .env

# ② 编辑 .env,至少填 DEEPSEEK_API_KEY
#    想用知识库功能,再填 EMBEDDING_API_KEY / RERANKER_API_KEY(SiliconFlow 免费注册)

# ③ 一键启动(首次要构建镜像,约 5~10 分钟,取决于网络)
docker compose up -d --build

# ④ 打开浏览器
#    前端 http://localhost:5173
#    后端接口文档 http://localhost:8000/docs
```

国内网络慢时加 pip 镜像源(推荐):

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose up -d --build
```

### 5.2 full 模式(本地模型 + GPU)

特点:嵌入/重排全部本地执行,不花钱、数据不出本机。前提:

- 仓库上一级有 `local_models/` 目录(内含 bge-base-zh-v1.5 和 reranker)
- 想用 GPU 需 Docker Desktop 的 WSL2 + NVIDIA 驱动;没 GPU 也能用 CPU(慢一些)

```bash
docker compose -f docker-compose.yml -f docker-compose.full.yml up -d --build
```

> lite 和 full 切换后,如果报索引维度冲突(768 vs 1024),先 `docker compose down -v`
> 清空旧索引再启动。

### 5.3 不想用 Docker 也可以

Docker 只是帮你免装依赖;不装 Docker 一样能跑,见 README「快速开始」:
装 Python/Node/OpenSearch/MySQL → `pip install -r requirements.txt` → `npm install` → 启动。

---

## 6. .env 文件里都有什么

| 变量 | 必填? | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ 必填 | 对话模型 key(DeepSeek 官网申请) |
| `EMBEDDING_API_KEY` | 知识库功能要 | SiliconFlow 等平台的嵌入 key(免费档够用) |
| `RERANKER_API_KEY` | 知识库功能要 | 同上,重排 key |
| `SENSENOVA_API_KEY` | 可选 | 识图/OCR |
| `MYSQL_ROOT_PASSWORD` | 选填 | 容器内 MySQL 的 root 密码,有默认值 |
| `PROFILE` | 选填 | `lite` 或 `full` |
| `MYSQL_PORT` / `OPENSEARCH_PORT` / `BACKEND_PORT` / `FRONTEND_PORT` | 选填 | 宿主端口,被占用时改(见常见问题 7.1) |

---

## 7. 常见问题排查

### 7.1 端口被占用:3306 已经被我本机的 MySQL 占了

```bash
# 在 .env 里加一行,换一个宿主端口
MYSQL_PORT=3307
docker compose down && docker compose up -d
```

### 7.2 前端页面打不开 / 后端报错

```bash
docker compose logs backend | tail -100   # 看后端日志
docker compose logs frontend | tail -30   # 看前端/nginx 日志
```

最常见的两类错误:
- `401 Unauthorized`(访问 api.siliconflow.cn 等)→ **.env 里 key 没填或填错了**,改完 `docker compose up -d` 重启
- `MySQL 未连接` → 等 mysql 容器 healthy 后再访问,或看 mysql 日志

### 7.3 改了代码不生效

```bash
docker compose up -d --build    # 一定要带 --build,否则用的是旧镜像
```

### 7.4 想彻底重置(数据全清)

```bash
docker compose down -v          # ⚠️ 会删掉所有会话记录和知识库索引
```

### 7.5 Linux 上 OpenSearch 启动失败

报 `max virtual memory areas vm.max_map_count [65530] is too low`:

```bash
sudo sysctl -w vm.max_map_count=262144
```

Windows / Mac 的 Docker Desktop 不需要处理。

### 7.6 磁盘被 Docker 占满了

```bash
docker system df        # 看占用
docker system prune     # 清理悬空镜像/缓存(不会删数据卷)
```

### 7.7 打包好镜像想给别人

```bash
docker save -o rag-backend-lite.tar rag_knowledge_base-backend
# 对方: docker load -i rag-backend-lite.tar
```

---

## 8. 命令速查表(贴墙版)

```bash
docker compose up -d              # 启动整套(后台)
docker compose up -d --build      # 改代码后重建并启动
docker compose logs -f backend    # 看后端日志
docker compose down               # 停止(保留数据)
docker compose down -v            # 停止并清空数据 ⚠️
docker ps                         # 看运行中的容器
docker images                     # 看镜像
docker volume ls                  # 看数据卷
docker exec -it <容器名> bash     # 进容器
docker system prune               # 清理垃圾
```

---

## 9. 给面试官的一句话

"项目提供 Docker 一键部署:五服务(lite 模式无需 GPU 和本地模型,含 SearXNG
联网搜索),嵌入/重排走 OpenAI 兼容 API,只有对话模型需要 API Key,配置完
`docker compose up -d --build` 即可在浏览器使用。另有 full 模式支持本地模型 + GPU。"
