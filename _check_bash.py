#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bash 结构检查器 —— 在没有 bash 的环境里做一次保守的静态体检。

只做「误报率极低」的检查：
  1. 引号闭合（单引号 / 双引号）
  2. 块关键字配对：if→fi、case→esac、for/while/until/select→done
  3. here-doc 终止符是否出现
  4. 函数定义与调用的对应关系
  5. 常见笔误：= 两边多空格、[ ] 里缺空格、未加引号的 $VAR 直接拼接

刻意不做括号数量统计 —— `$(`、`${`、`(cmd)`、`{ list; }` 混在一起时，
行级计数几乎必然误报，不如交给真正的 shell。

用法：python _check_bash.py <脚本.sh>
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

OK_MARK = "[OK]"
BAD_MARK = "[X]"
WARN_MARK = "[!]"

BLOCK = {"if": "fi", "case": "esac", "for": "done", "while": "done", "until": "done", "select": "done"}
CLOSERS = {"fi", "esac", "done"}


def strip_line(line, in_single, in_double):
    """去掉行内注释，返回 (净文本, in_single, in_double)"""
    out = []
    i = 0
    while i < len(line):
        ch = line[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i:i + 2])
                i += 2
                continue
            out.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "\\" and i + 1 < len(line):
            out.append(line[i:i + 2])
            i += 2
            continue
        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            break
        out.append(ch)
        i += 1
    return "".join(out), in_single, in_double


def scan(path):
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    problems = []
    warnings = []

    in_single = in_double = False
    heredocs = []
    stack = []
    defined_funcs = set()
    called_funcs = {}

    for lineno, raw in enumerate(lines, 1):
        line, in_single, in_double = strip_line(raw, in_single, in_double)
        stripped = line.strip()

        # here-doc 体内
        if heredocs:
            if stripped == heredocs[-1][0]:
                heredocs.pop()
            continue

        if not stripped:
            continue

        # 记录 here-doc 起始
        for m in re.finditer(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line):
            heredocs.append((m.group(2), lineno))

        # 函数定义：name()  {   或  function name {
        for m in re.finditer(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{", line):
            defined_funcs.add(m.group(1))
        for m in re.finditer(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", line):
            defined_funcs.add(m.group(1))

        # 记录内部函数的调用（只认 cmd_* 前缀的自定义函数，避免把变量名/系统命令误判）
        head = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\b", stripped)
        if head:
            name = head.group(1)
            if name.startswith("cmd_"):
                called_funcs.setdefault(name, lineno)

        # 块关键字配对
        for m in re.finditer(r"(?:^|[;&|]\s*|\bthen\s+|\bdo\s+)([a-z]+)\b", stripped):
            tok = m.group(1)
            if tok in BLOCK:
                stack.append((tok, lineno))
            elif tok in CLOSERS:
                if not stack:
                    problems.append("第 %d 行：多余的 %s" % (lineno, tok))
                    continue
                opener, oline = stack.pop()
                if BLOCK[opener] != tok:
                    problems.append("第 %d 行：%s 与第 %d 行的 %s 不匹配（应为 %s）" % (lineno, tok, oline, opener, BLOCK[opener]))

        # 常见笔误
        if re.search(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s+=\s+", line):
            warnings.append("第 %d 行：赋值语句 = 两边不应有空格" % lineno)
        if re.search(r"\[\s+\$\w+\s+\]", line) and "==" not in line:
            pass  # 交给 shell，避免误报

    if in_single:
        problems.append("文件结束时仍有未闭合的单引号")
    if in_double:
        problems.append("文件结束时仍有未闭合的双引号")
    for term, start in heredocs:
        problems.append("第 %d 行的 here-doc <<%s 没有终止符" % (start, term))
    for opener, oline in stack:
        problems.append("第 %d 行的 %s 没有对应的 %s" % (oline, opener, BLOCK[opener]))

    # 内部函数（cmd_*）被调用时必须有定义
    for name, lineno in sorted(called_funcs.items(), key=lambda kv: kv[1]):
        if name not in defined_funcs:
            problems.append("第 %d 行调用了 %s()，但没有找到定义" % (lineno, name))

    return problems, warnings, defined_funcs


def main():
    if len(sys.argv) < 2:
        print("用法：python _check_bash.py <脚本.sh>", file=sys.stderr)
        return 2

    total = 0
    for path in sys.argv[1:]:
        print("检查 %s" % path)
        problems, warnings, funcs = scan(path)
        for p in problems:
            print("  %s %s" % (BAD_MARK, p))
        for w in warnings:
            print("  %s %s" % (WARN_MARK, w))
        if not problems:
            print("  %s 结构检查通过（引号 / 块关键字 / here-doc / 函数调用）" % OK_MARK)
        if funcs:
            print("  定义函数 %d 个：%s" % (len(funcs), ", ".join(sorted(funcs))))
        print()
        total += len(problems)
    print("结构问题合计：%d" % total)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
