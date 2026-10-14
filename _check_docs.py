#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档一致性检查 —— 确认文档里提到的文件、参数、接口都真实存在。
用法：python _check_docs.py
"""
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  [OK] %s" % name)
    else:
        failed += 1
        print("  [X]  %s%s" % (name, ("\n       -> " + str(extra)) if extra else ""))


deploy = (HERE / "DEPLOY-LINUX.md").read_text(encoding="utf-8")
readme = (HERE / "README.md").read_text(encoding="utf-8")
py_src = (HERE / "nav-server.py").read_text(encoding="utf-8")
sh_src = (HERE / "nav-server.sh").read_text(encoding="utf-8")

print("\n[1] 文档里引用的文件都真实存在")
for fname in ["nav-server.py", "nav-server.sh", "config.json", "config.example.json", "README.md", "LICENSE"]:
    check("存在 %s" % fname, (HERE / fname).is_file())
    check("DEPLOY-LINUX 提到 %s" % fname, fname in deploy)

print("\n[2] Python 命令行参数与文档一致")
for flag in ["--config", "--port", "--host", "--list", "--quiet", "--version"]:
    check("nav-server.py 支持 %s" % flag, flag in py_src)
    check("文档提到 %s" % flag, flag in deploy or flag in readme)
check("文档里的短选项 -c / -p 有对应实现",
      re.search(r'"-c",\s*"--config"', py_src) is not None and re.search(r'"-p",\s*"--port"', py_src) is not None)

print("\n[3] shell 子命令与文档一致")
subcmds = re.findall(r"^\s{2}(\w[\w|-]*)\)\s", sh_src, re.M)
subcmds = [s.split("|")[0] for s in subcmds]
for cmd in ["start", "stop", "restart", "status", "list", "log", "fg", "config", "doctor", "install-service"]:
    check("脚本实现了 %s" % cmd, cmd in subcmds, subcmds)
    check("文档提到 %s" % cmd, cmd in deploy or cmd in readme)
check("脚本实现了 help", "help" in subcmds, subcmds)
check("脚本用法里说明了 help", "help|-h|--help" in sh_src or "--help" in sh_src)

print("\n[4] HTTP 接口与文档一致")
for route in ["/api/health", "/api/status", "/api/check-update", "/api/reload-vault", "/apps/"]:
    check("代码里有 %s" % route, route in py_src)
    check("文档提到 %s" % route, route in deploy or route in readme)

print("\n[5] 配置字段与文档一致")
cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
for key in cfg:
    check("config.json 含字段 %s" % key, key in deploy or key in readme)
check("文档声明默认端口 1938", "1938" in deploy and cfg.get("port") == 1938)
check("文档说明 host 默认 0.0.0.0", "0.0.0.0" in deploy and cfg.get("host") == "0.0.0.0")

print("\n[6] 交叉引用有效")
check("README 链到 DEPLOY-LINUX.md", "DEPLOY-LINUX.md" in readme)
check("DEPLOY-LINUX 链回 README.md", "README.md" in deploy)
check("DEPLOY-LINUX 链到 config.example.json", "config.example.json" in deploy)
check("DEPLOY-LINUX 没有指向不存在的文件",
      all(not p or (HERE / p).exists() for p in re.findall(r"\]\(([\w.-]+\.(?:md|json|py|sh))\)", deploy)),
      [p for p in re.findall(r"\]\(([\w.-]+\.(?:md|json|py|sh))\)", deploy) if not (HERE / p).exists()])

print("\n[7] 部署文档覆盖了关键步骤")
steps = {
    "克隆仓库": "git clone",
    "检查 Python": "python3 --version",
    "配置校验": "--list",
    "前台启动": "python3 nav-server.py",
    "后台启动": "nav-server.sh start",
    "健康检查": "/api/health",
    "开机自启": "install-service",
    "systemd": "systemctl --user",
    "防火墙": "ufw",
    "端口被占用排查": "lsof",
    "卸载": "rm -rf",
}
for label, needle in steps.items():
    check("涵盖「%s」" % label, needle in deploy)

print("\n[8] 文档安全性提醒")
check("提醒了 host 暴露风险", "安全提醒" in deploy or "没有登录认证" in deploy)
check("提醒了端口占用", "端口被占用" in deploy)
check("提醒了没有 git 的影响", "检测更新" in deploy and "git" in deploy)

print("\n" + "-" * 46)
print("通过 %d / 失败 %d" % (passed, failed))
sys.exit(1 if failed else 0)
