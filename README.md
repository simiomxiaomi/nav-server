# nav-server · 工作区导航服务

![License](https://img.shields.io/badge/License-MIT-00e59b)
![Python](https://img.shields.io/badge/Python-3.7%2B-38bdf8)
![Dependencies](https://img.shields.io/badge/dependencies-0-8fa2b5)

> 把 `E:\工作区DS`（或任意目录）下的所有纯前端项目托管到**一个** HTTP 服务上，
> 配一个导航页，点一下就能打开任意项目。默认端口 **1938**，改配置文件即可换。
>
> 项目没下载也没关系：**服务会自动把它们拉下来**——有 git 就 `git clone` / `git pull`，
> 没有 git 就下载 GitHub 压缩包解压。装完就是能直接用的状态。

零第三方依赖，只用 Python 标准库。

---

## 它解决什么问题

原来每个项目要单独起一个静态服务器、记一堆端口。现在：

```
python nav-server.py          →  打开 http://127.0.0.1:1938/
                                   ↓ 点卡片
                               /apps/diff-viewer/   ← 直接开跑
                               /apps/color-lab/
                               /apps/markdown-studio/
                               ……
```

一个进程、一个端口、一个入口，全部项目就都活了。

---

## 两种部署方式

> 📖 **完整部署流程（从克隆仓库到服务启动、开机自启、防火墙、排错）见
> [DEPLOY-LINUX.md](DEPLOY-LINUX.md)。** 下面是精简版。

### 方式一：直接用 Python 启动（最省事）

```bash
cd nav-server
python nav-server.py
```

打开 <http://127.0.0.1:1938/> 即可。

> 第一次启动时如果 `projects` 里的目录还没下载，服务会在后台自动把缺失的项目拉下来
> （见「[自动拉取 / 部署项目](#自动拉取--部署项目v11-新增)」），导航页上能看到实时进度。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-c, --config <路径>` | 指定配置文件（默认同目录 `config.json`） |
| `-p, --port <端口>` | **临时**覆盖端口，不改配置文件 |
| `--host <地址>` | 临时覆盖监听地址 |
| `--list` | 只列出项目就绪情况，不启动服务 |
| `--sync` | 拉取 / 部署一遍项目后退出（缺失的克隆、已有的更新），不启动服务 |
| `--only a,b` | 配合 `--sync`：只处理指定项目 |
| `--missing-only` | 配合 `--sync`：只安装缺失的项目，不更新已有的 |
| `--no-auto-sync` | 本次启动不做自动拉取（临时覆盖配置里的 `autoSync`） |
| `-q, --quiet` | 不打印每个请求的访问日志 |
| `-v, --version` | 打印版本 |

### 方式二：用 shell 脚本管理（Linux / macOS / WSL）

```bash
cd nav-server
chmod +x nav-server.sh

./nav-server.sh              # 后台启动
./nav-server.sh status       # 看状态 + 项目清单
./nav-server.sh log          # 实时跟踪日志
./nav-server.sh restart      # 重启（改完配置用这个）
./nav-server.sh stop         # 停止
./nav-server.sh doctor       # 环境自检
```

全部子命令：

| 命令 | 作用 |
| --- | --- |
| `start` | 后台启动（默认） |
| `fg` | 前台运行，Ctrl+C 停止（调试用） |
| `stop` / `restart` | 停止 / 重启 |
| `status` | 进程状态 + HTTP 健康检查 + 项目清单 |
| `list` | 只看项目就绪情况 |
| `sync` | 拉取 / 部署一遍项目（相当于 `python nav-server.py --sync`） |
| `log` | `tail -f` 日志 |
| `config` | 打印当前生效配置 |
| `doctor` | 自检 python3 / 配置 / 端口 / git |
| `install-service` | 生成 systemd 用户服务，可开机自启 |

Windows 上如果没有 bash，用**方式一**就行。

---

## 改端口（重点）

端口写在 `config.json` 的 `port` 字段里，**默认 1938**：

```json
{
  "port": 1938
}
```

改完重启生效：

```bash
# 方式一：Ctrl+C 停掉，再 python nav-server.py
# 方式二：
./nav-server.sh restart
```

只想临时换个端口、不改文件：

```bash
python nav-server.py --port 8080
```

> 端口被占用时服务会直接告诉你，而不是丢一堆 traceback。
> 想看谁占了：`ss -ltnp | grep 1938`（Linux）或 `netstat -ano | findstr 1938`（Windows）。

---

## 配置文件详解

```json
{
  "port": 1938,                 // 监听端口，默认 1938
  "host": "0.0.0.0",            // 0.0.0.0 允许局域网访问；只想本机用就写 127.0.0.1
  "title": "工作区导航",         // 导航页标题
  "root": "..",                 // 项目根目录（相对配置文件所在目录；也可以用绝对路径）
  "cacheTTL": 300,              // 「检测更新」结果缓存秒数，0 = 每次都重新检测
  "remote": "origin",           // git 远端名
  "branch": "main",             // 拉取 / 对比哪个分支
  "proxy": "",                  // 可选：GitHub API / 压缩包镜像前缀，见下文
  "openOnStart": false,         // 启动后自动打开浏览器
  "timeout": 6,                 // git / 网络检测超时秒数
  "autoSync": "always",         // 启动时自动拉取：always / "missing" / false
  "syncMode": "ff",             // 更新方式：ff（只允许快进）/ hard（强制对齐远端）
  "zipFallback": true,          // 没有 git 或 clone 失败时，回退到下载压缩包
  "syncTimeout": 120,           // clone / pull / 下载的超时秒数
  "projects": [
    {
      "id": "diff-viewer",                          // 访问路径用，只能字母数字点下划线连字符
      "name": "Diff Viewer",                        // 卡片标题
      "icon": "±",                                  // 卡片图标（emoji 或文字都行）
      "path": "diff-viewer",                        // 相对 root 的目录名
      "desc": "文本对比器 · LCS 逐行差异",            // 一句话描述
      "tag": "Tool",                                // 分类标签，仅用于展示
      "repo": "https://github.com/…/diff-viewer"    // 仓库地址，拉取与检测更新时用
    }
  ]
}
```

**加一个新项目**：往 `projects` 里塞一条、重启，导航页就有了（统计数字、搜索范围也会自动跟上），
缺失的目录会被自动拉下来。

**配置写错了**：服务会明确告诉你是第几行第几列的问题，不会静默失败。

---

## 自动拉取 / 部署项目（v1.1 新增）

**场景**：你把 nav-server 部署到一台机器上，`projects` 里列了 9 个项目，但那些项目根本没下载过
——导航页一整排 `[×] 目录不存在`。以前只能自己一个个 `git clone`，现在交给服务就好。

### 三种触发方式

1. **启动时自动**（默认开）：`"autoSync": "always"`，服务一起来就在后台补齐缺失、更新已有。
   只想补缺失就写 `"missing"`；完全不自动就写 `false`。
2. **导航页按钮**：工具栏「⇩ 部署 / 拉取全部」，或每张卡片上的「⇩ 部署」/「⇩ 拉取更新」。
   进度会实时显示在卡片上，不刷新页面。
3. **命令行**：`python nav-server.py --sync`（Linux 也可以 `./nav-server.sh sync`）。
   只想装缺失的：`--sync --missing-only`；只弄某几个：`--sync --only diff-viewer,color-lab`。

### 每个项目怎么处理

| 本地情况 | 动作 |
| --- | --- |
| 目录不存在 | `git clone`（带 `--branch <branch>`；分支不存在就改用仓库默认分支） |
| 目录是 git 仓库 | `git pull --ff-only`；没配上游分支就退回 `fetch` + `merge --ff-only` |
| 目录是压缩包部署的（有标记文件） | 重新下载压缩包覆盖更新 |
| 目录存在但不是 git 仓库、也不是它部署的 | **跳过**，绝不动你自己的文件 |
| 没有 `repo` 字段 | 跳过 |

`syncMode` 为 `"hard"` 时改成 `git fetch` + `git reset --hard origin/<branch>`，
会**丢弃本地改动**，只在确定本地不需要留东西时才用。

### 没有 git 也能部署

`"zipFallback": true`（默认）时，git 不存在或克隆失败会回退到下载 GitHub 压缩包
（`https://codeload.github.com/<owner>/<repo>/zip/refs/heads/<branch>`）解压到目标目录。
这种目录会留一个 `.nav-server-zip` 标记，之后「拉取更新」会重新下载覆盖它。
网络受限时给 `proxy` 配镜像前缀，压缩包地址也会走镜像。

### 放到哪儿

clone 下来的目录 = `root` + `projects[].path`。默认 `root` 是 `".."`，
也就是 nav-server 目录的上一级，所以默认会跟 nav-server 并排放。

> 服务是无人值守的：git 命令都设了 `GIT_TERMINAL_PROMPT=0`，绝不会卡在「输入密码」上。

---

## 更新检测怎么工作

导航页每张卡片上都有「⟳ 检测更新」，也会显示状态：

| 状态 | 含义 |
| --- | --- |
| 已是最新 | 远端和本地那个 commit 一致 |
| 有新版本 | 远端有新提交，并告诉你落后几个 |
| 有本地改动 | 有未提交的改动（显示文件数） |
| 未纳入 git | 本地不是 git 仓库 |
| 检测失败 | 网络不通或 git 不可用，会给出原因 |

检测按这个顺序尝试：

1. **`git ls-remote`** — 只读取远端引用，**不拉代码、不改动你任何文件**。最快最准。
2. 失败就**回退到 GitHub API** — 用 `repo` 字段解析出 `owner/repo` 去查最新提交。
3. 再失败就报告原因，并提示你可以配 `proxy`。

**注意**：这台机器的 `hosts` 把 GitHub 全系域名指到了 `127.0.0.1`，所以本地大概率会走不通。两种解法：

- 装好 `git`、并让 git 能访问远端（配代理或镜像）
- 或者给 `config.json` 加个镜像前缀：

```json
{ "proxy": "https://ghproxy.net" }
```

配了之后会请求 `https://ghproxy.net/https://api.github.com/repos/<owner>/<repo>/commits/main`。

> 检测结果默认缓存 300 秒，避免反复戳远端。点「检测更新」时会强制刷新。

---

## 导航页功能

- **搜索**：按项目名 / 描述 / 路径 / 分类实时过滤（按 <kbd>/</kbd> 快速聚焦）
- **部署 / 拉取全部**：一键把缺失的项目克隆下来、把已有的更新到最新，卡片上实时显示进度
- **检测全部更新**：串行跑一遍，实时显示进度，结束时汇总有几个项目有新版本
- **重载配置**：改了 `config.json` 不用重启，点一下就重新读
- **服务状态**：`/api/status` 返回端口、根目录、Python 版本、运行时长、项目清单
- 项目目录不存在或缺 `index.html` 会明确标出来，不会点开一片空白

---

## 接口一览

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/` | 导航页 |
| GET | `/apps/<id>/` | 打开对应项目 |
| GET | `/api/health` | 健康检查（用于启动探测） |
| GET | `/api/status` | 服务与项目状态 |
| GET | `/api/check-update?id=<id>[&force=1]` | 检测单个项目 |
| GET | `/api/check-update?force=1` | 检测全部项目 |
| POST | `/api/sync[?id=<id>]` | 后台拉取 / 部署（不带 id 就是全部） |
| GET | `/api/sync-status` | 拉取进度与结果（前端轮询用） |
| POST | `/api/reload-vault` | 重新读取 `config.json` |

---

## 安全说明

- 静态文件服务做了**路径穿越防护**：`../`、URL 编码的 `..%2f` 都会被拦掉，不会读到项目目录之外
- 项目 `id` 只允许 `[A-Za-z0-9._-]`，从配置层就堵住花样路径
- 导航页里的项目名 / 描述做了 HTML 转义，`config.json` 里塞 `<script>` 也不会被执行
- 压缩包解压同样挡掉 `../` 与绝对路径条目
- 默认监听 `0.0.0.0`，**局域网内可访问**。只想本机用就把 `host` 改成 `127.0.0.1`
- `/api/sync` 会让服务在本机执行 `git clone` / `git pull`，所以**别把端口直接暴露到公网**
  （真要暴露，至少用前面带认证的反向代理）；已存在但不是它管理的目录一律跳过，不会覆盖你的文件
- 服务本身不写任何数据，除了日志文件、拉取下来的项目目录和 `.nav-server-zip` 标记

---

## 验证记录

```bash
python _selftest.py        # 逻辑自测
python _e2e.py             # 端到端实测（真启服务）
python _check_bash.py nav-server.sh   # shell 结构检查
python _check_docs.py      # 文档一致性检查
```

**已通过：**

- `_selftest.py` — 配置解析与各类非法配置的拒绝（含新增的 `autoSync` / `syncMode` / `zipFallback` / `syncTimeout`）、GitHub 地址解析、代理拼接、压缩包地址与安全解压、同步决策逻辑（安装 / 更新 / 已最新 / 跳过 / 失败）、路径穿越防护、端口探测、无 git 时的降级、缓存命中/强制刷新、导航页渲染与转义、默认值
- `_check_bash.py` — `nav-server.sh` 的引号闭合、`if/fi` `case/esac` `do/done` 配对、here-doc 终止符、内部函数调用是否有定义
- `_check_docs.py` — 文档里提到的每个文件、命令行参数、shell 子命令、HTTP 接口、配置字段是否真实存在；交叉链接是否有效
- `_e2e.py`（**真启服务打请求**）— 9 个项目 `/apps/<id>/` 全部 200；静态资源 MIME 类型正确；挂载点缺斜杠自动 302；不存在的项目/文件/路径返回 404；4 种路径穿越姿势全部被拒；更新检测接口；拉取接口与进度接口；配置热重载；24 个并发请求全通过

**尚未验证：**

- `nav-server.sh` 的**实际执行**——本机没有可用的 bash（WSL 未安装发行版），只做了结构与函数配对的静态检查。请在有 bash 的机器上跑一次 `./nav-server.sh doctor` 确认
- **真实的 `git clone` / `git pull`、真实的 GitHub 压缩包下载**——本机既没装 git、`hosts` 又把 GitHub 全系域名指到了 `127.0.0.1`。同步逻辑是靠打桩（stub 掉 git 调用与下载函数）验证的，首次在真机上跑请留意 `--sync` 的输出

---

## 常见问题

**端口被占用？**
服务会直接报「端口 1938 无法使用」并告诉你怎么查。改 `config.json` 的 `port` 或 `--port` 临时换一个。

**启动后浏览器打不开？**
先看 `/api/health` 通不通：`curl http://127.0.0.1:1938/api/health`。如果本机能开、手机打不开，检查防火墙有没有放行该端口。

**卡片点了 503？**
项目目录不存在。点卡片上的「⇩ 部署」让服务自己拉下来，或者改 `config.json` 里那条的 `path`。

**卡片点了 404？**
目录在，但里面没有 `index.html`。

**部署完一打开，项目全是 `[×] 目录不存在`？**
这就是「项目还没下载」的状态，属于预期——nav-server 仓库里只有它自己，那 9 个项目是各自独立的仓库。
让它自己拉一遍即可：

```bash
python nav-server.py --sync      # 或导航页点「⇩ 部署 / 拉取全部」
```

默认 `autoSync` 是 `always`，服务启动时本来就会在后台拉，看看导航页的进度提示或者日志里
`[nav-server] 同步 xxx [已部署] …` 那几行。要是全失败，检查网络 / `proxy`，或者看下面这条。

**拉取一直失败？**
每张卡片会写明原因（`git clone 失败：…` / `下载失败：…`）。常见三种：

- GitHub 连不上 → 装好 git 并配代理，或者给 `config.json` 加镜像前缀 `{ "proxy": "https://ghproxy.net" }`
- 没有 git → 不用管，会自动回退到压缩包下载；不想用就设 `"zipFallback": false`
- `git pull` 报「无法快进合并」→ 本地有分叉，先自己处理，或把 `syncMode` 改成 `"hard"`（会丢弃本地改动）

**检测更新一直失败？**
看卡片上的具体原因。多半是没装 git 或 GitHub 被墙，按「更新检测怎么工作」那一节配镜像或代理。

**不想让它每次启动都拉代码？**
`"autoSync": false`，或者启动时加 `--no-auto-sync`。

**想让它开机自启？**
`./nav-server.sh install-service` 会生成 systemd 用户服务，按输出的三行命令启用即可。

---

## 文件结构

```
nav-server/
├── nav-server.py          服务主体（Python 标准库）
├── nav-server.sh          Linux/macOS 进程管理脚本
├── config.json            配置（端口、项目列表、自动拉取）
├── config.example.json    带注释说明的配置样例
├── DEPLOY-LINUX.md        Linux 部署实操手册（从 clone 到启动）
├── README.md              本文件
├── LICENSE                MIT
├── .gitignore
├── _selftest.py           逻辑自测
├── _e2e.py                端到端实测（真启服务打请求）
├── _check_bash.py         shell 结构检查器
└── _check_docs.py         文档一致性检查
```

---

## License

[MIT](LICENSE) © 2026 苏小晗
