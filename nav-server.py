#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作区导航服务 · nav-server
=====================================================================
把一个目录下的所有纯前端项目托管到一个 HTTP 服务上，并提供导航页。

用法：
    python nav-server.py                     # 用同目录的 config.json 启动
    python nav-server.py --config other.json # 指定配置文件
    python nav-server.py --port 8080         # 临时覆盖端口（不改配置）
    python nav-server.py --list              # 只列出项目与状态，不启动服务

特性：
  · 零第三方依赖，只用 Python 标准库
  · 端口 / 项目列表 / 代理全部由 config.json 控制
  · 每个项目挂在 /apps/<id>/ 下，导航页一键跳转
  · 附「检测更新」：对比本地与远端仓库，无需拉取代码

作者：苏小晗 · MIT License
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate, parsedate_to_datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows 控制台默认可能是 GBK，直接输出中文/符号会抛 UnicodeEncodeError。
# 统一把标准输出切到 UTF-8，避免本地启动时因为一行日志而崩掉。
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"

MIME_EXTRA = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".mjs": "text/javascript; charset=utf-8",
}

# 检测结果缓存：{ project_id: {"at": 时间戳, "data": {...}} }
_UPDATE_CACHE = {}
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path):
    """读取配置并补齐默认值，返回 (config, 错误信息)"""
    p = Path(path)
    if not p.is_file():
        return None, "找不到配置文件：%s" % p
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, "配置文件不是合法 JSON：第 %d 行第 %d 列 — %s" % (e.lineno, e.colno, e.msg)
    except OSError as e:
        return None, "读取配置失败：%s" % e

    if not isinstance(raw, dict):
        return None, "配置文件的顶层必须是一个对象"

    cfg = {
        "port": 1938,
        "host": "0.0.0.0",
        "title": "工作区导航",
        "root": "..",
        "cacheTTL": 300,
        "remote": "origin",
        "branch": "main",
        "proxy": "",
        "openOnStart": False,
        "timeout": 6,
        "projects": [],
    }

    for key in cfg:
        if key in raw and raw[key] is not None:
            cfg[key] = raw[key]

    # 类型与范围校验
    try:
        cfg["port"] = int(cfg["port"])
    except (TypeError, ValueError):
        return None, "port 必须是数字"
    if not (1 <= cfg["port"] <= 65535):
        return None, "port 必须在 1–65535 之间"

    try:
        cfg["timeout"] = max(1, int(cfg["timeout"]))
        cfg["cacheTTL"] = max(0, int(cfg["cacheTTL"]))
    except (TypeError, ValueError):
        return None, "timeout / cacheTTL 必须是数字"

    if not isinstance(cfg["projects"], list):
        return None, "projects 必须是数组"

    # 项目根目录（相对配置文件所在目录解析）
    root_raw = str(cfg["root"]).strip() or "."
    root = Path(root_raw)
    if not root.is_absolute():
        root = (p.parent / root).resolve()
    cfg["rootPath"] = root

    # 归一化项目条目
    seen = set()
    items = []
    for i, entry in enumerate(cfg["projects"]):
        if not isinstance(entry, dict):
            return None, "projects[%d] 必须是对象" % i
        pid = str(entry.get("id") or entry.get("path") or "").strip()
        if not pid:
            return None, "projects[%d] 缺少 id" % i
        if not re.fullmatch(r"[A-Za-z0-9._-]+", pid):
            return None, "projects[%d] 的 id「%s」只能包含字母、数字、点、下划线、连字符" % (i, pid)
        if pid in seen:
            return None, "项目 id 重复：%s" % pid
        seen.add(pid)

        rel = str(entry.get("path") or pid).strip()
        target = Path(rel)
        if not target.is_absolute():
            target = (root / rel).resolve()

        items.append({
            "id": pid,
            "name": str(entry.get("name") or pid),
            "icon": str(entry.get("icon") or "▣"),
            "desc": str(entry.get("desc") or ""),
            "tag": str(entry.get("tag") or "App"),
            "repo": str(entry.get("repo") or ""),
            "path": rel,
            "dir": target,
            "exists": target.is_dir(),
            "entry": (target / "index.html").is_file(),
            "files": count_files(target) if target.is_dir() else 0,
        })
    cfg["projects"] = items
    cfg["configPath"] = str(p.resolve())
    return cfg, None


def count_files(directory):
    """统计项目里的文件数量（跳过隐藏文件与常见开发目录）"""
    total = 0
    try:
        for item in os.scandir(directory):
            if item.name.startswith(".") or item.name in ("node_modules", "__pycache__"):
                continue
            if item.is_file():
                total += 1
            elif item.is_dir():
                total += count_files(item.path)
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# 更新检测
# ---------------------------------------------------------------------------
def run_git(args, cwd, timeout=8):
    """执行 git 命令，返回 (成功, 输出或错误信息)"""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False, "未安装 git（命令不可用）"
    except subprocess.TimeoutExpired:
        return False, "git 命令超时（%ss）" % timeout
    except OSError as e:
        return False, "git 启动失败：%s" % e

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, (msg[0] if msg else "git 返回码 %d" % proc.returncode)
    return True, proc.stdout.strip()


def http_get_json(url, timeout=6):
    """抓取一个 JSON 接口（带 UA，GitHub API 要求）"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "nav-server/%s" % VERSION,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def normalize_github_repo(url):
    """把各种 GitHub 地址整理成 owner/repo"""
    if not url:
        return ""
    m = re.search(r"github\.com[/:]([^/]+)/([^/#?]+)", url)
    if not m:
        return ""
    return "%s/%s" % (m.group(1), m.group(2).replace(".git", ""))


def check_single_project(cfg, project, force=False):
    """检测单个项目相对远端仓库的状态"""
    pid = project["id"]
    ttl = int(cfg.get("cacheTTL") or 0)

    if not force:
        with _CACHE_LOCK:
            cached = _UPDATE_CACHE.get(pid)
            if cached and ttl > 0 and (time.time() - cached["at"]) < ttl:
                data = dict(cached["data"])
                data["cached"] = True
                return data

    result = {
        "id": pid,
        "name": project["name"],
        "repo": project["repo"],
        "state": "unknown",     # up-to-date | behind | dirty | no-git | error
        "message": "",
        "branch": "",
        "localCommit": "",
        "remoteCommit": "",
        "behind": 0,
        "ahead": 0,
        "dirtyFiles": 0,
        "lastCommit": "",
        "lastCommitDate": "",
        "method": "",
        "cached": False,
        "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not project["exists"]:
        result["state"] = "error"
        result["message"] = "本地目录不存在"
        return result

    # 尝试通过 git 判断
    ok, out = run_git(["rev-parse", "--is-inside-work-tree"], project["dir"], cfg["timeout"])
    if not ok:
        result["state"] = "no-git"
        result["message"] = "本地不是 git 仓库（%s）" % out
        return finish(result, cfg, pid)

    result["method"] = "git"

    ok, branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], project["dir"], cfg["timeout"])
    result["branch"] = branch if ok else cfg["branch"]

    ok, head = run_git(["rev-parse", "--short", "HEAD"], project["dir"], cfg["timeout"])
    result["localCommit"] = head if ok else ""

    ok, subject = run_git(["log", "-1", "--pretty=%s"], project["dir"], cfg["timeout"])
    result["lastCommit"] = subject if ok else ""

    ok, date = run_git(["log", "-1", "--pretty=%ad", "--date=format:%Y-%m-%d %H:%M"], project["dir"], cfg["timeout"])
    result["lastCommitDate"] = date if ok else ""

    # 工作区是否有未提交改动
    ok, status = run_git(["status", "--porcelain"], project["dir"], cfg["timeout"])
    if ok:
        result["dirtyFiles"] = len([ln for ln in status.splitlines() if ln.strip()])

    # 对比远端引用（只读，不拉代码）
    remote = cfg.get("remote") or "origin"
    ls_ok, ls_out = run_git(
        ["ls-remote", "--heads", remote, result["branch"] or cfg["branch"]],
        project["dir"],
        cfg["timeout"],
    )

    if ls_ok and ls_out:
        result["remoteCommit"] = ls_out.split()[0][:7]
        if result["remoteCommit"] and result["localCommit"]:
            if result["remoteCommit"] == result["localCommit"]:
                result["state"] = "dirty" if result["dirtyFiles"] else "up-to-date"
                result["message"] = (
                    "有 %d 个未提交的本地改动" % result["dirtyFiles"]
                    if result["dirtyFiles"] else "已是最新版本"
                )
            else:
                # 统计领先 / 落后多少个提交
                cnt_ok, cnt_out = run_git(
                    ["rev-list", "--left-right", "--count", "HEAD...%s/%s" % (remote, result["branch"])],
                    project["dir"],
                    cfg["timeout"],
                )
                if cnt_ok and cnt_out:
                    parts = cnt_out.split()
                    if len(parts) >= 2:
                        result["ahead"] = int(parts[0])
                        result["behind"] = int(parts[1])
                result["state"] = "behind" if result["behind"] else "dirty"
                if result["behind"]:
                    result["message"] = "远端有 %d 个新提交" % result["behind"]
                else:
                    result["message"] = "本地领先远端 %d 个提交" % result["ahead"]
        return finish(result, cfg, pid)

    # git 不可用 / 网络不通 → 回退到 GitHub API
    api_msg = ls_out if not ls_ok else "无法读取远端分支"
    repo = normalize_github_repo(result["repo"])
    if not repo:
        result["state"] = "error"
        result["message"] = "检测失败：%s" % api_msg
        return finish(result, cfg, pid)

    api_url = "https://api.github.com/repos/%s/commits/%s" % (
        repo, urllib.parse.quote(result["branch"] or cfg["branch"]),
    )
    proxy = str(cfg.get("proxy") or "").strip()
    if proxy:
        api_url = proxy.rstrip("/") + "/" + api_url

    try:
        info = http_get_json(api_url, cfg["timeout"])
        result["method"] = "api" + ("+proxy" if proxy else "")
        result["remoteCommit"] = str(info.get("sha", ""))[:7]
        commit = info.get("commit", {}) or {}
        result["lastCommit"] = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
        result["lastCommitDate"] = (commit.get("author", {}) or {}).get("date", "").replace("T", " ")[:16]
        if result["remoteCommit"] and result["remoteCommit"] == result["localCommit"]:
            result["state"] = "dirty" if result["dirtyFiles"] else "up-to-date"
            result["message"] = "已是最新版本" if not result["dirtyFiles"] else "有 %d 个未提交的本地改动" % result["dirtyFiles"]
        else:
            result["state"] = "behind"
            result["message"] = "远端已更新（%s）" % result["remoteCommit"]
    except urllib.error.HTTPError as e:
        result["state"] = "error"
        result["message"] = "GitHub API %s：%s" % (e.code, api_msg)
    except urllib.error.URLError as e:
        result["state"] = "error"
        result["message"] = "网络不可达（%s）。可在 config.json 里配置 proxy 镜像" % e.reason
    except Exception as e:                                   # noqa: BLE001
        result["state"] = "error"
        result["message"] = "检测失败：%s" % e

    return finish(result, cfg, pid)


def finish(result, cfg, pid):
    with _CACHE_LOCK:
        _UPDATE_CACHE[pid] = {"at": time.time(), "data": dict(result)}
    return result


# ---------------------------------------------------------------------------
# 导航页
# ---------------------------------------------------------------------------
NAV_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<meta name="color-scheme" content="dark">
<style>
:root{
  --bg:#080b10; --bg-soft:#0c1219; --panel:#0f161f; --panel-2:#121b26;
  --border:#1d2937; --border-soft:#16202b;
  --text:#c8d6e3; --text-dim:#8fa2b5; --muted:#63768a;
  --accent:#00e59b; --accent-2:#38bdf8; --accent-glow:rgba(0,229,155,.35);
  --warn:#fbbf24; --err:#fb7185;
  --mono:"JetBrains Mono","Cascadia Code","SF Mono",Consolas,Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
  --radius:12px; --maxw:1180px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.7;-webkit-font-smoothing:antialiased}
.bg-grid{position:fixed;inset:0;z-index:-2;
  background-image:linear-gradient(rgba(29,41,55,.35) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(29,41,55,.35) 1px,transparent 1px);
  background-size:52px 52px;
  mask-image:radial-gradient(ellipse 80% 60% at 50% 0%,#000 20%,transparent 85%);
  -webkit-mask-image:radial-gradient(ellipse 80% 60% at 50% 0%,#000 20%,transparent 85%)}
.bg-glow{position:fixed;z-index:-1;width:620px;height:620px;border-radius:50%;filter:blur(130px);opacity:.15;pointer-events:none}
.bg-glow.a{background:var(--accent);top:-260px;left:-140px}
.bg-glow.b{background:var(--accent-2);top:40%;right:-280px;opacity:.11}

header{position:sticky;top:0;z-index:50;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  background:rgba(8,11,16,.78);border-bottom:1px solid var(--border-soft)}
.nav{max-width:var(--maxw);margin:0 auto;padding:15px 24px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.logo{font-family:var(--mono);font-size:15px;font-weight:700;color:var(--accent);text-decoration:none;display:flex;align-items:center;gap:2px}
.cursor-block{display:inline-block;width:8px;height:16px;background:var(--accent);animation:blink 1.1s steps(1) infinite}
@keyframes blink{0%,50%{opacity:1}51%,100%{opacity:0}}
.nav-right{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-family:var(--mono);font-size:12px;color:var(--muted);background:var(--panel-2);
  border:1px solid var(--border);border-radius:99px;padding:5px 12px}
.pill b{color:var(--accent);font-weight:600}

.wrap{max-width:var(--maxw);margin:0 auto;padding:0 24px}
.page-head{padding:56px 0 26px}
.sec-tag{font-family:var(--mono);font-size:13px;color:var(--accent);letter-spacing:1px;display:block;margin-bottom:10px}
.page-title{font-size:clamp(28px,5vw,38px);font-weight:800;letter-spacing:-1.2px;color:#eaf2f9;margin-bottom:12px}
.page-title::after{content:"";display:block;width:56px;height:3px;margin-top:14px;
  background:linear-gradient(90deg,var(--accent),transparent);border-radius:2px}
.page-desc{color:var(--text-dim);font-size:15px;max-width:68ch;margin-top:16px}

.stats{display:flex;flex-wrap:wrap;gap:12px;padding:22px 0 6px}
.stat{flex:1 1 140px;background:linear-gradient(180deg,var(--panel),var(--bg-soft));
  border:1px solid var(--border);border-radius:var(--radius);padding:15px 18px}
.stat b{display:block;font-family:var(--mono);font-size:24px;color:var(--accent);line-height:1.2}
.stat span{font-size:12.5px;color:var(--muted)}

.toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;padding:22px 0 16px}
.search{position:relative;flex:1 1 280px;max-width:400px;display:flex;align-items:center}
.search-ico{position:absolute;left:14px;font-size:15px;color:var(--muted);pointer-events:none}
.search-input{width:100%;font-family:var(--mono);font-size:13.5px;color:var(--text);
  background:linear-gradient(180deg,var(--panel),var(--bg-soft));border:1px solid var(--border);
  border-radius:99px;padding:10px 16px 10px 38px;appearance:none;-webkit-appearance:none;outline:none}
.search-input::placeholder{color:var(--muted)}
.search-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,229,155,.12)}
.actions{display:flex;gap:9px;flex-wrap:wrap}
.btn{font-family:var(--mono);font-size:13px;cursor:pointer;color:var(--text-dim);
  background:var(--panel-2);border:1px solid var(--border);border-radius:9px;padding:9px 16px;
  display:inline-flex;align-items:center;gap:7px;transition:.18s;text-decoration:none}
.btn:hover{color:var(--accent);border-color:var(--accent);transform:translateY(-2px)}
.btn.primary{background:var(--accent);color:#04120c;border-color:transparent;font-weight:700}
.btn.primary:hover{color:#04120c;box-shadow:0 8px 24px var(--accent-glow)}
.btn[disabled]{opacity:.5;cursor:not-allowed;transform:none}
.btn .spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.hint{font-family:var(--mono);font-size:12.5px;color:var(--muted);padding-bottom:14px;min-height:20px}
.hint b{color:var(--accent)}

.list{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding-bottom:48px}
.card{background:linear-gradient(180deg,var(--panel),var(--bg-soft));border:1px solid var(--border);
  border-radius:var(--radius);padding:20px;position:relative;overflow:hidden;display:flex;flex-direction:column;gap:12px;
  transition:transform .2s,border-color .2s,box-shadow .25s}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:2px;
  background:linear-gradient(90deg,var(--accent),var(--accent-2));transform:scaleX(0);transform-origin:left;transition:transform .32s}
.card:hover{transform:translateY(-4px);border-color:rgba(0,229,155,.32);box-shadow:0 18px 44px rgba(0,0,0,.5)}
.card:hover::before{transform:scaleX(1)}
.card.missing{opacity:.72}

.c-top{display:flex;align-items:flex-start;gap:14px}
.c-icon{width:54px;height:54px;border-radius:14px;display:grid;place-items:center;font-size:26px;flex:none;
  background:rgba(0,229,155,.07);border:1px solid var(--border)}
.c-meta{min-width:0;flex:1}
.c-name{font-size:17.5px;font-weight:700;color:#eaf2f9;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.c-tag{font-family:var(--mono);font-size:10.5px;color:var(--accent);border:1px solid rgba(0,229,155,.3);
  background:rgba(0,229,155,.07);border-radius:99px;padding:1px 9px}
.c-desc{color:var(--text-dim);font-size:13.5px;margin-top:3px}
.c-path{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin-top:4px;word-break:break-all}

.c-status{font-family:var(--mono);font-size:12px;padding:8px 11px;border-radius:8px;
  background:rgba(0,0,0,.28);border:1px solid var(--border-soft);color:var(--muted);
  min-height:36px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.c-status.up{color:var(--accent);border-color:rgba(0,229,155,.28)}
.c-status.behind{color:var(--warn);border-color:rgba(251,191,36,.3)}
.c-status.dirty{color:var(--accent-2);border-color:rgba(56,189,248,.3)}
.c-status.error,.c-status.no-git{color:var(--err);border-color:rgba(251,113,133,.28)}
.c-status .dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
.c-status .sub{color:var(--muted);font-size:11px}

.c-foot{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:auto}
.c-foot .btn{padding:7px 13px;font-size:12.5px}
.empty{grid-column:1/-1;text-align:center;padding:56px 20px;color:var(--muted);font-family:var(--mono);font-size:14px}

footer{border-top:1px solid var(--border-soft);padding:24px;text-align:center;
  font-family:var(--mono);font-size:12.5px;color:var(--muted)}
footer a{color:var(--text-dim);text-decoration:none}
footer a:hover{color:var(--accent)}

@media (max-width:640px){
  .nav{padding:13px 16px}
  .wrap{padding:0 16px}
  .list{grid-template-columns:1fr}
  .page-head{padding:40px 0 18px}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
}
</style>
</head>
<body>
<div class="bg-grid"></div>
<div class="bg-glow a"></div>
<div class="bg-glow b"></div>

<header>
  <div class="nav">
    <a class="logo" href="/">nav@workspace:~$<span class="cursor-block"></span></a>
    <div class="nav-right">
      <span class="pill">端口 <b id="pillPort">__PORT__</b></span>
      <span class="pill">项目 <b id="pillCount">__COUNT__</b></span>
      <span class="pill" id="pillVersion">v__VERSION__</span>
    </div>
  </div>
</header>

<main class="wrap">
  <section class="page-head">
    <span class="sec-tag">// workspace</span>
    <h1 class="page-title">__TITLE__</h1>
    <p class="page-desc">
      这些项目都跑在同一个本地服务上，<strong>点一下就能打开</strong>，不用各自启服务器。
      端口和项目清单都在 <code>config.json</code> 里，改完重启即可生效。
    </p>
  </section>

  <section class="stats">
    <div class="stat"><b>__COUNT__</b><span>个项目</span></div>
    <div class="stat"><b id="statFiles">__FILES__</b><span>个源文件</span></div>
    <div class="stat"><b id="statReady">__READY__</b><span>个已就绪</span></div>
    <div class="stat"><b>0</b><span>外部依赖</span></div>
  </section>

  <div class="toolbar">
    <div class="search">
      <span class="search-ico">⌕</span>
      <input type="search" id="search" class="search-input" placeholder="搜索项目名 / 描述 / 路径…" autocomplete="off">
    </div>
    <div class="actions">
      <button class="btn" id="checkAll">⟳ 检测全部更新</button>
      <button class="btn" id="reloadCfg">↻ 重载配置</button>
      <a class="btn primary" href="/api/status" target="_blank" rel="noopener">ⓘ 服务状态</a>
    </div>
  </div>

  <p class="hint" id="hint"></p>

  <section class="list" id="list"></section>
</main>

<footer>
  <div class="wrap">
    nav-server v__VERSION__ · Python 标准库 · MIT License ·
    <span style="color:var(--accent)">共 __COUNT__ 个项目</span>
  </div>
</footer>

<script>
(function () {
  'use strict';

  var STATE_KEY = 'nav-server:update-cache';

  var listEl = document.getElementById('list');
  var hintEl = document.getElementById('hint');
  var searchEl = document.getElementById('search');
  var PROJECTS = __PROJECTS_JSON__;
  var CONFIG = __CONFIG_JSON__;
  var results = {};

  try {
    var saved = JSON.parse(localStorage.getItem(STATE_KEY) || '{}');
    if (saved && typeof saved === 'object') results = saved;
  } catch (e) { results = {}; }

  function saveResults() {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(results)); } catch (e) {}
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  var STATE_TEXT = {
    'up-to-date': '已是最新',
    'behind': '有新版本',
    'dirty': '有本地改动',
    'no-git': '未纳入 git',
    'error': '检测失败',
    'checking': '检测中…',
    'unknown': '未检测'
  };

  function statusClass(state) {
    if (state === 'up-to-date') return 'up';
    if (state === 'behind') return 'behind';
    if (state === 'dirty') return 'dirty';
    if (state === 'error') return 'error';
    if (state === 'no-git') return 'no-git';
    return '';
  }

  function buildStatus(p) {
    var box = el('div', 'c-status');
    var r = results[p.id];

    if (!p.exists) {
      box.className = 'c-status error';
      box.appendChild(el('span', 'dot'));
      box.appendChild(el('span', null, '目录不存在'));
      box.appendChild(el('span', 'sub', p.dir));
      return box;
    }

    var state = r && r.state ? r.state : 'unknown';
    box.className = 'c-status ' + statusClass(state);
    box.appendChild(el('span', 'dot'));

    if (state === 'unknown') {
      box.appendChild(el('span', null, '未检测'));
      box.appendChild(el('span', 'sub', '点「检测更新」看看'));
      return box;
    }

    var label = STATE_TEXT[state] || state;
    box.appendChild(el('span', null, label));

    if (r.message) box.appendChild(el('span', 'sub', r.message));
    if (r.behind > 0) box.appendChild(el('span', 'sub', '落后 ' + r.behind + ' 个提交'));
    if (r.localCommit) box.appendChild(el('span', 'sub', '本地 ' + r.localCommit));
    if (r.remoteCommit && r.remoteCommit !== r.localCommit) {
      box.appendChild(el('span', 'sub', '远端 ' + r.remoteCommit));
    }
    if (r.cached) box.appendChild(el('span', 'sub', '（缓存）'));
    return box;
  }

  function buildCard(p) {
    var card = el('article', 'card' + (p.exists && p.entry ? '' : ' missing'));

    var top = el('div', 'c-top');
    var icon = el('div', 'c-icon', p.icon);
    icon.setAttribute('aria-hidden', 'true');

    var meta = el('div', 'c-meta');
    var nameRow = el('div', 'c-name');
    nameRow.appendChild(document.createTextNode(p.name));
    nameRow.appendChild(el('span', 'c-tag', p.tag));
    meta.appendChild(nameRow);
    meta.appendChild(el('div', 'c-desc', p.desc));
    meta.appendChild(el('div', 'c-path', p.path + (p.files ? ' · ' + p.files + ' 个文件' : '')));

    top.appendChild(icon);
    top.appendChild(meta);
    card.appendChild(top);

    card.appendChild(buildStatus(p));

    var foot = el('div', 'c-foot');
    if (p.exists && p.entry) {
      var open = el('a', 'btn primary', '打开项目');
      open.href = p.url;
      open.appendChild(el('span', null, '↗'));
      foot.appendChild(open);
    } else {
      var missing = el('button', 'btn', '不可用');
      missing.disabled = true;
      foot.appendChild(missing);
    }

    var check = el('button', 'btn', '⟳ 检测更新');
    check.setAttribute('data-check', p.id);
    foot.appendChild(check);

    if (p.repo) {
      var repo = el('a', 'btn', '仓库');
      repo.href = p.repo;
      repo.target = '_blank';
      repo.rel = 'noopener noreferrer';
      repo.appendChild(el('span', null, '↗'));
      foot.appendChild(repo);
    }

    card.appendChild(foot);
    return card;
  }

  function render() {
    var q = (searchEl.value || '').trim().toLowerCase();
    var shown = PROJECTS.filter(function (p) {
      if (!q) return true;
      return (p.name + ' ' + p.desc + ' ' + p.path + ' ' + p.tag).toLowerCase().indexOf(q) !== -1;
    });

    listEl.innerHTML = '';
    if (!shown.length) {
      listEl.appendChild(el('p', 'empty', '// 没有匹配「' + searchEl.value.trim() + '」的项目'));
      return;
    }
    var frag = document.createDocumentFragment();
    shown.forEach(function (p) { frag.appendChild(buildCard(p)); });
    listEl.appendChild(frag);

    var ready = PROJECTS.filter(function (p) { return p.exists && p.entry; }).length;
    document.getElementById('statReady').textContent = ready;
    document.getElementById('statFiles').textContent =
      PROJECTS.reduce(function (s, p) { return s + (p.files || 0); }, 0);
  }

  function setHint(html) { hintEl.innerHTML = html; }

  /* ---------- 单个检测 ---------- */
  function checkOne(id, force) {
    results[id] = { id: id, state: 'checking', message: '正在检测…' };
    render();
    return fetch('/api/check-update?id=' + encodeURIComponent(id) + (force ? '&force=1' : ''))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        results[id] = data;
        saveResults();
        render();
        return data;
      })
      .catch(function (err) {
        results[id] = { id: id, state: 'error', message: '请求失败：' + err };
        render();
        return null;
      });
  }

  /* ---------- 全部检测（串行，避免把 git 打满） ---------- */
  function checkAll(force) {
    var btn = document.getElementById('checkAll');
    btn.disabled = true;
    var targets = PROJECTS.filter(function (p) { return p.exists; });
    var i = 0, behind = 0, errors = 0;

    setHint('正在检测 <b>' + targets.length + '</b> 个项目…');

    function next() {
      if (i >= targets.length) {
        btn.disabled = false;
        btn.innerHTML = '⟳ 检测全部更新';
        var tail = errors ? '，<b>' + errors + '</b> 个检测失败' : '';
        setHint('检测完成：<b>' + behind + '</b> 个项目有新版本' + tail);
        return;
      }
      var p = targets[i++];
      btn.innerHTML = '<span class="spin">⟳</span> ' + p.name + ' (' + i + '/' + targets.length + ')';
      checkOne(p.id, force).then(function (data) {
        if (data && data.state === 'behind') behind++;
        if (data && data.state === 'error') errors++;
        setTimeout(next, 60);
      });
    }
    next();
  }

  /* ---------- 事件 ---------- */
  listEl.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-check]');
    if (!btn) return;
    checkOne(btn.getAttribute('data-check'), true);
  });

  document.getElementById('checkAll').addEventListener('click', function () { checkAll(true); });

  document.getElementById('reloadCfg').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    fetch('/api/reload-vault', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function () { location.reload(); })
      .catch(function () { btn.disabled = false; setHint('重载失败，请检查服务是否还在运行'); });
  });

  var searchTimer = null;
  searchEl.addEventListener('input', function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(render, 110);
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === '/' && document.activeElement !== searchEl) {
      ev.preventDefault();
      searchEl.focus();
      searchEl.select();
    }
    if (ev.key === 'Escape' && document.activeElement === searchEl && searchEl.value) {
      searchEl.value = '';
      render();
    }
  });

  render();
  setHint('提示：检测更新只读取远端引用，不会改动你的代码 · 结果会缓存 ' + (CONFIG.cacheTTL || 0) + ' 秒');
})();
</script>
</body>
</html>
"""


def build_nav_page(cfg):
    """把配置注入导航页模板"""
    projects = []
    for p in cfg["projects"]:
        projects.append({
            "id": p["id"],
            "name": p["name"],
            "icon": p["icon"],
            "desc": p["desc"],
            "tag": p["tag"],
            "repo": p["repo"],
            "path": p["path"],
            "dir": str(p["dir"]),
            "exists": p["exists"],
            "entry": p["entry"],
            "files": p["files"],
            "url": "/apps/%s/" % p["id"],
        })

    html = NAV_TEMPLATE
    html = html.replace("__TITLE__", escape_html(cfg["title"]))
    html = html.replace("__PORT__", str(cfg["port"]))
    html = html.replace("__VERSION__", VERSION)
    html = html.replace("__COUNT__", str(len(projects)))
    html = html.replace("__FILES__", str(sum(p["files"] for p in projects)))
    html = html.replace("__READY__", str(sum(1 for p in projects if p["exists"] and p["entry"])))
    html = html.replace("__PROJECTS_JSON__", json.dumps(projects, ensure_ascii=False))
    html = html.replace("__CONFIG_JSON__", json.dumps({
        "port": cfg["port"],
        "cacheTTL": cfg["cacheTTL"],
        "root": str(cfg["rootPath"]),
    }, ensure_ascii=False))
    return html.encode("utf-8")


def escape_html(text):
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class NavHandler(SimpleHTTPRequestHandler):
    server_version = "nav-server/" + VERSION

    # 静音默认的逐请求日志，避免刷屏
    def log_message(self, fmt, *args):
        if self.server.quiet:
            return
        sys.stderr.write("[nav-server] %s - %s\n" % (self.address_string(), fmt % args))

    def log_error(self, fmt, *args):
        sys.stderr.write("[nav-server] 错误 - %s\n" % (fmt % args))

    # ---------------- 通用工具 ----------------
    @property
    def cfg(self):
        return self.server.cfg

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect_to(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def project_by_id(self, pid):
        for p in self.cfg["projects"]:
            if p["id"] == pid:
                return p
        return None

    def safe_path(self, base, relative):
        """把相对路径拼到 base 下，并确保没有越界"""
        base = Path(base).resolve()
        target = (base / relative.lstrip("/")).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return None
        return target

    # ---------------- 路由 ----------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        # 导航页
        if path in ("/", "/index.html", "/nav"):
            return self.send_text(build_nav_page(self.cfg), content_type="text/html; charset=utf-8")

        # 健康检查 / 状态
        if path == "/api/health":
            return self.send_json({"ok": True, "service": "nav-server", "version": VERSION, "time": time.time()})
        if path == "/api/status":
            return self.send_json(self.status_payload())

        # 检测更新
        if path == "/api/check-update":
            return self.handle_check_update(query)

        # 项目静态资源
        if path.startswith("/apps/"):
            return self.serve_project(path, query)

        return self.send_text("404 · 找不到 " + path, status=404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # 读掉请求体，避免连接复用出问题
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass

        if path == "/api/reload-vault":
            # 记住命令行覆盖过的端口 / 地址，重载配置后要重新应用
            overrides = {
                "port": getattr(self.server, "port_override", None),
                "host": getattr(self.server, "host_override", None),
            }
            cfg, err = load_config(self.cfg["configPath"])
            if err:
                return self.send_json({"ok": False, "error": err}, status=400)
            if overrides["port"]:
                cfg["port"] = overrides["port"]
            if overrides["host"]:
                cfg["host"] = overrides["host"]
            self.server.cfg = cfg
            return self.send_json({"ok": True, "port": cfg["port"],
                                   "host": cfg["host"], "projects": len(cfg["projects"])})

        return self.send_json({"ok": False, "error": "不支持的操作"}, status=404)

    def status_payload(self):
        cfg = self.cfg
        return {
            "service": "nav-server",
            "version": VERSION,
            "port": cfg["port"],
            "host": cfg["host"],
            "root": str(cfg["rootPath"]),
            "configPath": cfg["configPath"],
            "python": sys.version.split()[0],
            "uptime": round(time.time() - self.server.started_at, 1),
            "cacheTTL": cfg["cacheTTL"],
            "proxy": cfg["proxy"],
            "projects": [{
                "id": p["id"],
                "name": p["name"],
                "path": str(p["dir"]),
                "exists": p["exists"],
                "entry": p["entry"],
                "files": p["files"],
                "url": "/apps/%s/" % p["id"],
                "repo": p["repo"],
            } for p in cfg["projects"]],
        }

    def handle_check_update(self, query):
        pid = (query.get("id") or [""])[0]
        force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")

        cfg = self.cfg
        if not pid:
            if not force:
                return self.send_json({"ok": False, "error": "缺少 id 参数"}, status=400)
            results = []
            for p in cfg["projects"]:
                if p["exists"]:
                    results.append(check_single_project(cfg, p, force=True))
            return self.send_json({"ok": True, "results": results})

        project = self.project_by_id(pid)
        if not project:
            return self.send_json({"ok": False, "error": "没有这个项目：%s" % pid}, status=404)

        data = check_single_project(cfg, project, force=force)
        data["ok"] = True
        return self.send_json(data)

    def serve_project(self, path, query):
        rest = path[len("/apps/"):]
        if not rest:
            return self.redirect_to("/")

        parts = rest.split("/", 1)
        pid = parts[0]
        relative = parts[1] if len(parts) > 1 else ""
        trail = path.endswith("/")          # 原始请求是否带末尾斜杠

        project = self.project_by_id(pid)
        if not project:
            return self.send_text("404 · 没有这个项目：" + pid, status=404)
        if not project["exists"]:
            return self.send_text(
                "503 · 项目目录不存在\n\n%s\n\n请在 config.json 中检查该项目的 path。" % project["dir"],
                status=503,
            )

        # 挂载点本身（/apps/<id>）没有末尾斜杠时补一个再重定向。
        # 必须做：否则浏览器会把 /apps/<id> 当成"文件"，页面里的相对路径
        # （style.css、app.js）会解析到上一层，样式和脚本全部 404。
        # 注意 relative 在这里是空串，不能写成 `if relative and ...`（会短路跳过）。
        if relative == "" and not trail:
            return self.redirect_to("/apps/%s/" % pid)

        # 目录（或空路径）→ 补 index.html
        if trail or relative == "":
            relative = relative + "index.html"

        target = self.safe_path(project["dir"], relative)
        if target is None:
            return self.send_text("403 · 非法的路径", status=403)
        if not target.is_file():
            # 目录没写 index.html 时给个友好提示
            if relative.endswith("index.html"):
                return self.send_text(
                    "404 · 该项目下没有 index.html\n\n%s" % target, status=404,
                    content_type="text/plain; charset=utf-8",
                )
            return self.send_text("404 · %s" % relative, status=404)

        return self.send_file(target)

    def send_file(self, target):
        try:
            stat = target.stat()
        except OSError:
            return self.send_text("404", status=404)

        ext = target.suffix.lower()
        ctype = MIME_EXTRA.get(ext)
        if ctype is None:
            ctype = self.guess_type(str(target))

        # 简单的协商缓存：按修改时间判断
        last_modified = formatdate(stat.st_mtime, usegmt=True)
        ims = self.headers.get("If-Modified-Since")
        if ims:
            try:
                if parsedate_to_datetime(ims).timestamp() >= int(stat.st_mtime):
                    self.send_response(304)
                    self.end_headers()
                    return
            except (TypeError, ValueError, OverflowError):
                pass

        try:
            body = target.read_bytes()
        except OSError:
            return self.send_text("500 · 读取文件失败", status=500)

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", last_modified)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def ensure_port_available(host, port):
    """提前探测端口，给出比 traceback 友好的报错"""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((probe_host, port))
        except OSError as e:
            return False, str(e)
    return True, ""


def print_banner(cfg, extra=""):
    line = "─" * 62
    print(line)
    print("  %s  ·  nav-server v%s" % (cfg["title"], VERSION))
    print(line)
    print("  配置       %s" % cfg["configPath"])
    print("  项目根目录 %s" % cfg["rootPath"])
    print("  监听       %s:%d" % (cfg["host"], cfg["port"]))
    print("  导航页     http://127.0.0.1:%d/" % cfg["port"])
    print("  状态接口   http://127.0.0.1:%d/api/status" % cfg["port"])
    print(line)
    ready = 0
    for p in cfg["projects"]:
        if p["exists"] and p["entry"]:
            mark, ready = "✓", ready + 1
        elif p["exists"]:
            mark = "!"
        else:
            mark = "×"
        print("  [%s] %-18s → /apps/%s/" % (mark, p["id"], p["id"]))
    print(line)
    print("  就绪 %d/%d 个项目%s" % (ready, len(cfg["projects"]),
                                "（! = 缺 index.html，× = 目录不存在）" if ready < len(cfg["projects"]) else ""))
    if extra:
        print("  " + extra)
    print("  按 Ctrl+C 停止服务")
    print(line)


def list_projects(cfg):
    print("%-20s %-8s %-6s %s" % ("ID", "状态", "文件数", "目录"))
    print("-" * 78)
    for p in cfg["projects"]:
        if p["exists"] and p["entry"]:
            state = "就绪"
        elif p["exists"]:
            state = "缺入口"
        else:
            state = "缺失"
        print("%-20s %-8s %-6d %s" % (p["id"], state, p["files"], p["dir"]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="nav-server.py",
        description="工作区导航服务：托管多个纯前端项目并提供导航页",
    )
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="配置文件路径（默认同目录 config.json）")
    parser.add_argument("-p", "--port", type=int, help="临时覆盖端口（不修改配置文件）")
    parser.add_argument("--host", help="临时覆盖监听地址")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印每个请求日志")
    parser.add_argument("--list", action="store_true", help="只列出项目状态，不启动服务")
    parser.add_argument("-v", "--version", action="version", version="nav-server " + VERSION)
    args = parser.parse_args(argv)

    cfg, err = load_config(args.config)
    if err:
        print("配置错误：%s" % err, file=sys.stderr)
        print("提示：可以先把 config.json 放在脚本同目录，或用 -c 指定路径。", file=sys.stderr)
        return 2

    if args.port:
        if not (1 <= args.port <= 65535):
            print("端口必须在 1–65535 之间", file=sys.stderr)
            return 2
        cfg["port"] = args.port
    if args.host:
        cfg["host"] = args.host

    if args.list:
        list_projects(cfg)
        return 0

    ok, why = ensure_port_available(cfg["host"], cfg["port"])
    if not ok:
        print("端口 %d 无法使用：%s" % (cfg["port"], why), file=sys.stderr)
        print("要么先停掉占用它的程序，要么改 config.json 里的 port。", file=sys.stderr)
        return 3

    print_banner(cfg)

    handler = partial(NavHandler, directory=str(cfg["rootPath"]))
    try:
        httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), handler)
    except OSError as e:
        print("启动失败：%s" % e, file=sys.stderr)
        return 3

    httpd.daemon_threads = True
    httpd.cfg = cfg
    httpd.quiet = args.quiet
    httpd.started_at = time.time()
    # 记录命令行覆盖项，重载配置时保持不被覆盖回去
    httpd.port_override = cfg["port"] if args.port else None
    httpd.host_override = cfg["host"] if args.host else None

    if cfg.get("openOnStart"):
        threading.Thread(
            target=lambda: _open_browser("http://127.0.0.1:%d/" % cfg["port"]),
            daemon=True,
        ).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[nav-server] 收到停止信号，正在关闭…")
    finally:
        httpd.shutdown()
        httpd.server_close()
        print("[nav-server] 已停止。")
    return 0


def _open_browser(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:                                        # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
