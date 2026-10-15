# Linux 部署指南 · 从克隆仓库到服务启动

> 本文是 [nav-server](nav-server.py) 的完整部署实操手册，面向「拿到一台 Linux 机器、
> 只下载了这一个仓库」的场景，一步步走到浏览器能打开导航页为止。
>
> 全程只用系统自带工具，不需要 pip 安装任何东西。

---

## 0. 前置要求

| 项目 | 要求 | 检查命令 |
| --- | --- | --- |
| Python | **3.7 或更高**（推荐 3.9+） | `python3 --version` |
| git | 任意版本（用于克隆和检测更新） | `git --version` |
| 磁盘 | 几 MB 足矣 | — |

> 绝大多数现代发行版自带 Python 3 和 git。若 `python3 --version` 报 `command not found`，
> 看文末的[排错](#9-常见问题)一章。

---

## 1. 从仓库下载到本地

### 方式一：git clone（推荐）

```bash
# 挑一个放项目的地方，这里以 ~/apps 为例
mkdir -p ~/apps && cd ~/apps

# HTTPS 克隆
git clone https://github.com/simiomxiaomi/nav-server.git

cd nav-server
```

如果你配了 SSH key，也可以用：

```bash
git clone git@github.com:simiomxiaomi/nav-server.git
```

### 方式二：下载压缩包

不想装 git 的话，在 GitHub 页面点 **Code → Download ZIP**，然后：

```bash
mkdir -p ~/apps && cd ~/apps
unzip nav-server-main.zip
mv nav-server-main nav-server
cd nav-server
```

> ⚠️ **没有 git 也能跑**：拉取项目时会自动改用 GitHub 压缩包下载（`zipFallback`）。
> 「检测更新」则会回退到 GitHub API，网络受限时会提示检测失败。想要完整体验就装一下 git。

### 克隆下来应该看到这些文件

```bash
ls -1
```

```
LICENSE
README.md
config.example.json
config.json
nav-server.py
nav-server.sh
```

---

## 2. 配置（部署时唯一需要动脑的地方）

用编辑器打开配置文件：

```bash
nano config.json      # 或者 vim / vi
```

### 2.1 先决定一件事：`root` 指向哪个目录

`root` 是**项目根目录**，所有 `projects[].path` 都相对于它解析。默认值是 `".."`，
意思是「本仓库的上一级」。

**刚 clone 下来时会有个问题**：默认清单里那 9 个项目（markdown-studio、diff-viewer…）
其实还没下载，导航页会把它们标成「目录不存在」。这**不影响服务运行**，而且从 v1.1 起
可以**让服务自己把它们拉下来**（见下面的场景 D）。

有四种处理方式，按需选一个：

#### 场景 A：只想留 nav-server 自己，作为通用导航台（推荐首次部署）

改 `root` 为你真正放项目的目录，然后把 `projects` 精简成自己的清单：

```json
{
  "port": 1938,
  "root": "/home/你的用户名/apps",
  "projects": [
    {
      "id": "my-site",
      "name": "我的网站",
      "icon": "🌐",
      "path": "my-site",
      "desc": "自己的静态站点",
      "tag": "Web",
      "repo": ""
    }
  ]
}
```

> `root` 用**绝对路径**最省心（例如 `/home/suxiaohan/apps` 或 `/opt/www`）。
> 用相对路径时是相对**配置文件所在目录**，也就是 nav-server 仓库目录。

#### 场景 B：把这些项目也克隆下来，跟 nav-server 并排放

```bash
cd ~/apps
git clone https://github.com/simiomxiaomi/diff-viewer.git
git clone https://github.com/simiomxiaomi/color-lab.git
# …其他项目同理
```

克隆完目录结构是：

```
~/apps/
├── nav-server/          ← 就是本仓库
├── diff-viewer/
├── color-lab/
└── markdown-studio/
```

这时保持 `root` 为默认的 `".."` 即可，导航页上这些项目自动变成「就绪」。

#### 场景 C：一个项目都不克隆，只看 nav-server 自身

把 `projects` 改成空数组，导航页会显示「该目录下暂时没有项目」，但服务照常跑：

```json
{ "port": 1938, "root": "..", "projects": [] }
```

#### 场景 D：让 nav-server 自己把它们拉下来（v1.1 推荐）

不改配置，也不用自己敲 9 条 `git clone`。默认 `"autoSync": "always"`，
服务一启动就在后台把缺失的项目克隆下来、已有的更新到最新：

```bash
python3 nav-server.py --sync
```

输出长这样：

```
开始拉取 9 个项目…（git 不可用时会回退到压缩包下载）

  [已部署] diff-viewer        已克隆 main 分支
  [已部署] color-lab          已克隆 main 分支
  [已更新] markdown-studio    已更新到 1f3c9ab
  [已最新] json-viewer        已是最新（9a2b1c3）
  [已跳过] my-notes           目录已存在且不是 git 仓库，跳过（不动你自己的文件）
  [失败  ] focus-timer        克隆失败：unable to access 'https://github.com/…'

拉取完成：已部署 2，已更新 1，已最新 1，已跳过 1，失败 1
```

也可以不敲命令，启动服务后直接在导航页点「⇩ 部署 / 拉取全部」。
只想装缺失的：`--sync --missing-only`；只处理某几个：`--sync --only a,b`。

> 没有 git 时它会去下载 GitHub 压缩包解压（`"zipFallback": true`）；
> 网络受限就给 `config.json` 配个镜像前缀 `{ "proxy": "https://ghproxy.net" }`。
> 已存在但不是它部署的目录一律跳过，不会覆盖你自己的文件。

### 2.2 要改的关键字段

| 字段 | 默认 | 怎么改 |
| --- | --- | --- |
| `port` | `1938` | 换成任意空闲端口。被占用时启动会明确报错 |
| `host` | `0.0.0.0` | 想只允许本机访问就改 `127.0.0.1`；局域网/外网访问保持 `0.0.0.0` |
| `root` | `..` | 项目根目录，见上面三种场景 |
| `title` | `工作区导航` | 导航页标题 |
| `cacheTTL` | `300` | 更新检测结果缓存秒数，`0` = 每次都重新检测 |
| `proxy` | `""` | 网络受限时填 GitHub API / 压缩包镜像前缀，如 `https://ghproxy.net` |
| `openOnStart` | `false` | 服务器上一般不用开；本机桌面用可以设 `true` |
| `autoSync` | `"always"` | 启动时自动拉取：`"always"` 补缺失+更新已有 / `"missing"` 只补缺失 / `false` 关闭 |
| `syncMode` | `"ff"` | `"ff"` 只允许快进合并（安全）；`"hard"` 强制对齐远端（会丢弃本地改动） |
| `zipFallback` | `true` | 没装 git 或 clone 失败时，回退到 GitHub 压缩包下载解压 |
| `syncTimeout` | `120` | clone / pull / 下载的超时秒数，慢网络调大 |

> 端口别选 80 / 443（需要 root 权限），也别选已被占用的。查占用见[排错](#9-常见问题)。

### 2.3 校验配置写得对不对

不用启动服务，先跑一次列表检查：

```bash
python3 nav-server.py --list
```

正常输出类似：

```
ID                   状态       文件数    目录
------------------------------------------------------------------------------
diff-viewer          就绪       7      /home/suxiaohan/apps/diff-viewer
color-lab            缺失       0      /home/suxiaohan/apps/color-lab
```

- **就绪** = 目录存在且有 `index.html`，可以打开
- **缺入口** = 目录在，但没找到 `index.html`
- **缺失** = 目录不存在（没克隆，或者 `path` 写错了）

如果配置有语法错误，这里会直接告诉你**第几行第几列**，而不是启动后才炸。

---

## 3. 启动服务

### 方式一：前台运行（第一次部署建议这样）

```bash
python3 nav-server.py
```

终端会打印一块横幅，列出每个项目的就绪状态：

```
──────────────────────────────────────────────────────────────
  工作区导航  ·  nav-server v1.1.0
──────────────────────────────────────────────────────────────
  配置       /home/suxiaohan/apps/nav-server/config.json
  项目根目录 /home/suxiaohan/apps
  监听       0.0.0.0:1938
  导航页     http://127.0.0.1:1938/
  状态接口   http://127.0.0.1:1938/api/status
  自动拉取   开启 · 启动时补齐缺失 + 更新已有
──────────────────────────────────────────────────────────────
  [✓] diff-viewer        → /apps/diff-viewer/
  [×] color-lab          → /apps/color-lab/
──────────────────────────────────────────────────────────────
  就绪 1/2 个项目（! = 缺 index.html，× = 目录不存在）
  按 Ctrl+C 停止服务
──────────────────────────────────────────────────────────────
```

看到这些就是起来了。**Ctrl+C 停止**。

### 方式二：后台运行 + 脚本管理（推荐长期使用）

```bash
chmod +x nav-server.sh      # 只需执行一次
./nav-server.sh start
```

输出：

```
[nav-server] 启动服务…（工作区导航 · 端口 1938）
[nav-server] 启动成功 — PID 12345
[nav-server] 导航页：http://127.0.0.1:1938/
[nav-server] 日志：  /home/suxiaohan/apps/nav-server/nav-server.log
```

脚本会在启动前**检查端口是否被占用**、启动后**轮询健康接口确认真的起来了**，
失败了会把日志最后几行打给你看，不会让你对着一个假 PID 发呆。

其他命令：

```bash
./nav-server.sh status      # 看进程状态 + 健康检查 + 项目清单
./nav-server.sh sync        # 拉取 / 部署一遍项目（缺失的克隆，已有的更新）
./nav-server.sh log         # tail -f 实时日志
./nav-server.sh restart     # 改完配置用这个
./nav-server.sh stop        # 停止
./nav-server.sh doctor      # 环境自检（python3 / 配置 / 端口 / git）
```

---

## 4. 验证部署成功

三条命令，从内到外验证：

```bash
# 1) 进程在不在（方式二才有 PID 文件）
./nav-server.sh status

# 2) 端口在不在听
ss -ltnp | grep 1938

# 3) 服务健康不健康
curl -s http://127.0.0.1:1938/api/health
```

期望看到：

```json
{"ok": true, "service": "nav-server", "version": "1.1.0", "time": 1791986223.12}
```

再看看项目状态：

```bash
curl -s http://127.0.0.1:1938/api/status | python3 -m json.tool
```

最后在浏览器打开：

```
http://服务器IP:1938/
```

如果是远程服务器，把 `服务器IP` 换成它的公网/内网地址。

---

## 5. 配置为开机自启（systemd 用户服务）

```bash
./nav-server.sh install-service
```

它会生成 `~/.config/systemd/user/nav-server.service` 并打印后续命令：

```bash
systemctl --user daemon-reload
systemctl --user enable --now nav-server
```

**想让它在你没登录时也运行**（真正的服务器场景，必做）：

```bash
sudo loginctl enable-linger $USER
```

常用管理命令：

```bash
systemctl --user status nav-server      # 状态
systemctl --user restart nav-server     # 重启
journalctl --user -u nav-server -f      # 实时日志
```

卸载：

```bash
systemctl --user disable --now nav-server
rm ~/.config/systemd/user/nav-server.service
```

---

## 6. 防火墙：让别的机器能访问

服务监听 `0.0.0.0` 了，但系统防火墙可能还挡着。按发行版选一条：

**Ubuntu / Debian（ufw）**

```bash
sudo ufw allow 1938/tcp
sudo ufw status
```

**CentOS / RHEL / Rocky（firewalld）**

```bash
sudo firewall-cmd --permanent --add-port=1938/tcp
sudo firewall-cmd --reload
```

**云服务器（阿里云 / 腾讯云等）**

还需要在**控制台的「安全组 / 防火墙」里放行 1938 端口**——只改系统防火墙是不够的。

> ⚠️ **安全提醒**：nav-server 没有登录认证，任何能访问该端口的人都能看你所有项目的源码。
> 公网暴露前请想清楚，或者：
> - 只在需要时临时开（用完 `sudo ufw delete allow 1938/tcp`）
> - 把 `host` 改成 `127.0.0.1`，用 SSH 端口转发访问：`ssh -L 1938:127.0.0.1:1938 user@服务器`
> - 前面挂一层带认证的反向代理（Nginx basic auth 等）

---

## 7. 日常维护

```bash
cd ~/apps/nav-server

./nav-server.sh status          # 看一眼状态
./nav-server.sh sync            # 把项目更新到最新（也可以点导航页的「⇩ 部署 / 拉取全部」）
./nav-server.sh log             # 排查问题
./nav-server.sh restart         # 改完配置重启
git pull && ./nav-server.sh restart   # 更新自身
```

**改了项目清单 / 端口**：改完 `config.json` 后重启；或者直接点导航页上的「↻ 重载配置」按钮，
不用重启进程（命令行覆盖的端口会被保留）。

---

## 8. 卸载

```bash
./nav-server.sh stop
./nav-server.sh install-service >/dev/null 2>&1 || true   # 若装过服务则先卸载（见第 5 节）

cd ..
rm -rf nav-server
```

服务本身不往系统里写任何东西，删掉目录就干净了。

---

## 9. 常见问题

### `python3: command not found`

按发行版装：

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y python3

# CentOS / RHEL 8+
sudo dnf install -y python3

# Alpine
sudo apk add python3
```

### 端口被占用

服务启动时会直接告诉你，不会丢一堆报错。要查是谁占的：

```bash
sudo ss -ltnp | grep 1938
# 或者
sudo lsof -i :1938
```

要么停掉那个程序，要么换个端口（改 `config.json` 的 `port`）。

### `./nav-server.sh: Permission denied`

```bash
chmod +x nav-server.sh
```

### 启动脚本说「启动失败」

它会自动把日志最后 20 行打出来。也可以手动看：

```bash
cat nav-server.log
```

### 本机能开，手机 / 别的电脑打不开

按顺序排查：

1. `config.json` 里 `host` 是不是 `0.0.0.0`？
2. 系统防火墙放行了吗？（第 6 节）
3. 云服务器安全组放行了吗？（第 6 节）
4. 从别的机器 `curl http://服务器IP:1938/api/health` 通不通？

### 导航页上项目标「缺失」

**先别急着手动克隆**——从 v1.1 起服务会自己拉：

```bash
python3 nav-server.py --sync      # 或导航页点「⇩ 部署 / 拉取全部」
```

默认 `"autoSync": "always"`，启动时本来就会在后台拉。要是拉完还是「缺失」：

- 看日志里 `[nav-server] 同步 xxx [失败] …` 的具体原因（网络 / 认证 / 分支名）
- 目录真的不存在且拉不动 → 手动 `git clone` 到 `root` 对应的位置，或修正 `path`
- `root` 指错地方了 → 用 `python3 nav-server.py --list` 看它实际找的是哪个路径

标「缺入口」则是目录在，但里面没有 `index.html`。

### 拉了但是「已跳过」

说明那个目录已经存在、又不是 nav-server 部署的 git 仓库（比如你自己手动放的代码），
服务**故意不碰它**，避免覆盖你的文件。想让它接管就删掉该目录再拉一次。

### 「检测更新」一直失败

卡片上会写明原因。常见情况：

- **没装 git** → `sudo apt install -y git`
- **网络到不了 GitHub** → 在 `config.json` 里配镜像：
  ```json
  { "proxy": "https://ghproxy.net" }
  ```
- **本地不是 git 仓库** → 如果项目是直接下载 ZIP 来的，就没有 `.git` 目录，
  无法比对版本。用 `git clone` 重新拉一份即可。

### 服务起不来，但日志里什么都没

试试前台运行看真实报错：

```bash
python3 nav-server.py
```

前台会直接把异常打在屏幕上。

---

## 10. 一键命令汇总（懒人版）

```bash
# ① 下载
mkdir -p ~/apps && cd ~/apps && git clone https://github.com/simiomxiaomi/nav-server.git && cd nav-server

# ② 把项目拉下来（缺失的克隆，已有的更新）
python3 nav-server.py --sync

# ③ 看一眼配置与就绪情况（可选：按第 2 节改成自己的）
python3 nav-server.py --list

# ④ 启动（前台）
python3 nav-server.py

# ⑤ 另开一个终端验证
curl -s http://127.0.0.1:1938/api/health
```

就这么五步。要是想后台常驻：

```bash
chmod +x nav-server.sh
./nav-server.sh start
./nav-server.sh status
```

---

## 附：想把自己的项目也放上导航页

编辑 `config.json`，往 `projects` 数组里加一条即可，重启后自动生效：

```json
{
  "id": "blog",
  "name": "我的博客",
  "icon": "📝",
  "path": "blog",
  "desc": "用静态生成器做的博客",
  "tag": "Web",
  "repo": "https://github.com/你的用户名/你的博客仓库"
}
```

- `id` 决定访问路径：`/apps/blog/`，只能包含字母、数字、点、下划线、连字符
- `path` 是相对 `root` 的目录名
- `desc` 会显示在卡片上，也会被搜索收录
- `repo` 填了才能用「检测更新」；留空则该项目的检测会提示缺少仓库地址

---

## 相关文档

- [README.md](README.md) — 项目总览、配置字段详解、接口列表
- [config.example.json](config.example.json) — 带逐字段说明的配置样例

---

<p align="center"><sub>© 2026 苏小晗 · MIT License</sub></p>
