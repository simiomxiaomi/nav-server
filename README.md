# nav-server · 工作区导航服务

![License](https://img.shields.io/badge/License-MIT-00e59b)
![Python](https://img.shields.io/badge/Python-3.7%2B-38bdf8)
![Dependencies](https://img.shields.io/badge/dependencies-0-8fa2b5)

> 把任意目录下的所有纯前端项目托管到**一个** HTTP 服务上，
> 配一个导航页，点一下就能打开任意项目。默认端口 **1938**，改配置文件即可换。

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

### 方式一：直接用 Python 启动（最省事）

```bash
cd nav-server
python nav-server.py
```

打开 <http://127.0.0.1:1938/> 即可。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-c, --config <路径>` | 指定配置文件（默认同目录 `config.json`） |
| `-p, --port <端口>` | **临时**覆盖端口，不改配置文件 |
| `--host <地址>` | 临时覆盖监听地址 |
| `--list` | 只列出项目就绪情况，不启动服务 |
| `-q, --quiet` | 不打印每个请求的访问日志 |

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
  "branch": "main",             // 对比哪个分支
  "proxy": "",                  // 可选：GitHub API 镜像前缀，见下文
  "openOnStart": false,         // 启动后自动打开浏览器
  "timeout": 6,                 // git / 网络检测超时秒数
  "projects": [
    {
      "id": "diff-viewer",                          // 访问路径用，只能字母数字点下划线连字符
      "name": "Diff Viewer",                        // 卡片标题
      "icon": "±",                                  // 卡片图标（emoji 或文字都行）
      "path": "diff-viewer",                        // 相对 root 的目录名
      "desc": "文本对比器 · LCS 逐行差异",            // 一句话描述
      "tag": "Tool",                                // 分类标签，仅用于展示
      "repo": "https://github.com/…/diff-viewer"    // 仓库地址，检测更新时用
    }
  ]
}
```

**加一个新项目**：往 `projects` 里塞一条、重启，导航页就有了（统计数字、搜索范围也会自动跟上）。

**配置写错了**：服务会明确告诉你是第几行第几列的问题，不会静默失败。

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
| POST | `/api/reload-vault` | 重新读取 `config.json` |

---

## 安全说明

- 静态文件服务做了**路径穿越防护**：`../`、URL 编码的 `..%2f` 都会被拦掉，不会读到项目目录之外
- 项目 `id` 只允许 `[A-Za-z0-9._-]`，从配置层就堵住花样路径
- 导航页里的项目名 / 描述做了 HTML 转义，`config.json` 里塞 `<script>` 也不会被执行
- 默认监听 `0.0.0.0`，**局域网内可访问**。只想本机用就把 `host` 改成 `127.0.0.1`
- 服务本身不写任何数据，除了日志文件

---

## 验证记录

```bash
python _selftest.py        # 逻辑自测：53 项
python _check_bash.py nav-server.sh   # shell 结构检查
```

**已通过：**

- `_selftest.py`（53 项）— 配置解析与各类非法配置的拒绝、GitHub 地址解析、代理拼接、路径穿越防护、端口探测、无 git 时的降级、缓存命中/强制刷新、导航页渲染与转义、默认值
- `_check_bash.py` — `nav-server.sh` 的引号闭合、`if/fi` `case/esac` `do/done` 配对、here-doc 终止符、内部函数调用是否有定义（21 个函数全部成对）
- **端到端实测**（真启服务打请求）：9 个项目 `/apps/<id>/` 全部 200；`style.css` / `app.js` / `README.md` 的 MIME 类型正确；不存在的项目 / 文件 / 路径返回 404；三种路径穿越姿势全部被拒（403/404，未泄露配置）

**尚未验证：**

- `nav-server.sh` 的**实际执行**——本机没有可用的 bash（WSL 未安装发行版），只做了结构与函数配对的静态检查。请在有 bash 的机器上跑一次 `./nav-server.sh doctor` 确认
- 真实的「检测到远端有新版本」流程——需要 git 能访问 GitHub，本机 hosts 屏蔽了它

---

## 常见问题

**端口被占用？**
服务会直接报「端口 1938 无法使用」并告诉你怎么查。改 `config.json` 的 `port` 或 `--port` 临时换一个。

**启动后浏览器打不开？**
先看 `/api/health` 通不通：`curl http://127.0.0.1:1938/api/health`。如果本机能开、手机打不开，检查防火墙有没有放行该端口。

**卡片点了 503？**
项目目录不存在，按提示检查 `config.json` 里那条的 `path`。

**卡片点了 404？**
目录在，但里面没有 `index.html`。

**检测更新一直失败？**
看卡片上的具体原因。多半是没装 git 或 GitHub 被墙，按「更新检测怎么工作」那一节配镜像或代理。

**想让它开机自启？**
`./nav-server.sh install-service` 会生成 systemd 用户服务，按输出的三行命令启用即可。

---

## 文件结构

```
nav-server/
├── nav-server.py          服务主体（Python 标准库）
├── nav-server.sh          Linux/macOS 进程管理脚本
├── config.json            配置（端口、项目列表）
├── config.example.json    带注释说明的配置样例
├── README.md              本文件
├── LICENSE                MIT
├── .gitignore
├── _selftest.py           逻辑自测（53 项）
└── _check_bash.py         shell 结构检查器
```

---

## License

[MIT](LICENSE) © 2026 苏小晗
