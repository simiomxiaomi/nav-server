#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nav-server 自测 —— 覆盖配置解析、更新检测的纯逻辑、路径安全等。
不启动 HTTP 服务（那部分由端到端测试覆盖），不需要网络。

用法：python _selftest.py
"""
import json
import io
import os
import shutil
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import importlib.util

spec = importlib.util.spec_from_file_location("navserver", HERE / "nav-server.py")
nav = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nav)

passed = 0
failed = 0

# 测试用的临时目录放在工作区内（系统 TEMP 可能受沙箱限制）
TMP_ROOT = HERE / "_tmp_selftest"
COUNTER = [0]

# 上一次跑崩了可能留下残骸，先清干净再开始
if TMP_ROOT.is_dir():
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


def new_tmp_dir():
    COUNTER[0] += 1
    d = TMP_ROOT / ("case%02d" % COUNTER[0])
    d.mkdir(parents=True, exist_ok=True)
    return d


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  [OK] %s" % name)
    else:
        failed += 1
        print("  [X]  %s%s" % (name, ("\n       -> " + str(extra)) if extra else ""))


def write_cfg(data):
    tmp = new_tmp_dir() / "config.json"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp


print("\n[1] 配置解析与校验")
base = {
    "port": 1938, "host": "0.0.0.0", "title": "测试", "root": ".",
    "projects": [{"id": "demo", "name": "Demo", "path": ".", "repo": "https://github.com/a/b"}],
}
cfg, err = nav.load_config(write_cfg(base))
check("合法配置能加载", err is None and cfg is not None, err)
check("端口被解析为 int", cfg and cfg["port"] == 1938 and isinstance(cfg["port"], int))
check("项目被归一化", cfg and len(cfg["projects"]) == 1 and cfg["projects"][0]["id"] == "demo")
check("rootPath 是绝对路径", cfg and Path(cfg["rootPath"]).is_absolute())
check("exists 字段被计算", cfg and isinstance(cfg["projects"][0]["exists"], bool))
check("files 字段被计算", cfg and isinstance(cfg["projects"][0]["files"], int))

cfg2, err2 = nav.load_config(write_cfg({**base, "port": "8080"}))
check("端口支持字符串数字", err2 is None and cfg2["port"] == 8080)

check("端口越界被拒", nav.load_config(write_cfg({**base, "port": 0}))[1] is not None)
check("端口非数字被拒", nav.load_config(write_cfg({**base, "port": "abc"}))[1] is not None)
check("projects 非数组被拒", nav.load_config(write_cfg({**base, "projects": {}}))[1] is not None)
check("项目缺 id 被拒", nav.load_config(write_cfg({**base, "projects": [{"name": "x"}]}))[1] is not None)
check("项目 id 非法字符被拒",
      nav.load_config(write_cfg({**base, "projects": [{"id": "../etc"}]}))[1] is not None)
check("项目 id 重复被拒",
      nav.load_config(write_cfg({**base, "projects": [{"id": "a"}, {"id": "a"}]}))[1] is not None)

bad_json = new_tmp_dir() / "config.json"
bad_json.write_text("{ 这不是 json }", encoding="utf-8")
msg = nav.load_config(bad_json)[1]
check("非法 JSON 给出可读错误", msg is not None and "JSON" in msg, msg)
check("文件不存在给出提示", "找不到" in (nav.load_config(HERE / "no-such.json")[1] or ""))

bom_cfg = new_tmp_dir() / "config.json"
bom_cfg.write_text(json.dumps({"port": 1938, "projects": []}), encoding="utf-8-sig")
check("带 BOM 的配置也能读（Windows 记事本存的）", nav.load_config(bom_cfg)[1] is None)

print("\n[2] GitHub 地址解析")
cases = [
    ("https://github.com/simiomxiaomi/diff-viewer", "simiomxiaomi/diff-viewer"),
    ("https://github.com/simiomxiaomi/diff-viewer.git", "simiomxiaomi/diff-viewer"),
    ("https://github.com/simiomxiaomi/diff-viewer/", "simiomxiaomi/diff-viewer"),
    ("git@github.com:simiomxiaomi/color-lab.git", "simiomxiaomi/color-lab"),
    ("https://github.com/a/b/tree/main", "a/b"),
    ("", ""),
    ("https://gitlab.com/a/b", ""),
]
for url, want in cases:
    got = nav.normalize_github_repo(url)
    check("解析 %r -> %r" % (url[:46], want), got == want, got)

print("\n[3] 代理地址拼接")
cfg, _ = nav.load_config(write_cfg({**base, "proxy": "https://ghproxy.net/"}))
api = "https://api.github.com/repos/a/b/commits/main"
proxied = cfg["proxy"].rstrip("/") + "/" + api
check("代理前缀正确拼接", proxied == "https://ghproxy.net/https://api.github.com/repos/a/b/commits/main", proxied)
check("proxy 默认为空串", nav.load_config(write_cfg(base))[0]["proxy"] == "")

print("\n[4] 路径安全（穿越防护）")
handler_probe = nav.NavHandler
root = HERE

def safe(base_dir, rel):
    base = Path(base_dir).resolve()
    target = (base / rel.lstrip("/")).resolve()
    try:
        target.relative_to(base)
        return target
    except ValueError:
        return None

check("普通相对路径通过", safe(root, "config.json") is not None)
check("子目录通过", safe(root, "sub/a.js") is not None)
check("../ 被拦", safe(root, "../secret.txt") is None)
check("多级 ../ 被拦", safe(root, "../../etc/passwd") is None)
check("绝对路径被拦或落回根内", safe(root, "/etc/passwd") is None or safe(root, "/etc/passwd").is_relative_to(root))
# 与服务器一致的 safe_path 行为
check("safe_path('../x') 返回 None", handler_probe.safe_path(None, root, "../x") is None)
check("safe_path('ok/a.css') 有值", handler_probe.safe_path(None, root, "ok/a.css") is not None)

print("\n[5] 端口可用性探测")
ok, why = nav.ensure_port_available("127.0.0.1", 0)
check("探测函数能正常工作", isinstance(ok, bool) and isinstance(why, str))
ok2, why2 = nav.ensure_port_available("127.0.0.1", 1)      # 特权端口，普通用户应失败
check("特权端口被正确报告", ok2 is False or ok2 is True)

print("\n[6] 更新检测：无 git 时的降级")
proj = {
    "id": "ghost", "name": "Ghost", "repo": "https://github.com/a/b",
    "dir": new_tmp_dir(), "exists": True,
    "entry": False, "files": 0, "path": "", "icon": "", "desc": "", "tag": "",
}
cfg3, _ = nav.load_config(write_cfg(base))
cfg3["cacheTTL"] = 0
res = nav.check_single_project(cfg3, proj, force=True)
check("返回结构完整", all(k in res for k in
      ("id", "state", "message", "branch", "localCommit", "remoteCommit", "behind", "method")))
check("本地非 git 仓库时 state=no-git", res["state"] == "no-git", res)
check("message 能说明原因", bool(res["message"]), res["message"])

missing = dict(proj)
missing["dir"] = new_tmp_dir() / "nope"
missing["exists"] = False
res2 = nav.check_single_project(cfg3, missing, force=True)
check("目录不存在时 state=error", res2["state"] == "error", res2)

print("\n[7] 缓存行为")
cfg4, _ = nav.load_config(write_cfg(base))
cfg4["cacheTTL"] = 300
nav._UPDATE_CACHE.clear()
nav.check_single_project(cfg4, proj, force=True)
cached = nav.check_single_project(cfg4, proj, force=False)
check("命中缓存时标记 cached=True", cached.get("cached") is True, cached.get("cached"))
forced = nav.check_single_project(cfg4, proj, force=True)
check("force=True 时不使用缓存", not forced.get("cached"), forced.get("cached"))
cfg4["cacheTTL"] = 0
nav._UPDATE_CACHE.clear()
nav.check_single_project(cfg4, proj, force=True)
no_cache = nav.check_single_project(cfg4, proj, force=False)
check("cacheTTL=0 时每次都重新检测", not no_cache.get("cached"))

print("\n[8] 导航页渲染")
page_cfg, _ = nav.load_config(write_cfg({
    **base,
    "projects": [
        {"id": "alpha", "name": "Alpha", "desc": "描述 A", "icon": "🎨", "path": "."},
        {"id": "beta", "name": "Beta", "desc": "描述 B", "path": "."},
    ],
}))
html = nav.build_nav_page(page_cfg).decode("utf-8")
check("是完整 HTML", html.lstrip().startswith("<!DOCTYPE html>"))
check("标题被替换", "测试" in html and "__TITLE__" not in html)
check("端口被注入", "1938" in html and "__PORT__" not in html)
check("项目数被注入", '"Alpha"' in html and '"Beta"' in html)
check("残留占位符已清空", "__PROJECTS_JSON__" not in html and "__CONFIG_JSON__" not in html)
check("项目 url 使用固定前缀", "/apps/alpha/" in html and "/apps/beta/" in html)
check("导航页有「部署 / 拉取全部」按钮", 'id="syncAll"' in html)
check("每张卡片能单独拉取", "data-sync" in html)
check("导航页会轮询拉取进度", "/api/sync-status" in html)
check("脚本里的 JSON 可被前端解析", (lambda: (
    json.loads(html.split("var PROJECTS = ")[1].split(";\n")[0]) and True
))())

xss_cfg, _ = nav.load_config(write_cfg({
    **base, "title": "<script>alert(1)</script>",
    "projects": [{"id": "x", "name": "<img onerror=x>", "desc": "d", "path": "."}],
}))
xss_html = nav.build_nav_page(xss_cfg).decode("utf-8")
check("标题被 HTML 转义", "<script>alert(1)</script>" not in xss_html and "&lt;script&gt;" in xss_html)
check("escape_html 处理引号", nav.escape_html('a"b&c<d>') == "a&quot;b&amp;c&lt;d&gt;")

print("\n[9] 常量与元信息")
check("VERSION 存在", isinstance(nav.VERSION, str) and len(nav.VERSION) >= 3)
check("MIME 表含 markdown", nav.MIME_EXTRA.get(".md", "").startswith("text/markdown"))
check("默认端口是 1938", nav.load_config(write_cfg({"projects": []}))[0]["port"] == 1938)
check("默认 host 是 0.0.0.0", nav.load_config(write_cfg({"projects": []}))[0]["host"] == "0.0.0.0")

print("\n[10] 自动拉取的配置解析")
check("autoSync 默认 always", nav.load_config(write_cfg(base))[0]["autoSync"] == "always")
check("autoSync=true → always",
      nav.load_config(write_cfg({**base, "autoSync": True}))[0]["autoSync"] == "always")
check("autoSync=false → never",
      nav.load_config(write_cfg({**base, "autoSync": False}))[0]["autoSync"] == "never")
check("autoSync=missing 被接受",
      nav.load_config(write_cfg({**base, "autoSync": "missing"}))[0]["autoSync"] == "missing")
check("autoSync 非法值被拒", nav.load_config(write_cfg({**base, "autoSync": "sometimes"}))[1] is not None)
check("syncMode 默认 ff", nav.load_config(write_cfg(base))[0]["syncMode"] == "ff")
check("syncMode=hard 被接受", nav.load_config(write_cfg({**base, "syncMode": "hard"}))[0]["syncMode"] == "hard")
check("syncMode 别名 pull → ff", nav.load_config(write_cfg({**base, "syncMode": "pull"}))[0]["syncMode"] == "ff")
check("syncMode 非法值被拒", nav.load_config(write_cfg({**base, "syncMode": "yolo"}))[1] is not None)
check("zipFallback 默认 true", nav.load_config(write_cfg(base))[0]["zipFallback"] is True)
check("zipFallback 字符串 false 被识别",
      nav.load_config(write_cfg({**base, "zipFallback": "false"}))[0]["zipFallback"] is False)
check("zipFallback 非法值被拒", nav.load_config(write_cfg({**base, "zipFallback": "maybe"}))[1] is not None)
check("syncTimeout 默认 120", nav.load_config(write_cfg(base))[0]["syncTimeout"] == 120)
check("syncTimeout 非数字被拒", nav.load_config(write_cfg({**base, "syncTimeout": "fast"}))[1] is not None)
check("syncTimeout 太小被抬到 10", nav.load_config(write_cfg({**base, "syncTimeout": 1}))[0]["syncTimeout"] == 10)
check("parse_bool 认 yes / 0", nav.parse_bool("yes") == (True, True) and nav.parse_bool(0) == (True, False))

print("\n[11] 压缩包地址与安全解压")
zip_cfg, _ = nav.load_config(write_cfg({**base, "branch": "main"}))
check("压缩包走 codeload",
      nav.zip_url_for(zip_cfg, {"repo": "https://github.com/a/b"})
      == "https://codeload.github.com/a/b/zip/refs/heads/main",
      nav.zip_url_for(zip_cfg, {"repo": "https://github.com/a/b"}))
check("非 GitHub 仓库没有压缩包地址", nav.zip_url_for(zip_cfg, {"repo": "https://gitlab.com/a/b"}) == "")
check("分支名带斜杠时地址依然可用",
      nav.zip_url_for(dict(zip_cfg, branch="feat/x"), {"repo": "https://github.com/a/b"})
      == "https://codeload.github.com/a/b/zip/refs/heads/feat/x",
      nav.zip_url_for(dict(zip_cfg, branch="feat/x"), {"repo": "https://github.com/a/b"}))
proxied_cfg = dict(zip_cfg, proxy="https://ghproxy.net/")
check("镜像前缀被拼上", nav.with_proxy(proxied_cfg, "https://x/y") == "https://ghproxy.net/https://x/y")
check("没配 proxy 时原样返回", nav.with_proxy(zip_cfg, "https://x/y") == "https://x/y")

zip_dir = new_tmp_dir()
zip_path = zip_dir / "repo.zip"
with zipfile.ZipFile(zip_path, "w") as zf:
    zf.writestr("demo-main/index.html", "<h1>hi</h1>")
    zf.writestr("demo-main/sub/a.js", "1")
    zf.writestr("../evil.txt", "x")
    zf.writestr("/abs-evil.txt", "y")
unpack = new_tmp_dir() / "out"
extracted = nav.safe_extract_zip(zip_path, unpack)
check("只解出 2 个正常文件", extracted == 2, extracted)
check("正常文件解压到位", (unpack / "demo-main" / "index.html").is_file())
check("../ 条目被挡掉", not (unpack.parent / "evil.txt").exists() and not (zip_dir / "evil.txt").exists())
check("绝对路径条目被挡掉", not (unpack / "abs-evil.txt").exists())
check("zip_root_of 找出顶层目录", nav.zip_root_of(unpack).name == "demo-main")
check("zip_root_of 对散装目录返回自身", nav.zip_root_of(new_tmp_dir()).name.startswith("case"))

print("\n[12] 同步决策逻辑（打桩 git 与下载）")
cfg_sync, err_sync = nav.load_config(write_cfg(base))
check("同步用的配置能加载", err_sync is None)
cfg_sync["cacheTTL"] = 0

git_state = {"table": {}, "calls": []}
real_run_git = nav.run_git
real_download = nav.download_file


def fake_run_git(args, cwd, timeout=8):
    git_state["calls"].append(list(args))
    if args[0] == "clone":
        target = Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text("x", encoding="utf-8")
        return True, "Cloning into '%s'..." % target.name
    return git_state["table"].get(tuple(args), (False, "未安装 git（命令不可用）"))


def make_project(base_dir, pid, repo="https://github.com/a/b"):
    return {"id": pid, "name": pid, "repo": repo, "dir": Path(base_dir) / pid,
            "exists": False, "entry": False, "files": 0, "path": pid,
            "icon": "", "desc": "", "tag": ""}


try:
    nav.run_git = fake_run_git

    # 1) 缺失目录 + clone 成功
    p_new = make_project(new_tmp_dir(), "demo")
    res = nav.sync_project(cfg_sync, p_new)
    clone_calls = [c for c in git_state["calls"] if c[0] == "clone"]
    check("缺失目录 → 已部署", res["status"] == "installed", res)
    check("clone 带了分支与目标目录",
          bool(clone_calls) and "--branch" in clone_calls[0] and clone_calls[0][-1] == str(p_new["dir"]),
          clone_calls[:1])
    check("clone 后目录里真有 index.html", (p_new["dir"] / "index.html").is_file())

    # 2) 没有 repo 的项目
    p_norepo = make_project(new_tmp_dir(), "bare", repo="")
    res = nav.sync_project(cfg_sync, p_norepo)
    check("没配 repo → 跳过", res["status"] == "skipped" and "repo" in res["message"], res)

    # 3) 已有 git 仓库：pull 后 HEAD 变了
    git_dir = new_tmp_dir()
    (git_dir / ".git").mkdir()
    (git_dir / "index.html").write_text("x", encoding="utf-8")
    p_git = {"id": "gitproj", "name": "Git", "repo": "https://github.com/a/b", "dir": git_dir,
             "exists": True, "entry": True, "files": 1, "path": "gitproj",
             "icon": "", "desc": "", "tag": ""}
    git_state["table"] = {
        ("rev-parse", "HEAD"): (True, "aaaa111122223333"),
        ("pull", "--ff-only"): (True, "Updating aaaa111..bbbb222"),
        ("rev-parse", "--short", "HEAD"): (True, "bbbb222"),
    }
    res = nav.sync_project(cfg_sync, p_git)
    check("HEAD 变了 → 已更新", res["status"] == "updated" and "bbbb222" in res["message"], res)

    # 4) 已有 git 仓库：HEAD 没变
    git_state["table"] = {
        ("rev-parse", "HEAD"): (True, "cccc3334444"),
        ("pull", "--ff-only"): (True, "Already up to date."),
        ("rev-parse", "--short", "HEAD"): (True, "cccc333"),
    }
    res = nav.sync_project(cfg_sync, p_git)
    check("HEAD 没变 → 已是最新", res["status"] == "current", res)

    # 5) pull 失败、fetch 也失败 → 失败并带原因
    git_state["table"] = {
        ("rev-parse", "HEAD"): (True, "dddd444"),
        ("pull", "--ff-only"): (False, "no tracking information"),
        ("fetch", "origin", "main"): (False, "could not resolve host github.com"),
    }
    res = nav.sync_project(cfg_sync, p_git)
    check("网络不通 → 失败且带真实原因",
          res["status"] == "failed" and "could not resolve host" in res["message"], res)

    # 6) pull 失败但 fetch + merge 能快进
    git_state["table"] = {
        ("rev-parse", "HEAD"): (True, "eeee555"),
        ("pull", "--ff-only"): (False, "no upstream configured"),
        ("fetch", "origin", "main"): (True, ""),
        ("merge", "--ff-only", "origin/main"): (True, "Fast-forward"),
        ("rev-parse", "--short", "HEAD"): (True, "ffff666"),
    }
    res = nav.sync_project(cfg_sync, p_git)
    check("pull 不行时退回 fetch+merge", res["status"] == "updated", res)

    # 7) 本地有分叉，无法快进 → 失败并提示 syncMode hard
    git_state["table"] = {
        ("rev-parse", "HEAD"): (True, "1111aaa"),
        ("pull", "--ff-only"): (False, "no upstream configured"),
        ("fetch", "origin", "main"): (True, ""),
        ("merge", "--ff-only", "origin/main"): (False, "Not possible to fast-forward"),
    }
    res = nav.sync_project(cfg_sync, p_git)
    check("无法快进 → 失败并提示 hard", res["status"] == "failed" and "hard" in res["message"], res)

    # 8) syncMode=hard 走 fetch + reset
    cfg_hard = dict(cfg_sync, syncMode="hard")
    git_state["table"] = {
        ("rev-parse", "HEAD"): (True, "2222bbb"),
        ("fetch", "--prune", "origin", "main"): (True, ""),
        ("reset", "--hard", "origin/main"): (True, "HEAD is now at 3333ccc"),
        ("rev-parse", "--short", "HEAD"): (True, "3333ccc"),
    }
    res = nav.sync_project(cfg_hard, p_git)
    check("hard 模式走 fetch + reset --hard", res["status"] == "updated", res)

    # 9) 已存在但不是 git 仓库 → 不动它
    mine = new_tmp_dir() / "mine"
    mine.mkdir()
    (mine / "index.html").write_text("x", encoding="utf-8")
    p_mine = {"id": "mine", "name": "Mine", "repo": "https://github.com/a/b", "dir": mine,
              "exists": True, "entry": True, "files": 1, "path": "mine",
              "icon": "", "desc": "", "tag": ""}
    res = nav.sync_project(cfg_sync, p_mine)
    check("非 git 且非空 → 跳过（保护用户文件）",
          res["status"] == "skipped" and "不动" in res["message"], res)
    check("跳过时没有动那个目录", sorted(x.name for x in mine.iterdir()) == ["index.html"])

    # 10) 没有 git（clone 失败）+ zipFallback → 用压缩包部署
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("zipped-main/index.html", "<h1>zip</h1>")
        zf.writestr("zipped-main/app.js", "1")
    payload = buf.getvalue()

    def fake_git_missing(args, cwd, timeout=8):
        return False, "未安装 git（命令不可用）"

    def fake_download(cfg, url, target):
        target.write_bytes(payload)
        return True, "已下载 2 KB"

    nav.run_git = fake_git_missing
    nav.download_file = fake_download
    cfg_zip = dict(nav.load_config(write_cfg({**base, "zipFallback": True}))[0])
    p_zip = make_project(new_tmp_dir(), "zipped")
    res = nav.sync_project(cfg_zip, p_zip)
    check("没有 git 时用压缩包部署",
          res["status"] == "installed" and "压缩包" in res["message"], res)
    check("压缩包内容解压到位", (p_zip["dir"] / "index.html").read_text(encoding="utf-8") == "<h1>zip</h1>")
    check("留下了 .nav-server-zip 标记", (p_zip["dir"] / nav.ZIP_MARKER).is_file())

    res = nav.sync_project(cfg_zip, p_zip)
    check("标记目录再拉 → 重新下载覆盖", res["status"] == "updated", res)

    cfg_nozip = dict(cfg_zip, zipFallback=False)
    res = nav.sync_project(cfg_nozip, p_zip)
    check("zipFallback 关闭时标记目录被跳过", res["status"] == "skipped", res)

    # 11) clone 失败 + 没开 zipFallback → 失败并给建议
    p_fail = make_project(new_tmp_dir(), "nofallback")
    res = nav.sync_project(cfg_nozip, p_fail)
    check("clone 失败且兜底关闭 → 失败并提示",
          res["status"] == "failed" and ("zipFallback" in res["message"] or "proxy" in res["message"]), res)
finally:
    nav.run_git = real_run_git
    nav.download_file = real_download

print("\n[13] 项目状态刷新与批量汇总")
case_dir = new_tmp_dir()
(case_dir / "demo").mkdir()
(case_dir / "demo" / "index.html").write_text("x", encoding="utf-8")
case_file = case_dir / "config.json"
case_file.write_text(json.dumps({
    "port": 1938, "root": ".", "projects": [{"id": "demo", "path": "demo"}],
}, ensure_ascii=False), encoding="utf-8")
cfg_refresh, err_refresh = nav.load_config(case_file)
check("config 加载时就能算出 exists/entry",
      err_refresh is None and cfg_refresh["projects"][0]["exists"] and cfg_refresh["projects"][0]["entry"])
(case_dir / "demo" / "app.js").write_text("1", encoding="utf-8")
check("刷新前文件数还是 1", cfg_refresh["projects"][0]["files"] == 1, cfg_refresh["projects"][0]["files"])
nav.refresh_project_state(cfg_refresh)
check("refresh_project_state 重新统计文件数", cfg_refresh["projects"][0]["files"] == 2,
      cfg_refresh["projects"][0]["files"])
shutil.rmtree(case_dir / "demo")
nav.refresh_project_state(cfg_refresh)
check("目录没了 → exists 变 False", cfg_refresh["projects"][0]["exists"] is False)

check("summarize_sync 汇总各类结果",
      nav.summarize_sync([{"status": "installed"}, {"status": "updated"}, {"status": "failed"}])
      == "拉取完成：已部署 1，已更新 1，失败 1",
      nav.summarize_sync([{"status": "installed"}, {"status": "updated"}, {"status": "failed"}]))
check("summarize_sync 空列表有话说", nav.summarize_sync([]) == "没有需要拉取的项目")
check("没配 repo 的项目不进拉取队列",
      nav.pick_sync_targets({"projects": [{"id": "a", "repo": ""}, {"id": "b", "repo": "x"}]})[0]["id"] == "b")
check("autoSync=never 时不做启动拉取",
      nav.auto_sync_ids({"autoSync": "never", "projects": [{"id": "a", "repo": "x", "exists": False, "entry": False}]}) == [])
check("autoSync=missing 时只挑缺失的",
      nav.auto_sync_ids({"autoSync": "missing", "projects": [
          {"id": "a", "repo": "x", "exists": False, "entry": False},
          {"id": "b", "repo": "x", "exists": True, "entry": True}]}) == ["a"])
check("autoSync=always 时全都挑",
      sorted(nav.auto_sync_ids({"autoSync": "always", "projects": [
          {"id": "a", "repo": "x", "exists": False, "entry": False},
          {"id": "b", "repo": "x", "exists": True, "entry": True}]})) == ["a", "b"])

print("\n" + "-" * 46)
print("通过 %d / 失败 %d" % (passed, failed))

# 清理测试临时目录
try:
    if TMP_ROOT.is_dir():
        shutil.rmtree(TMP_ROOT)
except OSError:
    pass

sys.exit(1 if failed else 0)
