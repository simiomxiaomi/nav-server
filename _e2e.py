#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nav-server 端到端实测：真启服务、真打请求。
用法：python _e2e.py
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
PORT = 1939                      # 用另一个端口，避免和正在运行的服务撞车
BASE = "http://127.0.0.1:%d" % PORT

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


def request(path, method="GET", follow=True):
    """返回 (状态码, 正文, 头字典)。不跟随重定向时返回原始 3xx。"""
    url = BASE + path
    req = urllib.request.Request(url, method=method)
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler if follow else NoRedirect()
    )
    try:
        with opener.open(req, timeout=8) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    print("启动服务（端口 %d）…" % PORT)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "nav-server.py"), "--config", str(HERE / "config.json"),
         "--port", str(PORT), "--quiet"],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # 等端口就绪
        ready = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                st, body, _ = request("/api/health")
                if st == 200:
                    ready = True
                    break
            except (urllib.error.URLError, OSError, ConnectionError):
                pass
            time.sleep(0.2)

        if not ready:
            print("服务没起来，退出码：%s" % proc.poll())
            return 1

        print("服务已就绪\n")

        print("[1] 健康检查与状态")
        st, body, headers = request("/api/health")
        health = json.loads(body)
        check("health 返回 200", st == 200)
        check("health 字段正确", health.get("ok") is True and health.get("service") == "nav-server", health)
        st, body, _ = request("/api/status")
        status = json.loads(body)
        check("status 返回 200", st == 200)
        check("status.port 与启动参数一致", status.get("port") == PORT, status.get("port"))
        check("status 列出 9 个项目", len(status.get("projects", [])) == 9, len(status.get("projects", [])))
        check("status 里项目 url 前缀正确", status["projects"][0]["url"].startswith("/apps/"))

        print("\n[2] 导航页")
        st, body, headers = request("/")
        html = body.decode("utf-8")
        check("导航页 200", st == 200)
        check("Content-Type 是 html", "text/html" in headers.get("Content-Type", ""))
        check("端口被注入到页面", str(PORT) in html, PORT)
        check("包含全部 9 个 /apps/ 链接", html.count("/apps/") >= 9, html.count("/apps/"))
        check("无残留占位符", "__PROJECTS_JSON__" not in html and "__PORT__" not in html)
        injected = json.loads(html.split("var PROJECTS = ")[1].split(";\n")[0])
        check("注入的项目 JSON 可解析", isinstance(injected, list) and len(injected) == 9, len(injected))

        print("\n[3] 项目跳转（全部 9 个）")
        ids = [p["id"] for p in status["projects"]]
        for pid in ids:
            st, body, headers = request("/apps/%s/" % pid)
            ok = st == 200 and b"<html" in body.lower()[:600]
            check("%-18s 200 且是 HTML" % pid, ok, "状态 %s 长度 %s" % (st, len(body)))

        print("\n[4] 静态资源与 MIME")
        for path, want in [
            ("/apps/markdown-studio/style.css", "text/css"),
            ("/apps/markdown-studio/app.js", "javascript"),
            ("/apps/diff-viewer/README.md", "markdown"),
            ("/apps/color-lab/index.html", "html"),
        ]:
            st, body, headers = request(path)
            ct = headers.get("Content-Type", "")
            check("%-38s %s · %s" % (path, st, ct.split(";")[0]), st == 200 and want in ct, ct)
        st, body, headers = request("/apps/markdown-studio/style.css")
        check("响应带 Last-Modified（可缓存）", "Last-Modified" in headers, headers.keys())

        print("\n[5] 挂载点缺末尾斜杠时自动重定向")
        import http.client as _http

        def raw_get(p):
            conn = _http.HTTPConnection("127.0.0.1", PORT, timeout=8)
            conn.request("GET", p)
            resp = conn.getresponse()
            result = (resp.status, resp.getheader("Location"), resp.read())
            conn.close()
            return result

        st, loc, body = raw_get("/apps/diff-viewer")
        check("无斜杠 -> 302", st == 302, st)
        check("Location 正好是 /apps/diff-viewer/", loc == "/apps/diff-viewer/", loc)
        st2, loc2, _ = raw_get("/apps/diff-viewer/")
        check("带斜杠 -> 直接 200", st2 == 200, st2)
        st3, loc3, _ = raw_get("/apps/diff-viewer/app.js")
        check("具体文件 -> 200 不重定向", st3 == 200 and loc3 is None, (st3, loc3))
        st4, body4, _ = request("/apps/diff-viewer", follow=True)
        check("跟随重定向后拿到页面", st4 == 200 and b"<html" in body4.lower()[:600], st4)
        # 关键：这正是重定向存在的意义 —— 相对资源必须能解析对
        st5, body5, _ = request("/apps/diff-viewer/style.css")
        check("重定向后的相对资源可访问", st5 == 200 and b"--del" in body5, st5)

        print("\n[6] 错误处理")
        for path, want in [
            ("/apps/nonexistent/", 404),
            ("/apps/markdown-studio/nope.js", 404),
            ("/nope", 404),
        ]:
            st, body, _ = request(path)
            check("%-36s -> %s" % (path, st), st == want, st)
        st, body, _ = request("/api/nope")
        check("未知 API 返回 404", st == 404, st)

        print("\n[7] 路径穿越防护（不能读到项目目录之外）")
        attacks = [
            "/apps/markdown-studio/../../config.json",
            "/apps/markdown-studio/..%2f..%2fconfig.json",
            "/apps/markdown-studio/%2e%2e/%2e%2e/config.json",
            "/apps/markdown-studio/....//....//config.json",
        ]
        for path in attacks:
            st, body, _ = request(path)
            leaked = b'"projects"' in body or b'"port"' in body
            check("%-48s -> %s%s" % (path[:48], st, "（泄露！）" if leaked else " 已拒绝"),
                  st in (400, 403, 404) and not leaked)

        print("\n[8] 更新检测 API")
        st, body, _ = request("/api/check-update?id=diff-viewer&force=1")
        data = json.loads(body)
        check("检测接口 200", st == 200)
        check("返回结构完整", all(k in data for k in ("id", "state", "message", "method")), list(data))
        check("无 git 时 state=no-git", data["state"] == "no-git", data["state"])
        st, body, _ = request("/api/check-update?id=x")
        check("不存在的项目返回 404", st == 404, st)
        st, body, _ = request("/api/check-update")
        check("缺 id 且未 force 返回 400", st == 400, st)
        st, body, _ = request("/api/check-update?force=1")
        check("force=1 且无 id 时检测全部", st == 200 and b"results" in body, st)

        print("\n[9] 重载配置")
        st, body, _ = request("/api/reload-vault", method="POST")
        data = json.loads(body)
        check("重载返回 200 且 ok", st == 200 and data.get("ok") is True, data)
        check("重载后命令行覆盖的端口保持不变", data.get("port") == PORT, data.get("port"))

        print("\n[10] 并发压一下（线程服务器）")
        import concurrent.futures
        def hit(_):
            s, b, _h = request("/apps/diff-viewer/")
            return s == 200
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(hit, range(24)))
        check("24 个并发请求全部 200", all(results), sum(1 for r in results if not r))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n服务已停止")

    print("\n" + "-" * 46)
    print("通过 %d / 失败 %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
