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
    python nav-server.py --sync              # 拉取 / 部署一遍项目后退出
    python nav-server.py --no-auto-sync      # 本次启动不做自动拉取

特性：
  · 零第三方依赖，只用 Python 标准库
  · 端口 / 项目列表 / 代理全部由 config.json 控制
  · 每个项目挂在 /apps/<id>/ 下，导航页一键跳转
  · 「自动部署」：缺失的项目自动 git clone；已有的自动 git pull
    （没有 git 时回退到 GitHub 压缩包下载解压）
  · 附「检测更新」：只读对比本地与远端仓库，不改动任何文件

作者：苏小晗 · MIT License
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
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

# 输出重定向到日志文件时默认是块缓冲，后台跑会「看不到日志」。
# 打开行缓冲，让 banner、拉取进度这些能立刻写进 nav-server.log。
_reconfigure = getattr(getattr(sys, "stdout", None), "reconfigure", None)
if _reconfigure is not None:
    try:
        _reconfigure(line_buffering=True)
    except (ValueError, OSError):
        pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VERSION = "1.1.0"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"

# 用 zip 兜底部署时留下的标记文件：有它才说明这个目录归 nav-server 管，
# 之后才允许再次用 zip 覆盖更新（避免动到用户自己放的代码）。
ZIP_MARKER = ".nav-server-zip"

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

# 拉取 / 部署任务的共享状态（后台线程写，HTTP 线程读）
SYNC_STATE = {
    "running": False,
    "mode": "",             # auto | manual | single
    "startedAt": "",
    "finishedAt": "",
    "total": 0,
    "results": [],
    "summary": "",
    "ids": [],
}
_SYNC_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def parse_bool(value):
    """把 JSON 里的 true/false、"true"/"false"、"1"/"0" 统一成 bool。返回 (是否可解析, 值)"""
    if isinstance(value, bool):
        return True, value
    if isinstance(value, (int, float)):
        return True, bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True, True
    if text in ("0", "false", "no", "off", ""):
        return True, False
    return False, False


def normalize_auto_sync(value):
    """autoSync 的三种取值：never / missing / always；无法识别返回 None"""
    if isinstance(value, bool):
        return "always" if value else "never"
    text = str(value).strip().lower()
    if text in ("always", "all", "true", "1", "yes", "on"):
        return "always"
    if text in ("missing", "install", "bootstrap"):
        return "missing"
    if text in ("never", "off", "false", "0", "no", "none", "disable", "disabled"):
        return "never"
    return None


def load_config(path):
    """读取配置并补齐默认值，返回 (config, 错误信息)"""
    p = Path(path)
    if not p.is_file():
        return None, "找不到配置文件：%s" % p
    try:
        # utf-8-sig：Windows 记事本存的配置常带 BOM，不能因此报「不是合法 JSON」
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
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
        "autoSync": "always",
        "syncMode": "ff",
        "zipFallback": True,
        "syncTimeout": 120,
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

    try:
        cfg["syncTimeout"] = max(10, int(cfg["syncTimeout"]))
    except (TypeError, ValueError):
        return None, "syncTimeout 必须是数字（建议 60 以上）"

    cfg["autoSync"] = normalize_auto_sync(cfg["autoSync"])
    if cfg["autoSync"] is None:
        return None, "autoSync 只能是 false / \"missing\" / \"always\""

    mode = str(cfg["syncMode"] or "ff").strip().lower()
    if mode in ("ff-only", "ff_only", "pull", "safe", "fast-forward"):
        mode = "ff"
    if mode not in ("ff", "hard"):
        return None, "syncMode 只能是 \"ff\"（只允许快进）或 \"hard\"（强制对齐远端）"
    cfg["syncMode"] = mode

    ok, flag = parse_bool(cfg["zipFallback"])
    if not ok:
        return None, "zipFallback 只能是 true / false"
    cfg["zipFallback"] = flag

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

        items.append({
            "id": pid,
            "name": str(entry.get("name") or pid),
            "icon": str(entry.get("icon") or "▣"),
            "desc": str(entry.get("desc") or ""),
            "tag": str(entry.get("tag") or "App"),
            "repo": str(entry.get("repo") or ""),
            "path": rel,
            "dir": root / rel,
            "exists": False,
            "entry": False,
            "files": 0,
        })
    cfg["projects"] = items
    # 目录 / index.html / 文件数统一在这里算，拉取之后复用同一个函数刷新
    refresh_project_state(cfg)
    cfg["configPath"] = str(p.resolve())
    return cfg, None


def refresh_project_state(cfg):
    """重新扫描每个项目的目录，刷新 dir / exists / entry / files（同步完成或重载配置后调用）"""
    root = Path(cfg["rootPath"])
    for p in cfg["projects"]:
        target = Path(p["path"])
        if not target.is_absolute():
            target = (root / p["path"]).resolve()
        p["dir"] = target
        p["exists"] = target.is_dir()
        p["entry"] = (target / "index.html").is_file()
        p["files"] = count_files(target) if p["exists"] else 0
    return cfg


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
    env = dict(os.environ)
    # 服务是无人值守的：绝不能让 git 卡在「输入用户名/密码」上
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS") or "true"
    env["SSH_ASKPASS"] = env.get("SSH_ASKPASS") or "true"
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=env,
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
# 拉取 / 部署：把 config.json 里的 repo 真正落到磁盘
# ---------------------------------------------------------------------------
SYNC_STATUS_TEXT = {
    "installed": "已部署",
    "updated": "已更新",
    "current": "已最新",
    "skipped": "已跳过",
    "failed": "失败",
    "running": "进行中",
}


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def zip_url_for(cfg, project):
    """GitHub 压缩包地址（codeload），返回时不带 proxy 前缀"""
    repo = normalize_github_repo(project.get("repo") or "")
    if not repo:
        return ""
    branch = str(cfg.get("branch") or "main")
    return "https://codeload.github.com/%s/zip/refs/heads/%s" % (
        repo, urllib.parse.quote(branch),
    )


def with_proxy(cfg, url):
    """按配置给 URL 加上镜像前缀"""
    proxy = str(cfg.get("proxy") or "").strip()
    if proxy and url:
        return proxy.rstrip("/") + "/" + url
    return url


def download_file(cfg, url, target):
    """下载 url 到 target，返回 (成功, 说明)"""
    req = urllib.request.Request(url, headers={"User-Agent": "nav-server/%s" % VERSION})
    try:
        with urllib.request.urlopen(req, timeout=int(cfg.get("syncTimeout") or 120)) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        return False, "下载失败：HTTP %s" % e.code
    except urllib.error.URLError as e:
        return False, "下载失败：%s" % e.reason
    except OSError as e:
        return False, "下载失败：%s" % e
    if not data:
        return False, "下载失败：收到空响应"
    try:
        target.write_bytes(data)
    except OSError as e:
        return False, "写入临时文件失败：%s" % e
    return True, "已下载 %.0f KB" % (len(data) / 1024.0)


def safe_extract_zip(zip_path, dest):
    """把压缩包解压到 dest，挡掉绝对路径与 ../ 穿越条目，返回解压出的文件数"""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                continue
            out = (dest / name).resolve()
            try:
                out.relative_to(dest)
            except ValueError:
                continue
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def zip_root_of(directory):
    """GitHub 的 zip 里只有一个 repo-branch/ 顶层目录，返回它（结构异常时返回原目录）"""
    directory = Path(directory)
    try:
        items = list(directory.iterdir())
    except OSError:
        return directory
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return directory


def copy_tree_into(src, dst):
    """把 src 里的内容合并复制到 dst（不用 copytree 的 dirs_exist_ok，Python 3.7 也能跑）"""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for item in Path(src).iterdir():
        target = dst / item.name
        if item.is_dir():
            copy_tree_into(item, target)
        else:
            shutil.copy2(item, target)


def make_work_dir(near):
    """给下载 / 解压找个临时目录：优先用目标目录旁边（同盘、权限一致），
    不行再退回系统临时目录"""
    try:
        cand = Path(near).parent / (".nav-server-tmp-%d" % os.getpid())
        cand.mkdir(parents=True, exist_ok=True)
        return cand
    except OSError:
        return Path(tempfile.mkdtemp(prefix="nav-zip-"))


def install_from_zip(cfg, project):
    """用 GitHub 压缩包把项目部署到目标目录（没有 git，或 clone 失败时的兜底）"""
    target = Path(project["dir"])
    url = zip_url_for(cfg, project)
    if not url:
        return False, "仓库地址解析不出 owner/repo，无法用压缩包兜底"

    tmp = make_work_dir(target)
    try:
        zpath = tmp / "repo.zip"
        ok, msg = download_file(cfg, with_proxy(cfg, url), zpath)
        if not ok:
            return False, msg

        raw = tmp / "raw"
        try:
            count = safe_extract_zip(zpath, raw)
        except zipfile.BadZipFile:
            return False, "下载到的不是有效压缩包（网络被劫持时可试试配置 proxy 镜像）"
        except OSError as e:
            return False, "解压失败：%s" % e
        if not count:
            return False, "压缩包里没有文件"

        src = zip_root_of(raw)
        try:
            copy_tree_into(src, target)
            # 打上标记：这个目录由 nav-server 用压缩包部署，之后允许它继续覆盖更新
            (target / ZIP_MARKER).write_text(
                "repo=%s\nbranch=%s\nat=%s\n" % (
                    project.get("repo") or "", cfg.get("branch") or "", now_text(),
                ),
                encoding="utf-8",
            )
        except OSError as e:
            return False, "写入项目目录失败：%s" % e
        return True, "%s，解压 %d 个文件" % (msg, count)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sync_project(cfg, project):
    """对单个项目执行一次同步：缺失就安装，已有就更新。返回结果字典。"""
    pid = project["id"]
    target = Path(project["dir"])
    repo = str(project.get("repo") or "").strip()
    branch = str(cfg.get("branch") or "main")
    timeout = int(cfg.get("syncTimeout") or 120)
    mode = cfg.get("syncMode") or "ff"
    zip_ok = bool(cfg.get("zipFallback"))

    result = {
        "id": pid,
        "name": project.get("name") or pid,
        "status": "failed",          # installed | updated | current | skipped | failed
        "message": "",
        "at": now_text(),
    }

    def done(status, message):
        result["status"] = status
        result["message"] = message
        return result

    if not repo:
        return done("skipped", "配置里没有 repo 地址，跳过")

    is_git = (target / ".git").is_dir()

    # ---- 情况一：目录存在且是 git 仓库 → 拉取更新 ----
    if target.is_dir() and is_git:
        ok, before = run_git(["rev-parse", "HEAD"], target, timeout)
        before = before if ok else ""

        if mode == "hard":
            ok, out = run_git(["fetch", "--prune", "origin", branch], target, timeout)
            if not ok:
                return done("failed", "fetch 失败：%s" % out)
            ok, out = run_git(["reset", "--hard", "origin/%s" % branch], target, timeout)
            if not ok:
                return done("failed", "reset --hard 失败：%s" % out)
        else:
            ok, out = run_git(["pull", "--ff-only"], target, timeout)
            if not ok:
                # 当前分支没配上游时 pull 会直接失败，退回 fetch + merge --ff-only
                f_ok, f_out = run_git(["fetch", "origin", branch], target, timeout)
                if not f_ok:
                    return done("failed", "pull 失败：%s；fetch 也失败：%s" % (out, f_out))
                m_ok, m_out = run_git(["merge", "--ff-only", "origin/%s" % branch], target, timeout)
                if not m_ok:
                    return done(
                        "failed",
                        "无法快进合并：%s（本地有分叉时可把 syncMode 改成 \"hard\"）" % m_out,
                    )

        ok, after = run_git(["rev-parse", "--short", "HEAD"], target, timeout)
        after = after if ok else ""
        if before and after and before.startswith(after):
            return done("current", "已是最新（%s）" % after)
        return done("updated", "已更新到 %s" % (after or "最新版本"))

    # ---- 情况二：目录存在但不是 git 仓库 ----
    if target.is_dir() and not is_git:
        marker = target / ZIP_MARKER
        try:
            empty = not any(target.iterdir())
        except OSError:
            empty = False

        if marker.is_file():
            if not zip_ok:
                return done("skipped", "这个目录是压缩包部署的，但 zipFallback 已关闭，跳过")
            ok, msg = install_from_zip(cfg, project)
            return done("updated", msg) if ok else done("failed", msg)

        if not empty:
            return done("skipped", "目录已存在且不是 git 仓库，跳过（不动你自己的文件）")

    # ---- 情况三：目录不存在（或是个空目录）→ 克隆 / 下载 ----
    try:
        empty_dir = target.is_dir() and not any(target.iterdir())
    except OSError:
        empty_dir = False
    if target.exists() and not empty_dir:
        # 既不是 git 仓库、又不是空目录 → 不动它
        return done("skipped", "目标路径已存在且不是空目录，跳过")

    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return done("failed", "无法创建目录 %s：%s" % (parent, e))

    clone_err = ""
    ok, out = run_git(["clone", "--single-branch", "--branch", branch, repo, str(target)], parent, timeout)
    if ok:
        return done("installed", "已克隆 %s 分支" % branch)

    clone_err = out
    # 分支名可能不叫 config 里写的那个 → 用仓库默认分支再试一次
    ok2, out2 = run_git(["clone", "--single-branch", repo, str(target)], parent, timeout)
    if ok2:
        return done("installed", "已克隆（仓库默认分支，配置里的 branch=%s 可能不存在）" % branch)
    if out2 and out2 != clone_err:
        clone_err = "%s / %s" % (clone_err, out2)

    if zip_ok:
        z_ok, z_msg = install_from_zip(cfg, project)
        if z_ok:
            return done("installed", "git 克隆失败（%s），改用压缩包：%s" % (clone_err or "原因未知", z_msg))
        return done("failed", "克隆失败：%s；压缩包兜底也失败：%s" % (clone_err or "原因未知", z_msg))

    return done("failed", "克隆失败：%s（可打开 zipFallback 用压缩包部署，或配置 proxy 镜像）"
                % (clone_err or "原因未知"))


def summarize_sync(results):
    """把结果列表汇总成一句话"""
    if not results:
        return "没有需要拉取的项目"
    parts = []
    for key in ("installed", "updated", "current", "skipped", "failed"):
        n = sum(1 for r in results if r.get("status") == key)
        if n:
            parts.append("%s %d" % (SYNC_STATUS_TEXT.get(key, key), n))
    return "拉取完成：" + "，".join(parts)


def pick_sync_targets(cfg, only_ids=None):
    """挑出需要同步的项目：必须配了 repo；只有非 git 仓库才需要额外判断"""
    targets = []
    for p in cfg["projects"]:
        if only_ids and p["id"] not in only_ids:
            continue
        if not (p.get("repo") or "").strip():
            continue
        targets.append(p)
    return targets


def sync_status_payload(cfg=None):
    """给前端轮询用的同步进度"""
    with _SYNC_LOCK:
        payload = {
            "running": SYNC_STATE["running"],
            "mode": SYNC_STATE["mode"],
            "startedAt": SYNC_STATE["startedAt"],
            "finishedAt": SYNC_STATE["finishedAt"],
            "total": SYNC_STATE["total"],
            "done": len(SYNC_STATE["results"]),
            "results": [dict(r) for r in SYNC_STATE["results"]],
            "summary": SYNC_STATE["summary"],
            "ids": list(SYNC_STATE.get("ids") or []),
        }
    if cfg is not None:
        payload["projects"] = {
            p["id"]: {"exists": p["exists"], "entry": p["entry"], "files": p["files"],
                      "dir": str(p["dir"])}
            for p in cfg["projects"]
        }
    return payload


def start_sync_batch(cfg, only_ids=None, mode_label="manual"):
    """后台线程跑一次同步，返回 (是否已启动, 说明)。同一时间只允许一个任务。"""
    with _SYNC_LOCK:
        if SYNC_STATE["running"]:
            return False, "已经有一个拉取任务在进行中，请稍候"
        targets = pick_sync_targets(cfg, only_ids)
        if not targets:
            return False, "没有可拉取的项目（检查一下这些项目的 repo 字段）"
        SYNC_STATE.update({
            "running": True,
            "mode": mode_label,
            "startedAt": now_text(),
            "finishedAt": "",
            "total": len(targets),
            "results": [],
            "summary": "",
            "ids": [p["id"] for p in targets],
        })

    threading.Thread(target=_sync_worker, args=(cfg, targets), daemon=True).start()
    return True, "已开始拉取 %d 个项目" % len(targets)


def _sync_worker(cfg, targets):
    results = []
    try:
        for p in targets:
            try:
                res = sync_project(cfg, p)
            except Exception as e:                           # noqa: BLE001
                res = {"id": p["id"], "name": p.get("name") or p["id"], "status": "failed",
                       "message": "内部错误：%s" % e, "at": now_text()}
            refresh_project_state(cfg)                       # 立刻让导航页看到新状态
            with _CACHE_LOCK:
                _UPDATE_CACHE.pop(p["id"], None)             # 更新检测的缓存作废
            with _SYNC_LOCK:
                SYNC_STATE["results"].append(res)
            results.append(res)
            print("[nav-server] 同步 %-18s [%s] %s" % (
                p["id"], SYNC_STATUS_TEXT.get(res["status"], res["status"]), res["message"]))
    finally:
        summary = summarize_sync(results)
        with _SYNC_LOCK:
            SYNC_STATE["running"] = False
            SYNC_STATE["finishedAt"] = now_text()
            SYNC_STATE["summary"] = summary
        print("[nav-server] %s" % summary)


def sync_cli(cfg, only_ids=None):
    """命令行 --sync：同步一遍并打印结果，返回退出码"""
    targets = pick_sync_targets(cfg, only_ids)
    if not targets:
        print("没有可拉取的项目（检查这些项目的 repo 字段）")
        return 1

    print("开始拉取 %d 个项目…（git 不可用时会回退到压缩包下载）\n" % len(targets))
    results = []
    for p in targets:
        res = sync_project(cfg, p)
        refresh_project_state(cfg)
        with _CACHE_LOCK:
            _UPDATE_CACHE.pop(p["id"], None)
        results.append(res)
        print("  [%-6s] %-18s %s" % (
            SYNC_STATUS_TEXT.get(res["status"], res["status"]), p["id"], res["message"]))

    print("\n%s\n" % summarize_sync(results))
    list_projects(cfg)
    return 1 if any(r["status"] == "failed" for r in results) else 0


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
.c-status .spin{display:inline-block;animation:spin 1s linear infinite;color:var(--accent-2)}

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
      缺失的项目点「⇩ 部署」会自动下载安装，已有的点「⇩ 拉取更新」就更新代码。
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
      <button class="btn primary" id="syncAll">⇩ 部署 / 拉取全部</button>
      <button class="btn" id="checkAll">⟳ 检测全部更新</button>
      <button class="btn" id="reloadCfg">↻ 重载配置</button>
      <a class="btn" href="/api/status" target="_blank" rel="noopener">ⓘ 服务状态</a>
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
  var syncResults = {};              // id -> {status, message}
  var syncState = { running: false, ids: [], done: 0, total: 0 };
  var syncTimer = null;

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

  var SYNC_TEXT = {
    'installed': '已部署',
    'updated': '已更新',
    'current': '已是最新',
    'skipped': '已跳过',
    'failed': '拉取失败'
  };

  function syncClass(status) {
    if (status === 'failed') return 'error';
    if (status === 'skipped') return 'behind';
    if (status === 'updated' || status === 'installed' || status === 'current') return 'up';
    return '';
  }

  function syncPending(p) {
    return syncState.running && syncState.ids.indexOf(p.id) !== -1 && !syncResults[p.id];
  }

  function buildStatus(p) {
    var box = el('div', 'c-status');
    var s = syncResults[p.id];

    if (s || syncPending(p)) {
      var status = s ? s.status : 'running';
      box.className = 'c-status ' + (s ? syncClass(status) : '');
      box.appendChild(el('span', 'dot'));
      if (!s) {
        box.appendChild(el('span', 'spin', '⇩'));
        box.appendChild(el('span', null, '等待拉取…'));
      } else {
        box.appendChild(el('span', null, SYNC_TEXT[status] || status));
        if (s.message) box.appendChild(el('span', 'sub', s.message));
        if (!p.exists) box.appendChild(el('span', 'sub', '目录仍不存在'));
      }
      return box;
    }

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
      var sync = el('button', 'btn', p.exists ? '⇩ 拉取更新' : '⇩ 部署');
      sync.setAttribute('data-sync', p.id);
      if (syncState.running) sync.disabled = true;
      foot.appendChild(sync);

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

  /* ---------- 拉取 / 部署（服务端后台跑，前端轮询进度） ---------- */
  function absorbSync(data) {
    syncState.running = !!data.running;
    syncState.ids = data.ids || [];
    syncState.done = data.done || 0;
    syncState.total = data.total || 0;

    syncResults = {};
    (data.results || []).forEach(function (r) { syncResults[r.id] = r; });

    // 服务端已经重新扫过目录，这里同步刷新卡片上的状态
    var fresh = data.projects || {};
    PROJECTS.forEach(function (p) {
      var f = fresh[p.id];
      if (!f) return;
      if (f.exists !== p.exists || f.entry !== p.entry || f.files !== p.files) {
        delete results[p.id];                    // 目录变了，之前的检测结果作废
      }
      p.exists = f.exists;
      p.entry = f.entry;
      p.files = f.files;
      if (f.dir) p.dir = f.dir;
    });
    saveResults();
  }

  function syncProgressText() {
    return '正在拉取 / 部署项目… <b>' + syncState.done + ' / ' + syncState.total + '</b>';
  }

  function watchSync(silent) {
    clearInterval(syncTimer);
    var tick = function () {
      fetch('/api/sync-status')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          absorbSync(data);
          render();
          if (!data.running) {
            clearInterval(syncTimer);
            var btn = document.getElementById('syncAll');
            if (btn) { btn.disabled = false; btn.innerHTML = '⇩ 部署 / 拉取全部'; }
            setHint(data.summary || '拉取结束');
            return;
          }
          setHint(syncProgressText());
        })
        .catch(function () {
          clearInterval(syncTimer);
          var btn = document.getElementById('syncAll');
          if (btn) btn.disabled = false;
          setHint('拿不到拉取进度，服务还在跑吗？');
        });
    };
    if (!silent) tick();
    syncTimer = setInterval(tick, 700);
  }

  function startSync(id) {
    var all = document.getElementById('syncAll');
    if (all) {
      all.disabled = true;
      all.innerHTML = '<span class="spin">⇩</span> 拉取中…';
    }
    var url = '/api/sync' + (id ? '?id=' + encodeURIComponent(id) : '');
    fetch(url, { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) {
          if (all) { all.disabled = false; all.innerHTML = '⇩ 部署 / 拉取全部'; }
          setHint('拉取没能开始：' + (data.error || '未知原因'));
          return;
        }
        absorbSync(data.sync || {});
        render();
        setHint(syncProgressText());
        watchSync(true);
      })
      .catch(function () {
        if (all) { all.disabled = false; all.innerHTML = '⇩ 部署 / 拉取全部'; }
        setHint('请求失败，服务还在跑吗？');
      });
  }

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
    var check = ev.target.closest('[data-check]');
    if (check) {
      checkOne(check.getAttribute('data-check'), true);
      return;
    }
    var sync = ev.target.closest('[data-sync]');
    if (sync && !sync.disabled) startSync(sync.getAttribute('data-sync'));
  });

  document.getElementById('syncAll').addEventListener('click', function () { startSync(''); });

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
  setHint('提示：项目缺失时点「⇩ 部署」会自动克隆，已有项目点「⇩ 拉取更新」就更新代码 · 检测更新只读远端引用，不动你的文件');

  /* 服务启动时可能正在自动拉取项目，先问一次进度 */
  fetch('/api/sync-status')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      absorbSync(data);
      render();
      if (data.running) {
        var btn = document.getElementById('syncAll');
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin">⇩</span> 拉取中…'; }
        setHint('服务启动时开启了自动拉取，正在同步项目…');
        watchSync(true);
      } else if (data.results && data.results.length) {
        setHint('上次自动拉取：' + (data.summary || '完成'));
      }
    })
    .catch(function () {});
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
        "autoSync": cfg["autoSync"],
        "syncMode": cfg["syncMode"],
        "zipFallback": cfg["zipFallback"],
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

        # 拉取 / 部署进度
        if path == "/api/sync-status":
            return self.send_json(sync_status_payload(self.cfg))

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

        if path == "/api/sync":
            return self.handle_sync(urllib.parse.parse_qs(parsed.query))

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
            "autoSync": cfg["autoSync"],
            "syncMode": cfg["syncMode"],
            "zipFallback": cfg["zipFallback"],
            "sync": {k: v for k, v in sync_status_payload().items() if k != "projects"},
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

    def handle_sync(self, query):
        """POST /api/sync[?id=<id>]：后台拉取 / 部署项目，用 /api/sync-status 轮询进度"""
        pid = (query.get("id") or [""])[0].strip()
        only = None
        if pid:
            project = self.project_by_id(pid)
            if not project:
                return self.send_json({"ok": False, "error": "没有这个项目：%s" % pid}, status=404)
            if not (project.get("repo") or "").strip():
                return self.send_json(
                    {"ok": False, "error": "项目 %s 没有配置 repo 地址，无法拉取" % pid}, status=400)
            only = [pid]

        started, why = start_sync_batch(
            self.cfg, only_ids=only, mode_label="single" if pid else "manual",
        )
        if not started:
            return self.send_json({"ok": False, "error": why}, status=409)
        return self.send_json({"ok": True, "running": True, "message": why,
                               "sync": sync_status_payload(self.cfg)})

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
    auto_text = {"never": "关闭（只用界面按钮 / --sync）",
                 "missing": "开启 · 启动时补齐缺失项目",
                 "always": "开启 · 启动时补齐缺失 + 更新已有"}.get(cfg.get("autoSync"), "关闭")
    print("  自动拉取   %s" % auto_text)
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
    missing = 0
    for p in cfg["projects"]:
        if p["exists"] and p["entry"]:
            state = "就绪"
        elif p["exists"]:
            state = "缺入口"
        else:
            state = "缺失"
            missing += 1
        print("%-20s %-8s %-6d %s" % (p["id"], state, p["files"], p["dir"]))
    if missing:
        print("\n有 %d 个项目还没下载，可以自动拉取：" % missing)
        print("  python nav-server.py --sync          # 有 git 就 clone，没有就下载压缩包")
        print("或者启动服务后在导航页点「⇩ 部署 / 拉取全部」")


def auto_sync_ids(cfg):
    """按 autoSync 设置挑出启动时要同步的项目 id"""
    mode = cfg.get("autoSync") or "never"
    if mode == "never":
        return []
    if mode == "missing":
        return [p["id"] for p in cfg["projects"]
                if (p.get("repo") or "").strip() and not (p["exists"] and p["entry"])]
    return [p["id"] for p in cfg["projects"] if (p.get("repo") or "").strip()]


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
    parser.add_argument("--sync", action="store_true",
                        help="拉取 / 部署一遍项目（缺失的克隆、已有的更新）后退出，不启动服务")
    parser.add_argument("--only", help="配合 --sync：只处理这些项目 id，逗号分隔（如 a,b）")
    parser.add_argument("--missing-only", action="store_true",
                        help="配合 --sync：只安装缺失的项目，不更新已有的")
    parser.add_argument("--no-auto-sync", action="store_true", help="本次启动不做自动拉取（覆盖 config 的 autoSync）")
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

    if args.only:
        known = {p["id"] for p in cfg["projects"]}
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        unknown = [x for x in wanted if x not in known]
        if unknown:
            print("配置里没有这些项目：%s" % "、".join(unknown), file=sys.stderr)
            return 2
        cfg["onlyIds"] = wanted

    if args.sync:
        ids = cfg.get("onlyIds")
        if args.missing_only and not ids:
            ids = [p["id"] for p in cfg["projects"] if not (p["exists"] and p["entry"])]
        return sync_cli(cfg, ids)

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

    # 自动拉取：放在后台，不挡着服务启动
    if not args.no_auto_sync:
        ids = cfg.get("onlyIds") or auto_sync_ids(cfg)
        if ids:
            started, why2 = start_sync_batch(cfg, ids, mode_label="auto")
            if started:
                print("[nav-server] 自动拉取已开始（%s，共 %d 个项目），导航页可看进度" % (
                    "只安装缺失" if cfg.get("autoSync") == "missing" else "安装缺失 + 更新已有", len(ids)))
            else:
                print("[nav-server] 自动拉取未启动：%s" % why2)
        else:
            print("[nav-server] 项目都在本地，无需拉取（autoSync=%s）" % cfg.get("autoSync"))

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
