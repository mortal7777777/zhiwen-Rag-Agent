# Linux 命令行与服务器管理教程

> 目标：学完本教程，达到简历要求的"熟悉 Linux 常用命令与服务器管理"。
> 定位：Windows 用户从零起步 → 常用命令熟练 → 能独立部署和排查服务器问题。
> 方法：每章先看概念，再敲命令（**必须动手敲**），最后做章末练习。
> 约定：`$` 开头是普通用户命令，`#` 开头是 root 命令（或需要 sudo）。

---

## 第 0 章 准备环境（Windows 用户必读）

### 0.1 两种学习环境，选一种

| 方式 | 优点 | 缺点 | 适合 |
|---|---|---|---|
| **WSL2**（推荐先试） | 免费、秒启、和 Windows 共享文件 | 虚拟化占用内存 | 练习命令、本地部署 |
| **云服务器**（阿里云/腾讯云学生机） | 真实服务器环境，能练 SSH/防火墙/公网 | 要花钱（学生机很便宜） | 练"服务器管理"（必选，简历也写了了解 ECS） |

### 0.2 安装 WSL2（Windows 11 一条命令）

```powershell
# Windows 终端（管理员）执行：
wsl --install
# 装完重启，默认装 Ubuntu。然后：
wsl            # 进入 Linux 终端
sudo apt update && sudo apt upgrade -y   # 首次更新软件源
```

- Windows 和 WSL 共享文件：Windows 的 `C:\Users\...` 在 Linux 里是 `/mnt/c/Users/...`
- 在 WSL 里可以用 `code .` 直接打开 VS Code（会装 WSL 插件，体验很好）
- 云服务器：买完后用 Windows 自带的 SSH 连：`ssh root@你的公网IP`

### 0.3 学习方法

- 每个命令敲 3 遍以上；`man 命令` 看手册（按 q 退出）
- 记不住没关系，**用 `--help` 和 man 查**就是专业做法
- 本教程命令都按"面试会问 + 部署够用"精选，不需要背全部

### 0.4 本章掌握标准
- [ ] 能进入 WSL/服务器终端，执行 `pwd`、`ls`、`whoami`
- [ ] 知道 `sudo` 是临时提权

---

## 第 1 章 终端基础：命令是怎么工作的

### 1.1 命令结构

```
命令  -选项  参数
ls    -l     /etc
```

- 选项通常用 `-l`（短）或 `--long`（长）表示
- 一行命令可以组合：`ls -lha /etc`（`-l` 长格式、`-h` 人类可读、`-a` 含隐藏文件）

### 1.2 路径：Linux 最重要的事

```bash
/            # 根目录（一切从这里开始，没有盘符 C: D:）
~            # 家目录，root 是 /root，普通用户是 /home/用户名
.            # 当前目录
..           # 上级目录
/opt/app     # 绝对路径（从根开始）
app/data     # 相对路径（从当前位置开始）
```

**Windows vs Linux 路径对照**：`C:\Users\me\file.txt` = `/mnt/c/Users/me/file.txt`（WSL）或 `/home/me/file.txt`；Linux 没有盘符，所有设备都挂载在 `/` 下。

### 1.3 常用按键

| 按键 | 作用 |
|---|---|
| Tab | 补全（**命令/文件名补全，最常用的键**） |
| ↑/↓ | 历史命令 |
| Ctrl+C | 中断当前命令 |
| Ctrl+D | 退出当前 shell |
| Ctrl+L | 清屏（或输入 clear） |

### 1.4 man 手册（不会用 man 不算会 Linux）

```bash
man ls        # ls 的手册
man man       # man 的手册
ls --help     # 大多数命令的简版帮助
```

### 1.5 练习
1. 用绝对路径进入 `/etc`，再用 `..` 回 `/`
2. `man cp` 查 cp 有哪些选项，找出复制目录要加什么参数（答案：`-r`）

### 1.6 本章掌握标准
- [ ] 能解释绝对路径/相对路径，说出 5 个常用目录的用途（`/etc` 配置、`/var/log` 日志、`/home` 用户、`/tmp` 临时、`/opt` 软件）
- [ ] 用 Tab 补全和 `--help` 解决"不知道某命令怎么用"

---

## 第 2 章 文件与目录操作（最常用，必须闭眼敲）

### 2.1 看与进

```bash
pwd                 # 我在哪
ls                  # 当前目录内容
ls -l               # 长格式（权限/属主/大小/时间）
ls -a               # 含隐藏文件（. 开头）
ls -lh              # 大小人类可读（K/M/G）
cd /etc             # 进入目录
cd ~                # 回家
cd -                # 回上一个目录
```

`ls -l` 输出解读（面试会问第一列）：
```
-rw-r--r-- 1 root root 1024 Aug 10 12:00 file.txt
- rw- r-- r--          # 类型(-文件/d目录/l链接) + 属主权限 + 属组权限 + 其他权限
```

### 2.2 建与删

```bash
mkdir app                # 建目录
mkdir -p a/b/c           # 递归建多层
touch test.txt           # 建空文件（或更新文件时间）
rm test.txt              # 删文件
rm -r app                # 删目录（递归）
rm -rf app               # 强制递归删除 —— ⚠️ 最危险命令，没有回收站！
cp file.txt bak.txt      # 复制
cp -r app app_backup     # 复制目录
mv old.txt new.txt       # 移动/重命名
```

⚠️ **`rm -rf` 红线**：永远不要对 `/`、`~`、`/etc` 执行 `rm -rf`；执行前先 `ls` 确认路径。面试题："rm -rf / 会怎样"——答案：所有文件没了，系统崩。

### 2.3 看内容

```bash
cat file.txt        # 全量打印（小文件）
head -20 file.txt   # 前 20 行
tail -20 file.txt   # 后 20 行
tail -f app.log     # 实时跟踪追加内容（看日志最常用！Ctrl+C 退出）
less file.txt       # 分页查看（空格翻页，q 退出，/ 搜索）
```

### 2.4 查找

```bash
find /opt -name "*.log"          # 按名字找文件
find /opt -type d -name "app"    # 找目录
grep "error" app.log             # 在文件里搜文本（面试高频）
grep -rn "DEEPSEEK" /opt/app     # 递归搜目录（-r 递归 -n 带行号）
grep -v "^#" nginx.conf          # 反向匹配：排除注释行
```

### 2.5 通配符

```bash
*.log        # 所有 .log 结尾
app?.txt     # app1.txt app2.txt（? 单个字符）
[abc].txt    # a.txt b.txt c.txt
```

### 2.6 练习
1. 建目录 `~/practice/{logs,data,scripts}`，在 logs 里建 3 个 .log 文件，用一条命令列出所有 .log
2. 用 `find` 找到 `/etc` 下所有 `.conf` 文件（答案：`find /etc -name "*.conf"`）
3. `tail -f` 配合另一个终端往文件里 `echo` 追加内容，观察实时输出

### 2.7 本章掌握标准
- [ ] 15 秒内完成"建目录→进去→建文件→改名→看内容→删掉"全流程
- [ ] 能解释 `ls -l` 第一列每一段的含义
- [ ] 知道 `rm -rf` 的危险性

---

## 第 3 章 文本处理三剑客 + 管道（grep/sed/awk）

> 面试必问"你用 Linux 处理过文本吗"，答案就是这三剑客 + 管道。

### 3.1 管道 `|` 与重定向（灵魂操作）

```bash
command1 | command2      # 把 command1 的输出给 command2 当输入
cat app.log | grep error | head -20   # 层层过滤
ls -l > out.txt          # 覆盖写入文件（> 是覆盖）
ls -l >> out.txt         # 追加写入
command 2> err.log       # 错误输出单独存文件
command > all.log 2>&1   # 标准输出和错误都写进一个文件（部署常用）
```

### 3.2 grep：搜文本

```bash
grep "error" app.log                  # 基础
grep -i "error" app.log               # 忽略大小写
grep -E "error|warn" app.log          # 正则，多关键字
grep -c "error" app.log               # 只数行数
grep -rn "password" /opt/app/         # 递归搜索目录
```

### 3.3 sed：替换文本

```bash
sed 's/old/new/' file.txt             # 每行第一次替换（输出到屏幕，不改文件）
sed 's/old/new/g' file.txt            # 全部替换
sed -i 's/old/new/g' file.txt         # ⚠️ -i 直接改文件（先备份再 -i）
sed -n '5,10p' file.txt               # 打印 5-10 行
sed '/^#/d' nginx.conf                # 删除注释行（d 删除）
```

⚠️ 面试题："sed 怎么改文件？"——答案：必须 `-i`，不加只预览。

### 3.4 awk：按列处理

```bash
awk '{print $1}' file.txt             # 取每行第一列（空格分隔）
awk -F: '{print $1}' /etc/passwd      # 按冒号分隔取第一列（读用户列表）
awk '{print NF, $NF}' file.txt        # 列数、最后一列
awk '$3 > 100 {print $1}' file.txt    # 条件筛选
ps aux | awk '{print $2, $11}'        # 进程 PID 和命令（实战例子）
```

### 3.5 配套命令

```bash
wc -l file.txt       # 行数（统计日志量）
sort file.txt        # 排序
sort -n -r file.txt  # 按数字倒序
uniq -c file.txt     # 去重并计数（需先 sort）
cut -d: -f1 /etc/passwd   # 按分隔符取列（awk 轻量替代）
head -n 3 ... | tail -n 1   # 管道链取中间行
```

**实战组合**（面试现场写这种命令很加分）：

```bash
# 统计访问量最高的 5 个 IP
cat access.log | awk '{print $1}' | sort | uniq -c | sort -rn | head -5

# 找出 2026-08-01 的错误日志条数
grep "2026-08-01" app.log | grep -c "ERROR"
```

### 3.6 练习
1. 用一行命令统计 `/var/log` 下最大的 3 个文件（提示：`ls -lS` 或 `du`）
2. 把 nginx.conf 里所有 `http://` 替换成 `https://` 并保存（sed -i）
3. `ps aux | awk '{print $2}'` 解释输出是什么

### 3.7 本章掌握标准
- [ ] 能写出"grep 过滤 → 排序 → 计数"的管道链
- [ ] 知道 sed 加 `-i` 才改文件、awk 按列取数的基本语法

---

## 第 4 章 权限与用户（服务器管理的核心）

### 4.1 权限模型

Linux 每个文件有三组权限：**属主（u）、属组（g）、其他人（o）**，每组是读（r=4）写（w=2）执行（x=1）。

```bash
-rw-r--r--   # 普通文件：属主可读写，其他人只读
-rwxr-xr-x   # 可执行文件/脚本
drwxr-xr-x   # 目录（d 开头）：读=列出，写=增删文件，执行=进入
```

### 4.2 chmod：改权限

```bash
chmod 755 script.sh        # 数字法：7=rwx 5=r-x 5=r-x
chmod +x script.sh         # 符号法：给所有人加执行权限
chmod u+w file.txt         # 给属主加写权限（u/g/o + a 所有人）
chmod -R 755 app/          # 递归改目录下所有文件
```

数字速记：r=4 w=2 x=1，加起来就是该组的权限（rwx=7, rw-=6, r-x=5, r--=4）。

### 4.3 常见场景：权限不够怎么办

```bash
ls -l file.txt             # 先看当前属主/权限
sudo chown -R www:www /opt/app   # 改属主:属组（chown 需要 root）
sudo chmod 750 /opt/app    # 部署常用：属主全权，组只读执行，其他人不能进
```

⚠️ 排查顺序：**先 ls -l 看权限，再决定 chmod/chown**，不要盲目 chmod 777（777 = 谁都能改，安全隐患）。

### 4.4 用户管理

```bash
whoami                  # 我是谁
sudo useradd -m app     # 建用户并建家目录
sudo passwd app         # 设密码
sudo usermod -aG sudo app   # 把 app 加入 sudo 组
su - app                # 切换用户
sudo -i                 # 切到 root
id app                  # 看用户的 uid/gid/组
```

### 4.5 sudo 与免密

- 普通用户执行管理命令：`sudo 命令`，首次要输自己的密码
- 给某用户免密 sudo（运维常用）：编辑 `/etc/sudoers.d/app`：

```bash
app ALL=(ALL) NOPASSWD: ALL
```

### 4.6 面试题
- "文件权限 755 是什么意思" → 属主 rwx、属组 r-x、其他人 r-x；目录可进入可读，文件可执行
- "为什么服务器不用 root 跑应用" → root 权限过大，一旦应用被攻破/误操作，损失是全系统的；用专用低权限用户
- "部署时权限报 permission denied 怎么排查" → `ls -l` 看属主，确认进程用户，用 chown 归属

### 4.7 本章掌握标准
- [ ] 能说出 rwx 数字对应关系，会用 chmod 数字法
- [ ] 能解释"部署为什么要建专用用户"
- [ ] 遇到 permission denied 知道三步排查（看属主→看进程用户→chown/chmod）

---

## 第 5 章 进程与系统监控 + systemd 服务管理

> "服务器管理"最核心的两件事：进程看护 + 服务自启。全在这一章。

### 5.1 看进程

```bash
ps -ef                    # 全量进程列表（-e 所有 -f 全格式）
ps -ef | grep python      # 找某个程序（面试高频）
ps -ef | grep uvicorn | grep -v grep   # 排除 grep 自身（标准写法）
top                       # 实时监控（按 q 退出，按 1 看多核，按 M 按内存排序）
htop                      # 更好看的 top（需安装）
```

### 5.2 杀进程

```bash
kill 12345                # 温柔终止（发 SIGTERM，让进程自己收尾）
kill -9 12345             # 强制杀（SIGKILL，直接干掉——先温柔后强制）
pkill -f uvicorn          # 按名字杀
kill -9 $(pgrep -f uvicorn)   # 组合拳
```

⚠️ 杀进程顺序：先 `ps -ef | grep` 确认 PID → `kill`（给机会保存）→ 不行再 `kill -9`。你项目里重启后端的脚本就是这个逻辑（netstat 找 8000 端口 → Stop-Process）。

### 5.3 后台运行（部署必用）

```bash
nohup python app.py > app.log 2>&1 &
# nohup = 断开终端也不死；> 输出到日志；2>&1 错误也进日志；& 后台执行

jobs        # 看当前终端后台任务
disown      # 让后台任务脱离终端
```

**进阶**：生产环境不用 nohup，用 systemd（下面）。nohup 适合临时任务。

### 5.4 systemd：服务管理（服务器管理的核心技能）

```bash
systemctl status nginx       # 服务状态
systemctl start nginx        # 启动
systemctl stop nginx         # 停止
systemctl restart nginx      # 重启
systemctl enable nginx       # 开机自启（关键！）
systemctl disable nginx      # 取消自启
systemctl --type=service     # 列出所有服务
```

**写一个 service 单元文件**（部署 Python 应用的标准做法）：

```bash
sudo vim /etc/systemd/system/myapp.service
```

```ini
[Unit]
Description=My FastAPI App
After=network.target

[Service]
User=app
WorkingDirectory=/opt/myapp
ExecStart=/opt/myapp/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
Environment=MYSQL_URL=mysql+pymysql://root:123@127.0.0.1:3306/app

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload      # 改完文件必须 reload
sudo systemctl enable --now myapp # 自启 + 启动
sudo systemctl status myapp       # 看状态
journalctl -u myapp -f            # 看这个服务的日志（-f 跟踪）
```

关键行解读：`User=app`（低权限用户跑）、`ExecStart`（启动命令）、`Restart=always`（崩了自动拉起）、`Environment`（注入环境变量，对应你项目的 MYSQL_URL 注入）。

### 5.5 看端口（排查"服务起了但访问不了"必备）

```bash
ss -tlnp                  # 看所有监听端口和对应进程（netstat 的现代替代）
ss -tlnp | grep 8000      # 8000 端口谁在听
lsof -i:8000              # 谁占用了 8000
curl http://127.0.0.1:8000/api/health   # 本机测服务通不通
```

### 5.6 内存与磁盘

```bash
free -h                   # 内存（Mem/swap）
df -h                     # 磁盘空间（你项目 OpenSearch 数据盘 8% 那次就是看这个）
du -sh /opt/app           # 目录占用
```

### 5.7 练习
1. 写一个 systemd 服务运行一个 `python3 -m http.server 8080`，配好开机自启
2. 用 `ss -tlnp` 找到你服务的 PID，再 `kill` 掉它（再启回来）
3. 用 `journalctl -u 服务名 -f` 观察服务日志

### 5.8 本章掌握标准
- [ ] 能解释 ps/kill 的用法和信号区别（TERM vs KILL）
- [ ] 能手写一个 systemd service 文件并 enable 开机自启
- [ ] 能说出"服务起不来"的排查三步（status → journalctl → 手动跑 ExecStart 看报错）

---

## 第 6 章 网络与远程（SSH + 排查）

### 6.1 SSH 连接与免密（服务器管理每天用）

```bash
ssh root@1.2.3.4                  # 连接（默认 22 端口）
ssh -p 2222 user@1.2.3.4          # 指定端口
ssh user@1.2.3.4 "ls /opt"        # 远程执行一条命令（运维常用）
```

**免密登录（配一次，以后不用输密码）**：

```bash
ssh-keygen -t ed25519             # 本地生成密钥对（一路回车，存 ~/.ssh/id_ed25519）
ssh-copy-id user@1.2.3.4          # 把公钥推到服务器（输入一次密码）
ssh user@1.2.3.4                  # 以后直接进
```

原理：服务器把你的公钥存在 `~/.ssh/authorized_keys`，连接时验证私钥。密钥对 = 私钥（自己留着，**绝不给别人**）+ 公钥（可以分发）。

### 6.2 传文件

```bash
scp file.txt user@1.2.3.4:/opt/          # 本地 → 服务器
scp -r app/ user@1.2.3.4:/opt/           # 目录加 -r
scp user@1.2.3.4:/opt/app.log .          # 服务器 → 本地
sftp user@1.2.3.4                        # 交互式文件传输（get/put/ls）
```

### 6.3 网络排查（面试高频："网站打不开怎么排查"）

排查顺序：**从自己到目标一层层测**。

```bash
ping 1.2.3.4                    # ① 通不通（ICMP）
curl -I http://1.2.3.4          # ② 服务有没有响应（-I 只看头）
curl -v http://1.2.3.4          # ③ 详细过程（握手/重定向/头）
nc -zv 1.2.3.4 80               # ④ 端口通不通（telnet 替代品）
ss -tlnp                        # ⑤ 本机端口有没有在听
```

常见结论：
- ping 不通 → 网络/防火墙（云服务器安全组）
- ping 通但 curl 不通 → 端口没放行（防火墙/安全组）或服务没起
- 本机 curl 通、外网不通 → 防火墙放行端口
- 域名解析问题：`nslookup 域名` / `dig 域名`

### 6.4 防火墙

```bash
# firewalld（CentOS/Rocky 默认）
sudo systemctl status firewalld
sudo firewall-cmd --list-all               # 当前规则
sudo firewall-cmd --permanent --add-port=8000/tcp   # 放行端口
sudo firewall-cmd --reload                 # 生效

# ufw（Ubuntu 默认，WSL 里没防火墙，云服务器有）
sudo ufw status
sudo ufw allow 8000/tcp
```

⚠️ 云服务器还有一层"安全组"（在云控制台配）——ECS 上常见问题：防火墙全放行了还连不上，就是安全组没放行。

### 6.5 面试题
- "SSH 免密怎么配" → 本地生成密钥对 → 公钥放服务器 authorized_keys
- "端口被占用怎么处理" → `ss -tlnp` 找 PID → kill（先 TERM 后 KILL）
- "网站打不开的排查思路" → 分层：DNS → 网络 ping → 端口 nc → 服务 curl → 应用日志

### 6.6 本章掌握标准
- [ ] 能独立完成 SSH 免密配置
- [ ] 能用"ping → curl → nc → ss → 日志"五步排查网络问题
- [ ] 知道防火墙和安全组的区别

---

## 第 7 章 压缩打包（部署搬运必备）

```bash
# tar：打包 + 压缩一步到位
tar -czvf app.tar.gz /opt/app/        # 打包压缩（c 创建 z gzip v 显示 f 文件）
tar -xzvf app.tar.gz                  # 解压（x 解压）
tar -tzvf app.tar.gz                  # 查看包内容（不解压）
tar -xzvf app.tar.gz -C /opt/         # 解压到指定目录

# zip（和 Windows 互通）
zip -r app.zip app/
unzip app.zip -d /opt/app

# 部署经典流程
tar -czvf app_20260826.tar.gz /opt/app   # 先备份
tar -xzvf app.tar.gz -C /tmp/            # 再解压新版
mv /opt/app /opt/app.bak && mv /tmp/app /opt/app   # 原子替换
```

练习：把你的项目目录打包、解压到 /tmp、再改回来。

掌握标准：
- [ ] 会用 tar -czvf/-xzvf 和 zip/unzip
- [ ] 理解"先备份再替换"的部署原则

---

## 第 8 章 软件安装与定时任务

### 8.1 包管理器（按系统二选一）

```bash
# Debian/Ubuntu 系
sudo apt update                     # 更新软件源索引
sudo apt install nginx -y           # 安装
sudo apt remove nginx               # 卸载
sudo apt search 包名                # 搜索

# RedHat/CentOS 系
sudo yum install nginx -y           # 或 dnf
```

### 8.2 Python 环境（部署 Python 应用的核心）

```bash
# 系统 Python + 虚拟环境（服务器标准做法，不用 conda）
sudo apt install python3 python3-venv python3-pip -y
cd /opt/myapp
python3 -m venv venv                # 建虚拟环境
source venv/bin/activate            # 激活（提示符前出现 (venv)）
pip install -r requirements.txt     # 装依赖
```

⚠️ 服务器上**永远用虚拟环境**，不要 `pip install` 到系统 Python（会污染系统，版本冲突）。

### 8.3 环境变量

```bash
export MYSQL_URL="mysql://..."      # 临时（当前终端）
echo 'export MYSQL_URL="mysql://..."' >> ~/.bashrc   # 永久（当前用户）
source ~/.bashrc                    # 生效
# 或写入 systemd 的 Environment= 行（服务专属，推荐）
```

### 8.4 crontab 定时任务（运维标配）

```bash
crontab -e                # 编辑当前用户的定时任务
crontab -l                # 查看
```

格式：`分 时 日 月 周 命令`

```bash
# 每天凌晨 2 点备份数据库
0 2 * * * /opt/scripts/backup.sh >> /var/log/backup.log 2>&1
# 每 10 分钟健康检查
*/10 * * * * curl -s http://127.0.0.1:8000/api/health || echo "DOWN" >> /var/log/health.log
```

⚠️ crontab 的坑：环境变量不继承（要在脚本里自己 export）；命令要写绝对路径。

### 8.5 本章掌握标准
- [ ] 能建虚拟环境并安装依赖
- [ ] 能写一个 crontab（每天备份日志/定时健康检查）
- [ ] 知道永久环境变量写在 ~/.bashrc 或 systemd

---

## 第 9 章 日志与排错（从"能用"到"会管理"的分水岭）

### 9.1 日志在哪

```bash
/var/log/             # 系统日志目录
  syslog/messages     # 系统日志
  nginx/access.log    # 访问日志
  nginx/error.log     # Nginx 错误日志
journalctl -u 服务名   # systemd 服务的日志（最常用）
```

### 9.2 排错六步法（背下来，面试 + 实战都用）

1. **服务状态**：`systemctl status 服务名`（看 Active: failed/running）
2. **服务日志**：`journalctl -u 服务名 -n 50 --no-pager`（最后 50 行）
3. **手动复现**：手动跑 ExecStart 那条命令，看直接报错（绕过 systemd）
4. **端口与进程**：`ss -tlnp`、`ps -ef | grep 进程名`
5. **本机测**：`curl http://127.0.0.1:8000/api/health`
6. **外部测**：防火墙/安全组/域名

### 9.3 三个高频故障演练（对着做一遍）

**故障 A：服务起不来**
```bash
systemctl status myapp          # 看到 failed
journalctl -u myapp -n 30       # 看报错：ModuleNotFoundError / 端口被占 / 权限
# 常见原因与解法：
#   依赖没装        → 激活 venv 后 pip install -r requirements.txt
#   端口被占        → ss -tlnp 找到旧进程杀掉
#   权限不足        → ls -l 检查目录属主，chown -R app:app /opt/myapp
```

**故障 B：端口开了但外网访问不了**
```bash
ss -tlnp | grep 8000            # ① 本机在听吗（0.0.0.0 还是 127.0.0.1？）
# 如果是 127.0.0.1 → 只监听本机，改 --host 0.0.0.0
curl http://127.0.0.1:8000      # ② 本机通吗
firewall-cmd --list-all         # ③ 防火墙放行了吗
# ④ 云控制台安全组放行了吗
```

**故障 C：磁盘满了**
```bash
df -h                           # 哪个盘满了
du -sh /var/log/* | sort -rh | head -5   # 谁占的大
journalctl --vacuum-size=100M   # 清理 systemd 日志
# 日志轮转配置在 /etc/logrotate.d/
```

### 9.4 本章掌握标准
- [ ] 能把"排错六步法"默写出来
- [ ] 独立完成故障 A/B/C 三个演练

---

## 第 10 章 综合实战：完整部署一个 FastAPI + Vue 项目

> 这是把前面所有章节串起来的最终考试。场景：把类似你的 rag 项目部署到一台干净服务器。

### 10.1 部署步骤总览

```bash
# 1. 连接与准备
ssh root@服务器IP
sudo apt update && sudo apt install -y python3-venv nginx git

# 2. 建部署用户（第 4 章）
sudo useradd -m app && sudo passwd app
sudo -u app -s /bin/bash         # 切到 app 用户

# 3. 拉代码（第 6 章 scp 或 git）
cd /opt && sudo git clone https://github.com/你/项目.git myapp
sudo chown -R app:app /opt/myapp

# 4. 装 Python 依赖（第 8 章）
cd /opt/myapp/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export MYSQL_URL="..."           # 先手动跑通，确认能启动
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 前台试跑，Ctrl+C 停

# 5. systemd 托管（第 5 章）
sudo vim /etc/systemd/system/myapp.service        # 写单元文件
sudo systemctl daemon-reload && sudo systemctl enable --now myapp
curl http://127.0.0.1:8000/api/health             # 本机验证

# 6. 前端构建 + Nginx（第 6 章 + 项目 nginx.conf）
cd /opt/myapp/frontend && npm install && npm run build
sudo cp -r dist/* /usr/share/nginx/html/
sudo vim /etc/nginx/conf.d/myapp.conf             # 抄你项目的 nginx.conf
sudo nginx -t && sudo systemctl reload nginx      # 校验配置再重载

# 7. 防火墙放行（第 6 章）
sudo firewall-cmd --permanent --add-service=http && sudo firewall-cmd --reload
```

### 10.2 排错（部署不顺利时按第 9 章六步法）

- 后端 500：`journalctl -u myapp -f` 看 Python 报错
- 前端白屏：`curl -I http://IP/` 看 Nginx 有没有托管到；检查 nginx.conf 的 try_files
- 接口 502：Nginx 连不上后端——`ss -tlnp | grep 8000` 确认后端在听、`proxy_pass` 地址对不对

### 10.3 本章掌握标准（最终验收）
- [ ] 独立完成 10.1 全流程（允许查文档，不允许查答案）
- [ ] 部署后能讲清楚：代码在哪、服务怎么托管、日志怎么查、怎么重启、怎么备份

---

## 附录 A：命令速查表（打印贴墙）

```bash
# 文件
pwd cd ls -lh mkdir -p touch rm -rf(慎) cp -r mv cat head tail -f less
# 文本
grep -rn sed -i 's/x/y/g' awk '{print $1}' sort uniq -c wc -l cut -d:
# 权限
chmod 755 chown user:group su sudo ls -l
# 进程
ps -ef top kill -9 systemctl(start/stop/restart/status/enable) journalctl -u
# 网络
ssh scp curl -v nc -zv ss -tlnp ping firewall-cmd
# 压缩
tar -czvf tar -xzvf zip -r unzip
# 磁盘
df -h du -sh free -h
```

## 附录 B：面试自查清单（对标简历"熟悉 Linux 常用命令与服务器管理"）

| 简历要求 | 对应章节 | 自评（熟练/会/不会） |
|---|---|---|
| Linux 常用命令 | 第 1-3 章 | □ |
| 文件与权限管理 | 第 2、4 章 | □ |
| 进程与服务管理 | 第 5 章 | □ |
| 服务器管理 | 第 5、6、8、9 章 | □ |
| 阿里云 ECS 部署 | 第 10 章 | □ |
| Nginx 反向代理 | 第 6 章 + 10.1-6 | □ |

面试前把"不会"的章节重做一遍练习，直到全部"熟练"。

## 附录 C：学习路线（一周计划）

- **Day 1**：第 0-1 章（装环境 + 路径 + Tab/man）
- **Day 2**：第 2 章（文件操作，全部命令敲 10 遍）
- **Day 3**：第 3 章（管道 + grep/sed/awk，做 3.6 练习）
- **Day 4**：第 4-5 章（权限 + systemd，写一个自己的 service 文件）
- **Day 5**：第 6 章（SSH 免密 + 网络排查 + 防火墙）
- **Day 6**：第 7-8 章（打包 + 虚拟环境 + crontab）
- **Day 7**：第 9-10 章（排错六步法 + 完整部署实战 + 附录 B 自评）

