# Kubernetes 学习指南（概念 → YAML → 本机实操 → 部署项目）

> 目标：学完能达到"会用"级别——理解核心对象、能读能写 Deployment/Service 等 YAML、
> 会用 kubectl 部署与排错、能把 Docker 项目搬到 K8s。
> 前置：先学 `DOCKER_MASTER_GUIDE.md`（K8s 管理的是容器）。
> 本机环境：Windows + Docker Desktop（自带 K8s 开关，无需额外安装）。

---

## 1. 为什么需要 K8s：Docker 管单机，K8s 管集群

Docker Compose 解决的问题：一台机器上编排多个容器。
K8s 解决的问题（多一层）：**多台机器（集群）上的容器编排**——自动调度、
滚动升级、故障自愈、服务发现、水平伸缩。

| 能力 | Docker Compose | Kubernetes |
|---|---|---|
| 单机多容器 | ✅ | ✅（也能单机） |
| 多节点集群调度 | ❌ | ✅ |
| 故障自愈（容器挂了自动重启） | restart 策略 | ✅ 自动重建 |
| 滚动升级/回滚 | 手动 | ✅ 原生 |
| 水平伸缩 | ❌ | ✅（HPA） |
| 服务发现/负载均衡 | compose 网络 | ✅ Service |
| 配置管理 | env_file | ✅ ConfigMap/Secret |

**一句话**：把"我要跑哪些容器、怎么互相访问、怎么更新"声明成 YAML，K8s 负责执行与维护。

## 2. 环境准备（Windows + Docker Desktop）

Docker Desktop → Settings → Kubernetes → **Enable Kubernetes** → Apply。
等待 `kubectl` 就绪（Docker Desktop 自带 kubectl）：

```powershell
kubectl version           # 客户端+服务端版本
kubectl get nodes         # 看到一个节点（你的电脑 = 单节点集群）
kubectl get ns            # 命名空间（default/kube-system...）
```

> 替代方案：minikube（更接近生产，但多一层 VM）；先 Docker Desktop 内置即可。

## 3. 核心概念地图（先建立全局）

```text
                    ┌──────────────────────────────────┐
                    │  Control Plane（控制面/主节点）    │
                    │  api-server：所有操作的入口       │
                    │  scheduler：决定 Pod 跑哪个节点    │
                    │  controller-manager：维护期望状态  │
                    │  etcd：集群状态存储（key-value）   │
                    └──────────────────────────────────┘
   kubectl apply -f xxx.yaml ─────┘│
                                   ▼
                    ┌──────────────────────────────────┐
                    │  Worker Node（工作节点）           │
                    │  kubelet：本节点的"监工"          │
                    │  kube-proxy：网络规则/负载均衡     │
                    │  ┌────────┐  ┌────────┐          │
                    │  │ Pod    │  │ Pod    │  ← 容器运行的地方 │
                    │  └────────┘  └────────┘          │
                    └──────────────────────────────────┘
```

**最小调度单元是 Pod 不是容器**：
- **Pod** = 一组紧密相关的容器（通常 1 个主容器）+ 共享网络/存储/生命周期
- Pod 是"野猫"：随时可能被杀/迁移 → 不要直接管 Pod，管它的**控制器**

**四层对象（从下往上）**：

| 对象 | 管什么 | 类比 |
|---|---|---|
| Pod | 一个或多个容器实例 | 进程 |
| **Deployment** | Pod 的期望状态：副本数、滚动更新、自愈 | supervisor |
| **Service** | Pod 的稳定访问入口（Pod IP 会变） | 负载均衡器/域名 |
| **Ingress** | 集群外部 HTTP 入口（域名→Service） | 反向代理/网关 |

**期望状态（declarative）**：你告诉 K8s"我要 3 个副本、镜像 v2"，
K8s 的 controller 不断把**实际状态**拉向**期望状态**——这就是自愈的原理
（容器崩了 controller 重建，不用你管）。

## 4. kubectl 必会命令（对应 docker 命令记忆）

| Docker | Kubernetes | 作用 |
|---|---|---|
| docker ps | `kubectl get pods` | 看运行实例 |
| docker logs | `kubectl logs <pod>` | 看日志（Deployment 用 `-f deploy/name`） |
| docker exec | `kubectl exec -it <pod> -- bash` | 进容器 |
| docker run | `kubectl create deployment` / apply yaml | 部署 |
| docker compose up | `kubectl apply -f xxx.yaml` | 声明式应用 |
| docker inspect | `kubectl describe <obj>` | 详情/事件（排错神器） |
| docker stop | `kubectl delete deployment <name>` | 删除 |
| — | `kubectl get events` | 集群事件 |
| — | `kubectl port-forward svc/xxx 8080:80` | 本地访问集群内服务（调试用） |

```bash
kubectl get pods -o wide          # 加节点/IP
kubectl get deploy,rs,pods        # 多个对象一起看
kubectl describe pod xxx          # 排错第一命令：Events 区看为什么没起来
kubectl logs --tail=50 deploy/backend   # 整个 Deployment 的日志（选一个 Pod）
kubectl delete -f xxx.yaml        # 删（声明式删除）
kubectl apply -f xxx.yaml         # 改配置后重新应用（滚动生效）
```

## 5. 第一个 YAML：Deployment 精读（本项目 backend）

```yaml
apiVersion: apps/v1              # API 版本（查文档，别瞎写）
kind: Deployment                 # 对象类型
metadata:
  name: zhiwen-backend            # 对象名（kubectl get deploy zhiwen-backend）
  labels:                        # 标签：选择器的 key
    app: zhiwen
    tier: backend
spec:                            # 期望状态
  replicas: 2                    # 两个副本（自愈 + 高可用）
  selector:
    matchLabels:                 # 管哪些 Pod（必须与 template 标签匹配）
      app: zhiwen
      tier: backend
  template:                      # Pod 模板
    metadata:
      labels: { app: zhiwen, tier: backend }
    spec:
      containers:
        - name: backend
          image: zhiwen-backend:latest   # 镜像（本地/仓库）
          imagePullPolicy: IfNotPresent # 本地有就不拉（单机开发关键！）
          ports: [{ containerPort: 8000 }]
          env:                            # 环境变量（生产用 Secret，见 §7）
            - name: DEEPSEEK_API_KEY
              valueFrom:
                secretKeyRef: { name: zhiwen-secrets, key: deepseek-key }
          resources:                      # 资源申请/限制（调度依据）
            requests: { cpu: "500m", memory: 512Mi }
            limits: { cpu: "2", memory: 2Gi }
          readinessProbe:                 # 就绪探针：没就绪前 Service 不给流量
            httpGet: { path: /api/health, port: 8000 }
            initialDelaySeconds: 10
```

**为什么 Service 需要 selector**：Deployment 不管 Pod 的 IP（每次重建都变）；
Service 用 `selector: app=zhiwen,tier=backend` 找到"当前这群 Pod"，做负载均衡。

## 6. Service 与 Ingress：怎么访问

### 6.1 Service（集群内部稳定入口 + 负载均衡）

```yaml
apiVersion: v1
kind: Service
metadata:
  name: zhiwen-backend
spec:
  selector: { app: zhiwen, tier: backend }   # 选 Pod（与 Deployment 标签一致）
  ports:
    - port: 80           # Service 的端口（其他 Pod 通过它访问）
      targetPort: 8000   # 转发到 Pod 的容器端口
  type: ClusterIP        # 默认：集群内可达（集群外不可达）
```

**Service 类型三选一**：
- `ClusterIP`（默认）：集群内访问 `http://zhiwen-backend:80`——**服务间调用用这个**
- `NodePort`：每个节点开一个端口（30000-32767）暴露到集群外（测试用）
- `LoadBalancer`：云厂商负载均衡器（生产）；**单机 Docker Desktop 会映射到 localhost**

### 6.2 Ingress（HTTP 域名入口，可选章节）

Ingress 是"外部 HTTP 路由表"——按域名/路径把请求送到对应 Service。
K8s 本身不实现它，需要 Ingress Controller（nginx-ingress 等）。单机学习可跳过。

## 7. ConfigMap 与 Secret：配置管理

```yaml
apiVersion: v1
kind: ConfigMap              # 非敏感配置（明文）
metadata: { name: zhiwen-config }
data:
  WEB_SEARCH_PROVIDER: searxng
  SEARXNG_BASE_URL: http://searxng:8888
---
apiVersion: v1
kind: Secret                 # 敏感配置（base64 只是编码不是加密！）
metadata: { name: zhiwen-secrets }
type: Opaque
data:
  deepseek-key: c2stYWFh...      # echo -n "sk-xxx" | base64
stringData:                      # 更友好：直接写明文，K8s 自动编码
  mysql-url: mysql+pymysql://root:xxx@mysql:3306/rag_assistant
```

**使用方式**（容器里变成环境变量或文件）：
```yaml
env:
  - name: DEEPSEEK_API_KEY
    valueFrom: { secretKeyRef: { name: zhiwen-secrets, key: deepseek-key } }
  - name: WEB_SEARCH_PROVIDER
    valueFrom: { configMapKeyRef: { name: zhiwen-config, key: WEB_SEARCH_PROVIDER } }
```

> 面试点：Secret 默认只做 base64（可被 get 看到），真正加密要 etcd 加密 + RBAC；
> 所以密钥管理最佳实践是外部系统（Vault/云 KMS）注入。

## 8. 滚动更新与回滚（K8s 的杀手锏）

```bash
kubectl set image deploy/zhiwen-backend backend=zhiwen-backend:v2   # 触发滚动更新
kubectl rollout status deploy/zhiwen-backend     # 看进度
kubectl rollout history deploy/zhiwen-backend    # 历史版本
kubectl rollout undo deploy/zhiwen-backend       # 一键回滚到上一版
```

**原理**（面试必问）：Deployment 创建新 ReplicaSet（新版本 Pod），
**逐个**替换旧 Pod（maxUnavailable/maxSurge 控制节奏），
新 Pod **就绪探针通过**才继续替换——所以更新失败会自动停下，不会全挂。

## 9. 存储：PV / PVC

```yaml
# PVC：声明"我要 5Gi 存储"（像申领资源，不关心底层实现）
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: zhiwen-data }
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: 5Gi } }
```
Pod 里 `volumes: [{ name: data, persistentVolumeClaim: { claimName: zhiwen-data } }]`。

> 单机 Docker Desktop 有 local-path provisioner，PVC 自动绑定本机目录。
> 生产（云上）用云盘（EBS/云盘）或 NFS。**原理一致：Pod 是野猫，数据必须独立于 Pod**。

## 10. 把本项目部署到 K8s（完整示例，对照 compose 理解）

```yaml
# zhiwen-k8s.yaml —— 一次声明全部（实际项目按服务拆文件）
apiVersion: apps/v1
kind: Deployment
metadata: { name: mysql }
spec:
  replicas: 1
  selector: { matchLabels: { app: mysql } }
  template:
    metadata: { labels: { app: mysql } }
    spec:
      containers:
        - name: mysql
          image: mysql:8.0
          env:
            - name: MYSQL_ROOT_PASSWORD
              valueFrom: { secretKeyRef: { name: zhiwen-secrets, key: mysql-root-pw } }
          ports: [{ containerPort: 3306 }]
          volumeMounts: [{ name: data, mountPath: /var/lib/mysql }]
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: mysql-data }
---
apiVersion: v1
kind: Service
metadata: { name: mysql }
spec:
  selector: { app: mysql }
  ports: [{ port: 3306, targetPort: 3306 }]    # 只集群内访问（不暴露外网！）
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: zhiwen-backend }
spec:
  replicas: 1
  selector: { matchLabels: { app: zhiwen, tier: backend } }
  template:
    metadata: { labels: { app: zhiwen, tier: backend } }
    spec:
      containers:
        - name: backend
          image: zhiwen-backend:latest
          imagePullPolicy: IfNotPresent
          env:
            - { name: MYSQL_URL, value: "mysql+pymysql://root:xxx@mysql:3306/rag_assistant?charset=utf8mb4" }
            - name: DEEPSEEK_API_KEY
              valueFrom: { secretKeyRef: { name: zhiwen-secrets, key: deepseek-key } }
          ports: [{ containerPort: 8000 }]
          readinessProbe:
            httpGet: { path: /api/health, port: 8000 }
            initialDelaySeconds: 15
---
apiVersion: v1
kind: Service
metadata: { name: zhiwen-backend }
spec:
  selector: { app: zhiwen, tier: backend }
  ports: [{ port: 80, targetPort: 8000 }]
---
# frontend：nginx 容器 + NodePort 暴露（单机测试）
apiVersion: apps/v1
kind: Deployment
metadata: { name: zhiwen-frontend }
spec:
  replicas: 1
  selector: { matchLabels: { app: zhiwen, tier: frontend } }
  template:
    metadata: { labels: { app: zhiwen, tier: frontend } }
    spec:
      containers:
        - name: frontend
          image: zhiwen-frontend:latest
          imagePullPolicy: IfNotPresent
          ports: [{ containerPort: 80 }]
---
apiVersion: v1
kind: Service
metadata: { name: zhiwen-frontend }
spec:
  selector: { app: zhiwen, tier: frontend }
  type: NodePort
  ports: [{ port: 80, targetPort: 80, nodePort: 30080 }]
```

**容器间访问 vs compose 对比**（面试理解点）：
- compose：`http://backend:8000`（compose 网络内服务名）
- K8s：`http://zhiwen-backend:80`（Service 名 + Service 端口）——**同样靠名字不靠 IP**
- frontend 的 nginx 反代目标从 `http://backend:8000` 改成 `http://zhiwen-backend:80`

```bash
kubectl apply -f zhiwen-k8s.yaml
kubectl get pods          # 等 Running 1/1
kubectl port-forward svc/zhiwen-frontend 5173:80   # 本地调试访问
curl http://127.0.0.1:5173
```

## 11. 排错方法论（对应 Docker 排错升级版）

| 症状 | 第一步 |
|---|---|
| Pod `Pending` | `kubectl describe pod` → Events：资源不够/镜像拉不到/调度失败 |
| Pod `ImagePullBackOff` | 镜像名/标签错、私有仓库没认证；单机本地镜像要 `imagePullPolicy: IfNotPresent` |
| Pod `CrashLoopBackOff` | `kubectl logs <pod>` 看崩溃原因（代码错/配置错） |
| Pod Running 但访问不通 | Service selector 与 Pod 标签是否匹配（`get pods --show-labels`）；`port-forward` 直连 Pod 隔离问题 |
| 更新后还是旧版本 | `rollout status`；确认新 ReplicaSet 是否就绪 |
| 忘了怎么配 | `kubectl explain deployment.spec` —— **kubectl 自带文档** |

**排错五步（背）**：get（状态）→ describe（事件）→ logs（应用日志）→
port-forward（绕开网络直接连）→ events（全局事件）。

## 12. 面试高频题

1. **Pod 和 Deployment 关系**？→ Deployment 是 Pod 的控制器：声明期望副本数，
   控制器保证实际=期望（自愈/滚动/回滚）。Pod 本身无自愈。
2. **Service 有什么用**？→ Pod IP 会变；Service 提供稳定 DNS 名 + 负载均衡 +
   选择器动态绑定 Pod 集合。
3. **滚动更新怎么做、为什么不会全挂**？→ 新 RS 逐个替换 + 就绪探针门控。
4. **ConfigMap 和 Secret 区别**？→ 都注入配置；Secret 存敏感信息（base64 非加密）。
5. **StatefulSet 什么时候用**？→ 有状态服务（数据库/需要稳定网络身份）：Pod 名
   固定（xxx-0、xxx-1）、顺序启停。本项目 MySQL 演示单实例可用 Deployment+PVC。
6. **单机 Docker Desktop K8s 和生产区别**？→ 单机无真实多节点调度/云盘；概念一致。
7. **声明式 vs 命令式**？→ apply yaml 声明期望态（推荐，可审计可 Git 化）；
   命令式（run/set）临时操作。

## 13. 自测清单 + 学习计划

**自测**：
- [ ] 能画出控制面/工作节点/etcd 关系图
- [ ] 能默写 Deployment 关键字段（replicas/selector/template/probes）
- [ ] 能解释 Service selector 与端口映射
- [ ] 能区分 ConfigMap/Secret/环境变量注入
- [ ] 能独立完成"改镜像→apply→rollout status→回滚"闭环
- [ ] 能背出排错五步

**学习计划（3-4 天）**：
- Day 1：第 1-4 节（概念 + kubectl），开 Docker Desktop K8s，跑 `get nodes/pods`
- Day 2：第 5-7 节（Deployment/Service/配置），手写并 apply 一个 nginx Deployment
- Day 3：第 8-10 节（滚动/存储/项目部署），把 zhiwen-k8s.yaml 部署起来
- Day 4：第 11-12 节 + 自测；故意制造故障（错镜像名/错标签）走一遍排错五步
