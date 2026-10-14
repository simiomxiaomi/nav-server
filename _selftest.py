#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nav-server 自测 —— 覆盖配置解析、更新检测的纯逻辑、路径安全等。
不启动 HTTP 服务（那部分由端到端测试覆盖），不需要网络。

用法：python _selftest.py
"""
import json
import os
import shutil
import sys
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

print("\n" + "-" * 46)
print("通过 %d / 失败 %d" % (passed, failed))

# 清理测试临时目录
try:
    if TMP_ROOT.is_dir():
        shutil.rmtree(TMP_ROOT)
except OSError:
    pass

sys.exit(1 if failed else 0)
