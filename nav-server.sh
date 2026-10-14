#!/usr/bin/env bash
# =====================================================================
#  工作区导航服务 · nav-server.sh
#  在 Linux / macOS 上一键部署：启动、停止、重启、查看状态与日志
#
#  用法：
#     ./nav-server.sh              启动（默认后台运行）
#     ./nav-server.sh start        同上，后台启动
#     ./nav-server.sh fg           前台运行（Ctrl+C 停止）
#     ./nav-server.sh stop         停止
#     ./nav-server.sh restart      重启
#     ./nav-server.sh status       查看运行状态 + 项目清单
#     ./nav-server.sh log          实时跟踪日志
#     ./nav-server.sh list         只看项目就绪情况
#     ./nav-server.sh config       打印当前配置摘要
#     ./nav-server.sh doctor       环境自检（python3 / 端口 / 配置 / git）
#     ./nav-server.sh install-service  生成 systemd 用户服务（可选）
#
#  作者：苏小晗 · MIT License
# =====================================================================

set -u

# ---------- 颜色 ----------
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'
else
  C_RESET=''; C_DIM=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_CYAN=''
fi

info()  { printf '%s\n' "${C_CYAN}[nav-server]${C_RESET} $*"; }
ok()    { printf '%s\n' "${C_GREEN}[nav-server]${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}[nav-server]${C_RESET} $*" >&2; }
err()   { printf '%s\n' "${C_RED}[nav-server]${C_RESET} $*" >&2; }
die()   { err "$*"; exit 1; }

# ---------- 定位脚本与配置 ----------
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
case "$SCRIPT_PATH" in
  /*) ;;
  *) SCRIPT_PATH="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd)/$(basename "$SCRIPT_PATH")" ;;
esac
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

PY_SCRIPT="$SCRIPT_DIR/nav-server.py"
CONFIG_FILE="${NAV_CONFIG:-$SCRIPT_DIR/config.json}"
PID_FILE="${NAV_PID:-$SCRIPT_DIR/.nav-server.pid}"
LOG_FILE="${NAV_LOG:-$SCRIPT_DIR/nav-server.log}"
PYTHON_BIN="${PYTHON:-}"
SERVICE_NAME="nav-server"

[ -f "$PY_SCRIPT" ] || die "找不到 nav-server.py（应位于 $SCRIPT_DIR）"
[ -f "$CONFIG_FILE" ] || die "找不到配置文件：$CONFIG_FILE"

# ---------- 读取配置（只解析需要的字段，避免依赖 jq） ----------
json_field() {
  # $1 = 字段名（顶层），优先 python3，回退到 grep
  local field="$1"
  if [ -n "$PYTHON_BIN" ]; then
    "$PYTHON_BIN" - "$CONFIG_FILE" "$field" <<'PY' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    v = cfg.get(sys.argv[2], "")
    if isinstance(v, bool):
        print("true" if v else "false")
    else:
        print(v)
except Exception:
    print("")
PY
  else
    sed -n "s/.*\"$field\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^,\"}]*\).*/\1/p" "$CONFIG_FILE" | head -n1
  fi
}

resolve_python() {
  if [ -n "$PYTHON_BIN" ]; then
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "PYTHON 指定的解释器不可用：$PYTHON_BIN"
    return 0
  fi
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      return 0
    fi
  done
  die "找不到 python3。请先安装（如 apt install python3），或用 PYTHON=/path/to/python3 ./nav-server.sh 指定"
}

PORT="$(json_field port)"
HOST="$(json_field host)"
TITLE="$(json_field title)"
[ -n "$PORT" ] || PORT=1938
[ -n "$HOST" ] || HOST="0.0.0.0"
[ -n "$TITLE" ] || TITLE="工作区导航"

LOCAL_URL="http://127.0.0.1:${PORT}/"

# ---------- 进程状态 ----------
read_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null | tr -dc '0-9')"
  [ -n "$pid" ] || return 1
  printf '%s' "$pid"
}

is_running() {
  local pid
  pid="$(read_pid)" || return 1
  kill -0 "$pid" 2>/dev/null
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

# ---------- 命令 ----------
cmd_start() {
  resolve_python

  if is_running; then
    warn "服务已经在运行（PID $(read_pid)）"
    info "地址：$LOCAL_URL   · 要看日志：$0 log"
    return 0
  fi

  if port_in_use "$PORT"; then
    die "端口 $PORT 已被占用。要么先停掉占用者，要么改 $CONFIG_FILE 里的 port"
  fi

  info "启动服务…（$TITLE · 端口 $PORT）"
  : > "$LOG_FILE" 2>/dev/null || true

  # nohup + 标准输入重定向，保证断开终端后仍存活
  nohup "$PYTHON_BIN" "$PY_SCRIPT" --config "$CONFIG_FILE" >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"

  # 等端口就绪，最多 10 秒
  local waited=0
  while [ "$waited" -lt 100 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      err "启动失败，最后几行日志："
      tail -n 20 "$LOG_FILE" >&2
      rm -f "$PID_FILE"
      return 1
    fi
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS --max-time 1 "${LOCAL_URL}api/health" >/dev/null 2>&1; then break; fi
    elif command -v wget >/dev/null 2>&1; then
      if wget -q -T 1 -O /dev/null "${LOCAL_URL}api/health" 2>/dev/null; then break; fi
    else
      sleep 0.6
      break
    fi
    sleep 0.1
    waited=$((waited + 1))
  done

  if kill -0 "$pid" 2>/dev/null; then
    ok "启动成功 — PID $pid"
    info "导航页：$LOCAL_URL"
    info "日志：  $LOG_FILE"
  else
    err "服务没能存活，请看日志：$LOG_FILE"
    tail -n 20 "$LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi
}

cmd_stop() {
  if ! is_running; then
    warn "服务没有在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid; pid="$(read_pid)"
  info "停止服务（PID $pid）…"
  kill "$pid" 2>/dev/null || true

  local waited=0
  while [ "$waited" -lt 50 ] && kill -0 "$pid" 2>/dev/null; do
    sleep 0.1
    waited=$((waited + 1))
  done

  if kill -0 "$pid" 2>/dev/null; then
    warn "进程没响应 TERM，改用 KILL"
    kill -9 "$pid" 2>/dev/null || true
    sleep 0.2
  fi

  rm -f "$PID_FILE"
  ok "已停止"
}

cmd_restart() {
  cmd_stop
  sleep 0.3
  cmd_start
}

cmd_status() {
  resolve_python
  if is_running; then
    local pid; pid="$(read_pid)"
    ok "运行中 — PID $pid"
    info "地址：  $LOCAL_URL"
    info "配置：  $CONFIG_FILE"
    info "日志：  $LOG_FILE"
    if command -v curl >/dev/null 2>&1; then
      local health
      health="$(curl -fsS --max-time 2 "${LOCAL_URL}api/health" 2>/dev/null || true)"
      if [ -n "$health" ]; then
        printf '%s\n' "${C_DIM}健康检查：$health${C_RESET}"
      else
        warn "进程在，但 HTTP 没响应（可能正在启动）"
      fi
    fi
    echo
    cmd_list
  else
    warn "未运行（端口 $PORT）"
    [ -f "$PID_FILE" ] && warn "残留的 PID 文件：$PID_FILE"
    return 1
  fi
}

cmd_list() {
  resolve_python
  "$PYTHON_BIN" "$PY_SCRIPT" --config "$CONFIG_FILE" --list
}

cmd_log() {
  [ -f "$LOG_FILE" ] || die "还没有日志文件：$LOG_FILE"
  info "跟踪日志（Ctrl+C 退出）— $LOG_FILE"
  tail -n 40 -f "$LOG_FILE"
}

cmd_fg() {
  resolve_python
  if port_in_use "$PORT"; then
    die "端口 $PORT 已被占用"
  fi
  info "前台运行（Ctrl+C 停止）…"
  exec "$PYTHON_BIN" "$PY_SCRIPT" --config "$CONFIG_FILE"
}

cmd_config() {
  resolve_python
  echo "${C_BOLD}当前配置${C_RESET}（$CONFIG_FILE）"
  echo "  标题    $TITLE"
  echo "  监听    $HOST:$PORT"
  echo "  地址    $LOCAL_URL"
  echo "  Python  $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
  echo
  echo "${C_DIM}改端口：编辑 $CONFIG_FILE 里的 \"port\"，然后 $0 restart${C_RESET}"
}

cmd_doctor() {
  echo "${C_BOLD}环境自检${C_RESET}"
  echo "  脚本目录    $SCRIPT_DIR"
  echo "  配置文件    $CONFIG_FILE"
  echo

  # Python
  if command -v python3 >/dev/null 2>&1; then
    ok "python3 可用：$(python3 --version 2>&1)"
  elif command -v python >/dev/null 2>&1; then
    warn "没有 python3，但有 python：$(python --version 2>&1)"
  else
    err "找不到 python（服务无法启动）"
  fi

  # 配置文件
  if [ -n "$PYTHON_BIN" ] || command -v python3 >/dev/null 2>&1; then
    resolve_python >/dev/null 2>&1 || true
    if [ -n "$PYTHON_BIN" ]; then
      if "$PYTHON_BIN" -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" "$CONFIG_FILE" 2>/dev/null; then
        ok "config.json 是合法 JSON"
      else
        err "config.json 解析失败"
      fi
    fi
  fi

  # 端口
  if port_in_use "$PORT"; then
    warn "端口 $PORT 已被占用"
  else
    ok "端口 $PORT 空闲"
  fi

  # git（更新检测要用）
  if command -v git >/dev/null 2>&1; then
    ok "git 可用：$(git --version 2>&1)"
  else
    warn "没有 git — 更新检测会退回 GitHub API（可能受网络限制）"
  fi

  # 项目就绪情况
  echo
  cmd_list

  echo
  if is_running; then
    ok "服务正在运行（PID $(read_pid)）"
  else
    info "服务当前未运行 — 用 $0 start 启动"
  fi
}

cmd_install_service() {
  resolve_python
  local unit_dir="$HOME/.config/systemd/user"
  local unit_file="$unit_dir/$SERVICE_NAME.service"

  mkdir -p "$unit_dir" || die "无法创建 $unit_dir"

  cat > "$unit_file" <<EOF
[Unit]
Description=$TITLE (nav-server)
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_BIN $PY_SCRIPT --config $CONFIG_FILE
Restart=on-failure
RestartSec=3
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF

  ok "已生成 systemd 用户服务：$unit_file"
  echo
  echo "  启用并启动："
  echo "    systemctl --user daemon-reload"
  echo "    systemctl --user enable --now $SERVICE_NAME"
  echo
  echo "  想开机自启（未登录也运行）："
  echo "    sudo loginctl enable-linger \$USER"
  echo
  echo "  查看状态 / 日志："
  echo "    systemctl --user status $SERVICE_NAME"
  echo "    journalctl --user -u $SERVICE_NAME -f"
  echo
  echo "  卸载："
  echo "    systemctl --user disable --now $SERVICE_NAME && rm $unit_file"
}

cmd_help() {
  sed -n '2,25p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'
}

# ---------- 入口 ----------
ACTION="${1:-start}"
case "$ACTION" in
  start)            cmd_start ;;
  stop)             cmd_stop ;;
  restart)          cmd_restart ;;
  status)           cmd_status ;;
  list|ls)          cmd_list ;;
  log|logs)         cmd_log ;;
  fg|foreground)    cmd_fg ;;
  config|conf)      cmd_config ;;
  doctor|check)     cmd_doctor ;;
  install-service)  cmd_install_service ;;
  help|-h|--help)   cmd_help ;;
  *) err "未知命令：$ACTION"; echo; cmd_help; exit 2 ;;
esac
