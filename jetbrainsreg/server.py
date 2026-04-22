"""
FingerprintReg Web 控制面板后端
FastAPI + WebSocket 实时推送 + 线程池并发注册 + 结果持久化
"""
import asyncio
import csv
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .register import (register_one, fill_card_info, clear_card_info, confirm_card,
                       scan_debug_browsers, connect_browser_by_port, open_browsers,
                       cleanup_stale_data_dirs, login_and_check, login_batch,
                       reset_port_counter,
                       TaskStatus, AccountResult, LoginResult)
from . import captcha_service

logger = logging.getLogger("jetbrainsreg.server")

app = FastAPI(title="FingerprintReg", version="0.3.0")

# ── 静态文件 ──
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── 持久化路径（用项目目录下的绝对路径） ──
_PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = _PROJECT_DIR / "output"
DATA_DIR.mkdir(exist_ok=True)
ACCOUNTS_JSON = DATA_DIR / "accounts.json"
ACCOUNTS_CSV = DATA_DIR / "accounts.csv"


# ── 持久化函数 ──

def _load_history() -> list[dict]:
    """启动时从 JSON 加载历史成功账号"""
    if ACCOUNTS_JSON.exists():
        try:
            data = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.warning(f"加载历史记录失败: {e}")
    return []


_file_lock = threading.Lock()


def _save_account(account: dict):
    """追加一条成功账号到 JSON 和 CSV（线程安全）"""
    with _file_lock:
        # JSON — 直接用内存中的 state.history，不重新读文件
        ACCOUNTS_JSON.write_text(
            json.dumps(state.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # CSV（追加模式）
        csv_exists = ACCOUNTS_CSV.exists() and ACCOUNTS_CSV.stat().st_size > 0
        with open(ACCOUNTS_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not csv_exists:
                writer.writerow(["#", "email", "password", "time"])
            writer.writerow([
                account.get("id", ""),
                account.get("email", ""),
                account.get("password", ""),
                account.get("time", ""),
            ])


def _save_history():
    """将完整 history 保存到 JSON（线程安全），用于更新绑卡状态等非新增场景"""
    with _file_lock:
        ACCOUNTS_JSON.write_text(
            json.dumps(state.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _update_country(email: str, country: str, country_name: str = ""):
    """更新某个账号的国家信息并持久化 + 广播"""
    changed = False
    with state.lock:
        for h in state.history:
            if h.get("email") == email:
                if h.get("country", "") != country:
                    h["country"] = country
                    h["country_name"] = country_name
                    changed = True
                break
    if changed:
        _save_history()
        _broadcast_from_thread({"type": "history_update", "history": state.history})


def _update_card_status(email: str, card_status: str, card_detail: str = ""):
    """更新某个账号的绑卡状态并持久化 + 广播"""
    changed = False
    with state.lock:
        for h in state.history:
            if h.get("email") == email:
                old = h.get("card_status", "")
                if old != card_status or h.get("card_detail", "") != card_detail:
                    h["card_status"] = card_status
                    h["card_detail"] = card_detail
                    h["card_check_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    changed = True
                break
    if changed:
        _save_history()
        _broadcast_from_thread({"type": "history_update", "history": state.history})


# ── 全局状态 ──

class AppState:
    def __init__(self):
        self.tasks: dict[int, dict] = {}
        self.results: list[dict] = []
        self.history: list[dict] = []   # 历史成功记录（持久化）
        self.browsers: dict[int, object] = {}  # task_id → Chromium 实例（成功注册后保留）
        self.running = False
        self.total_count = 0
        self.lock = threading.Lock()
        self.ws_connections: list[WebSocket] = []

state = AppState()


# ── WebSocket 管理 ──

async def _broadcast(message: dict):
    dead = []
    for ws in state.ws_connections:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in state.ws_connections:
            state.ws_connections.remove(ws)


_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _broadcast_from_thread(message: dict):
    if _event_loop and _event_loop.is_running():
        asyncio.run_coroutine_threadsafe(_broadcast(message), _event_loop)


# ── 后台绑卡状态实时监测 ──

_card_monitor_running = False


def _start_card_monitor():
    """启动后台线程，定期检查已登录浏览器的绑卡状态变化"""
    global _card_monitor_running
    if _card_monitor_running:
        return
    _card_monitor_running = True

    def _monitor_loop():
        from .register import _check_payment_methods
        while _card_monitor_running:
            try:
                time.sleep(30)  # 每 30 秒检查一次

                # 收集当前标记为 "unbound" 且有活跃浏览器的账号
                targets = []
                with state.lock:
                    for tid, br in list(state.browsers.items()):
                        task_info = state.tasks.get(tid, {})
                        email = task_info.get("email", "")
                        if not email:
                            continue

                        # 查找该 email 在 history 中的绑卡状态
                        hist = None
                        for h in state.history:
                            if h.get("email") == email:
                                hist = h
                                break
                        if not hist:
                            continue

                        # 只监测 "unbound" 的（已绑卡的不需要反复检查）
                        if hist.get("card_status") != "unbound":
                            continue

                        targets.append((tid, br, email))

                for tid, br, email in targets:
                    try:
                        tab = br.latest_tab
                        # 验证浏览器存活并获取当前 URL
                        current_url = tab.url or ""
                    except Exception:
                        # 浏览器已死，清理引用
                        with state.lock:
                            state.browsers.pop(tid, None)
                            state.tasks.pop(tid, None)
                        continue

                    try:
                        # 只在 tokens/payment 相关页面就地检测（navigate=False）
                        # 绝不强制导航 —— 避免打断用户正在操作的绑卡页面
                        if "tokens" in current_url or "payment" in current_url or "account.jetbrains.com" in current_url:
                            has_card, detail = _check_payment_methods(tab, navigate=False)
                            if has_card:
                                logger.info(f"[CardMonitor] {email} 绑卡状态变更: unbound → bound ({detail})")
                                _update_card_status(email, "bound", detail)
                    except Exception as e:
                        logger.debug(f"[CardMonitor] 检查 {email} 失败: {e}")

            except Exception as e:
                logger.debug(f"[CardMonitor] 监测循环异常: {e}")

    thread = threading.Thread(target=_monitor_loop, daemon=True, name="card-monitor")
    thread.start()
    logger.info("[CardMonitor] 后台绑卡状态监测线程已启动")


@app.on_event("startup")
async def _on_startup():
    global _event_loop
    _event_loop = asyncio.get_event_loop()
    # 加载历史成功记录
    state.history = _load_history()
    logger.info(f"已加载 {len(state.history)} 条历史成功记录")
    # 注意：不再在启动时自动清理浏览器数据目录，避免误杀正在运行的浏览器
    # 清理操作改为用户通过「关闭所有浏览器」按钮手动触发
    # 启动后台绑卡状态监测线程
    _start_card_monitor()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.ws_connections.append(ws)
    logger.info(f"WebSocket 连接 +1 (当前 {len(state.ws_connections)})")

    # 发送当前状态快照（含历史记录）
    await ws.send_json({
        "type": "snapshot",
        "running": state.running,
        "tasks": state.tasks,
        "results": state.results,
        "history": state.history,
    })

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in state.ws_connections:
            state.ws_connections.remove(ws)
        logger.info(f"WebSocket 连接 -1 (当前 {len(state.ws_connections)})")


# ── API ──

class StartRequest(BaseModel):
    count: int = 3
    password: str = config.DEFAULT_PASSWORD
    first_name: str = config.DEFAULT_FIRST_NAME
    last_name: str = config.DEFAULT_LAST_NAME
    browser: str = "chrome"  # chrome / edge / fingerprint
    country: str = "JP"      # tokens 页选择的国家代码
    incognito: bool = True   # 是否使用隐私模式
    auto_select_country: bool = True   # 注册后自动选择国家
    auto_click_add_card: bool = True   # 选国家后自动点 Add credit card
    ai_captcha: bool = False  # 是否启用全自动验证码（打码平台/AI）
    fullscreen: bool = False  # 是否最大化浏览器窗口


class FillCardRequest(BaseModel):
    task_id: int = 0            # 0 = 所有窗口
    card_number: str = ""       # 纯数字卡号
    expiry_date: str = ""       # MM/YY
    cvv: str = ""               # 3-4 位安全码
    card_name: str = ""         # 持卡人姓名


@app.get("/")
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>FingerprintReg</h1><p>static/index.html not found</p>")


@app.post("/api/start")
async def start_registration(req: StartRequest):
    if state.running:
        return {"ok": False, "error": "已有任务在运行"}

    if req.count < 1 or req.count > 10:
        return {"ok": False, "error": "窗口数量限制 1-10"}

    state.running = True
    state.total_count = req.count
    state.tasks.clear()
    state.results.clear()

    for i in range(1, req.count + 1):
        state.tasks[i] = {
            "task_id": i,
            "step": 0,
            "step_label": "排队中",
            "email": "",
            "password": req.password,
            "success": None,
            "error": "",
        }

    _broadcast_from_thread({
        "type": "started",
        "count": req.count,
        "tasks": state.tasks,
    })

    thread = threading.Thread(
        target=_run_batch,
        args=(req.count, req.password, req.first_name, req.last_name, req.browser, req.country, req.incognito,
              req.auto_select_country, req.auto_click_add_card, req.ai_captcha, req.fullscreen),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "count": req.count}


@app.get("/api/status")
async def get_status():
    return {
        "running": state.running,
        "tasks": state.tasks,
        "results": state.results,
        "history": state.history,
    }


@app.get("/api/history")
async def get_history():
    """获取历史成功记录"""
    return state.history


class DeleteHistoryRequest(BaseModel):
    ids: list[int] = []  # 要删除的 id 列表，空列表 = 全部删除


@app.post("/api/history/delete")
async def delete_history(req: DeleteHistoryRequest):
    """删除指定的历史记录（或全部删除）"""
    with _file_lock:
        if not req.ids:
            # 全部删除
            count = len(state.history)
            state.history.clear()
        else:
            # 删除指定 id
            id_set = set(req.ids)
            before = len(state.history)
            state.history = [h for h in state.history if h.get("id") not in id_set]
            count = before - len(state.history)

        # 重新编号
        for i, h in enumerate(state.history):
            h["id"] = i + 1

        # 重写 JSON 文件
        ACCOUNTS_JSON.write_text(
            json.dumps(state.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 重写 CSV 文件
        with open(ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["#", "email", "password", "time"])
            for h in state.history:
                writer.writerow([h.get("id", ""), h.get("email", ""),
                                 h.get("password", ""), h.get("time", "")])

    # 广播更新
    _broadcast_from_thread({"type": "history_update", "history": state.history})

    return {"ok": True, "deleted": count, "remaining": len(state.history)}


class ImportRequest(BaseModel):
    text: str = ""               # 原始文本（多行，支持多种格式）
    default_password: str = ""   # 如果文本中没有密码，使用此默认密码
    card_status: str = "unbound" # 导入后的绑卡状态: "unbound" | "" (未检测)


@app.post("/api/history/import")
async def import_accounts(req: ImportRequest):
    """
    导入账号到历史记录。
    支持格式（每行一个账号）:
      - email / password
      - email password
      - email,password
      - email:password
      - email / password / 其他信息（忽略多余字段）
      - 纯邮箱（使用 default_password）
    自动去重：已存在的邮箱跳过。
    """
    import re as _re

    raw = req.text.strip()
    if not raw:
        return {"ok": False, "error": "请输入账号信息"}

    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    # 解析每一行
    parsed = []
    parse_errors = []
    for line_no, line in enumerate(lines, 1):
        # 跳过注释行和标题行
        if line.startswith("#") or line.startswith("//"):
            continue
        lower = line.lower()
        if lower.startswith("email") and ("password" in lower or "密码" in lower):
            continue  # CSV 标题行

        email = ""
        password = ""

        # 尝试多种分隔符: " / ", ",", ":", 空格, tab
        parts = None
        for sep in [" / ", "\t", ",", ":", " "]:
            if sep in line:
                parts = [p.strip() for p in line.split(sep) if p.strip()]
                break

        if parts and len(parts) >= 2:
            # 找出哪个是邮箱
            for p in parts:
                if "@" in p and "." in p:
                    email = p
                    break
            if email:
                # 密码是紧跟邮箱之后的字段
                idx = parts.index(email)
                if idx + 1 < len(parts):
                    password = parts[idx + 1]
            else:
                # 第一个当邮箱，第二个当密码
                email = parts[0]
                password = parts[1]
        elif parts and len(parts) == 1:
            email = parts[0]
        else:
            email = line

        # 清理邮箱
        email = email.strip().lower()
        if not email or "@" not in email:
            parse_errors.append(f"第{line_no}行无法识别: {line[:50]}")
            continue

        # 密码兜底
        if not password:
            password = req.default_password or config.DEFAULT_PASSWORD

        parsed.append({"email": email, "password": password})

    if not parsed:
        return {
            "ok": False,
            "error": f"未识别到任何有效账号" + (f"（{len(parse_errors)} 行解析失败）" if parse_errors else ""),
            "errors": parse_errors[:10],
        }

    # 去重：跳过已存在的邮箱
    existing = {h.get("email", "").lower() for h in state.history}
    new_accounts = []
    skipped = 0
    for acc in parsed:
        if acc["email"] in existing:
            skipped += 1
            continue
        existing.add(acc["email"])  # 防止导入文本内部重复
        new_accounts.append(acc)

    if not new_accounts:
        return {
            "ok": True,
            "imported": 0,
            "skipped": skipped,
            "message": f"所有 {skipped} 个账号已存在，无需导入",
        }

    # 写入 history
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _file_lock:
        for acc in new_accounts:
            account = {
                "id": len(state.history) + 1,
                "email": acc["email"],
                "password": acc["password"],
                "time": now,
                "card_status": req.card_status or "",
                "card_detail": "",
                "card_check_time": "",
            }
            state.history.append(account)

        # 持久化 JSON
        ACCOUNTS_JSON.write_text(
            json.dumps(state.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 追加 CSV
        csv_exists = ACCOUNTS_CSV.exists() and ACCOUNTS_CSV.stat().st_size > 0
        with open(ACCOUNTS_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not csv_exists:
                writer.writerow(["#", "email", "password", "time"])
            for acc in new_accounts:
                writer.writerow([
                    len(state.history),
                    acc["email"],
                    acc["password"],
                    now,
                ])

    # 广播
    _broadcast_from_thread({"type": "history_update", "history": state.history})

    msg = f"成功导入 {len(new_accounts)} 个账号"
    if skipped:
        msg += f"，跳过 {skipped} 个已存在"
    if parse_errors:
        msg += f"，{len(parse_errors)} 行解析失败"

    return {
        "ok": True,
        "imported": len(new_accounts),
        "skipped": skipped,
        "errors": parse_errors[:10],
        "message": msg,
    }


@app.post("/api/stop")
async def stop_registration():
    state.running = False
    # 清除所有进行中的任务状态，防止卡在 "已有任务在运行"
    with state.lock:
        for tid, task in state.tasks.items():
            if task.get("success") is None:
                task["success"] = False
                task["error"] = "用户停止"
                task["step_label"] = "已停止"
    _broadcast_from_thread({"type": "stopped"})
    return {"ok": True}


# ── API Key 配置 ──

class ApiKeyRequest(BaseModel):
    api_key: str = ""


@app.get("/api/settings")
async def get_settings():
    """获取当前设置（含 API Key 脱敏显示）"""
    key = config.YYDS_API_KEY
    masked = ""
    if key:
        if len(key) > 10:
            masked = key[:6] + "****" + key[-4:]
        else:
            masked = key[:3] + "****"
    return {"api_key_masked": masked, "api_key_set": bool(key)}


@app.post("/api/settings/api-key")
async def set_api_key(req: ApiKeyRequest):
    """保存 YYDS Mail API Key"""
    key = req.api_key.strip()
    if not key:
        return {"ok": False, "error": "API Key 不能为空"}
    if not key.startswith("AC-"):
        return {"ok": False, "error": "API Key 格式不对，应以 AC- 开头"}
    try:
        config.save_api_key(key)
        return {"ok": True, "message": "API Key 已保存"}
    except Exception as e:
        return {"ok": False, "error": f"保存失败: {e}"}


# ── 延迟配置 API ──

_DELAY_KEYS = [
    "DELAY_CLICK", "DELAY_INPUT", "DELAY_PAGE_NAV", "DELAY_CAPTCHA_POLL",
    "DELAY_OTP_CHAR", "DELAY_STEP_TRANSITION", "DELAY_BROWSER_STAGGER",
    "EMAIL_POLL_INTERVAL", "EMAIL_POLL_TIMEOUT", "PAGE_TIMEOUT",
]

@app.get("/api/config")
async def get_config():
    """获取当前延迟配置"""
    return {k: getattr(config, k) for k in _DELAY_KEYS}


class UpdateConfigRequest(BaseModel):
    key: str
    value: float


@app.post("/api/config")
async def update_config(req: UpdateConfigRequest):
    """更新单个延迟配置项（运行时生效，不写文件）"""
    if req.key not in _DELAY_KEYS:
        return {"ok": False, "error": f"不支持的配置项: {req.key}"}
    if req.value < 0:
        return {"ok": False, "error": "值不能为负数"}
    setattr(config, req.key, req.value)
    logger.info(f"[Config] {req.key} = {req.value}")
    return {"ok": True, "key": req.key, "value": req.value}


# ── 指纹功能开关 API ──

@app.get("/api/fingerprint-toggles")
async def get_fingerprint_toggles():
    """获取指纹功能开关状态"""
    return config.get_fingerprint_toggles()


class FingerprintTogglesRequest(BaseModel):
    toggles: dict = {}


@app.post("/api/fingerprint-toggles")
async def set_fingerprint_toggles(req: FingerprintTogglesRequest):
    """保存指纹功能开关（运行时生效 + 持久化到 settings.json）"""
    if not req.toggles:
        return {"ok": False, "error": "未提供开关数据"}
    try:
        config.save_fingerprint_toggles(req.toggles)
        logger.info(f"[Config] 指纹开关已更新: {req.toggles}")
        return {"ok": True, "toggles": config.get_fingerprint_toggles()}
    except Exception as e:
        return {"ok": False, "error": f"保存失败: {e}"}


# ── 打码平台配置 API ──

class CaptchaConfigRequest(BaseModel):
    platform: str = ""        # "yescaptcha" / "capsolver" / ""
    client_key: str = ""      # API Key


@app.get("/api/captcha-settings")
async def get_captcha_settings():
    """获取打码平台配置（脱敏显示）"""
    platform = config.CAPTCHA_PLATFORM
    key = config.CAPTCHA_CLIENT_KEY
    masked = ""
    if key:
        if len(key) > 10:
            masked = key[:6] + "****" + key[-4:]
        else:
            masked = key[:3] + "****"
    balance = None
    if platform and key:
        try:
            balance = captcha_service.get_balance()
        except Exception:
            pass
    return {
        "platform": platform,
        "client_key_masked": masked,
        "client_key_set": bool(key),
        "balance": balance,
    }


@app.post("/api/captcha-settings")
async def set_captcha_settings(req: CaptchaConfigRequest):
    """保存打码平台配置"""
    platform = req.platform.strip().lower()
    key = req.client_key.strip()

    if platform and platform not in ("yescaptcha", "capsolver"):
        return {"ok": False, "error": "不支持的平台，请选择 yescaptcha 或 capsolver"}

    if platform and not key:
        return {"ok": False, "error": "请输入 API Key (clientKey)"}

    try:
        config.save_captcha_config(platform, key)

        # 验证 key 可用性
        if platform and key:
            try:
                balance = captcha_service.get_balance()
                return {"ok": True, "message": f"已保存！当前余额: {balance} 积分", "balance": balance}
            except Exception as e:
                return {"ok": True, "message": f"已保存，但验证余额失败: {e}（请检查 Key 是否正确）"}
        else:
            return {"ok": True, "message": "已清除打码平台配置"}
    except Exception as e:
        return {"ok": False, "error": f"保存失败: {e}"}


@app.post("/api/kill-all-browsers")
async def kill_all_browsers():
    """一键关闭所有由 FingerprintReg 管理的浏览器 + 系统中所有带 debug 端口的浏览器进程"""
    import subprocess
    killed = 0
    errors = []

    # 1. 清空 state.browsers（不调用 browser.quit，直接丢弃引用）
    with state.lock:
        browser_count = len(state.browsers)
        state.browsers.clear()
    if browser_count:
        logger.info(f"[KillAll] 已清除 {browser_count} 个浏览器引用")

    # 2. 扫描所有带 --remote-debugging-port 的浏览器进程的 PID，直接 taskkill
    try:
        ps_script = (
            'Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\' or name=\'msedge.exe\'" '
            '| Where-Object { $_.CommandLine -match \'--remote-debugging-port\' } '
            '| Select-Object -ExpandProperty ProcessId'
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            timeout=10, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="ignore")

        pids = set()
        for line in raw.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))

        if pids:
            # 用 taskkill /F /PID 批量强杀（快速、不等待）
            pid_args = []
            for pid in pids:
                pid_args.extend(["/PID", str(pid)])
            try:
                subprocess.run(
                    ["taskkill", "/F"] + pid_args,
                    timeout=10, capture_output=True
                )
                killed = len(pids)
                logger.info(f"[KillAll] taskkill 强制关闭 {killed} 个进程: {pids}")
            except Exception as e:
                errors.append(f"taskkill 失败: {e}")
                # 逐个杀
                for pid in pids:
                    try:
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)], timeout=5, capture_output=True)
                        killed += 1
                    except Exception:
                        pass
        else:
            logger.info("[KillAll] 未发现带 debug 端口的浏览器进程")
    except Exception as e:
        errors.append(f"扫描进程失败: {e}")
        logger.warning(f"[KillAll] 扫描进程失败: {e}")

    # 3. 同时重置运行状态（防止卡在 "已有任务在运行"）
    if state.running:
        state.running = False
        with state.lock:
            for tid, task in state.tasks.items():
                if task.get("success") is None:
                    task["success"] = False
                    task["error"] = "浏览器已关闭"
                    task["step_label"] = "已终止"
        _broadcast_from_thread({"type": "stopped"})
        logger.info("[KillAll] 已重置运行状态")

    # 3.5 重置端口计数器，确保新批次不会与旧端口冲突
    reset_port_counter()

    # 4. 后台清理数据目录（不阻塞返回）
    def _bg_cleanup():
        import time as _t
        _t.sleep(2)  # 等进程完全退出
        try:
            cleanup_stale_data_dirs()
        except Exception:
            pass
    threading.Thread(target=_bg_cleanup, daemon=True).start()

    msg = f"已强制关闭 {killed} 个浏览器进程"
    if errors:
        msg += f"，{len(errors)} 个操作失败"

    return {"ok": True, "message": msg, "killed": killed, "errors": errors}


@app.post("/api/force-start")
async def force_start_registration(req: StartRequest):
    """强制启动新窗口，无视当前是否有任务在运行。新旧窗口共存，都可以一键填卡。"""
    if req.count < 1 or req.count > 10:
        return {"ok": False, "error": "窗口数量限制 1-10"}

    # 不清空已有的 tasks/results/browsers，追加新的任务
    # 计算新的 task_id 起始值（避免与现有冲突）
    existing_ids = set(state.tasks.keys())
    start_id = max(existing_ids, default=0) + 1

    # 如果之前没在运行，标记为运行
    state.running = True

    for i in range(req.count):
        tid = start_id + i
        state.tasks[tid] = {
            "task_id": tid,
            "step": 0,
            "step_label": "排队中",
            "email": "",
            "password": req.password,
            "success": None,
            "error": "",
        }

    _broadcast_from_thread({
        "type": "started",
        "count": req.count,
        "tasks": state.tasks,
    })

    thread = threading.Thread(
        target=_run_batch_force,
        args=(start_id, req.count, req.password, req.first_name, req.last_name, req.browser, req.country, req.incognito,
              req.auto_select_country, req.auto_click_add_card, req.ai_captcha, req.fullscreen),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "count": req.count, "start_id": start_id}


class OpenBrowsersRequest(BaseModel):
    count: int = 1
    browser: str = "chrome"  # chrome / edge
    url: str = "https://account.jetbrains.com/login"  # 默认打开 JetBrains 登录页


@app.post("/api/open-browsers")
async def open_browsers_api(req: OpenBrowsersRequest):
    """打开带调试端口的浏览器窗口，用于一键填卡"""
    if req.count < 1 or req.count > 20:
        return {"ok": False, "error": "数量限制 1-20"}

    results = open_browsers(count=req.count, browser_type=req.browser, url=req.url)
    success = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success > 0,
        "message": f"已打开 {success}/{req.count} 个浏览器窗口",
        "results": results,
    }


@app.get("/api/browsers")
async def get_browsers():
    """扫描系统中所有带 debug 端口的 Chrome/Edge 浏览器实例"""
    # 1. 扫描系统进程
    scanned = scan_debug_browsers()

    # 2. 合并 FingerprintReg 自己注册时保留的浏览器
    with state.lock:
        for task_id, browser in list(state.browsers.items()):
            try:
                tab = browser.latest_tab
                url = tab.url or ""
                scanned.append({
                    "pid": 0,
                    "port": 0,
                    "browser": "jetbrainsreg",
                    "title": f"FingerprintReg #{task_id}",
                    "url": url,
                    "task_id": task_id,
                    "email": state.tasks.get(task_id, {}).get("email", ""),
                })
            except Exception:
                del state.browsers[task_id]

    return scanned


class CheckCardRequest(BaseModel):
    accounts: list[dict]  # [{email, password}, ...]


@app.post("/api/check-card")
async def check_card_binding(req: CheckCardRequest):
    """检测已注册账号是否已绑定银行卡。
    优先使用已打开的浏览器检查；如果没有已打开的浏览器，引导使用一键登录+检测功能。
    """
    results = []
    no_browser_count = 0

    for acc in req.accounts[:20]:
        email = acc.get("email", "")
        password = acc.get("password", "")
        result = {"email": email, "has_card": False, "error": ""}

        try:
            # 查找已打开的浏览器（匹配 email 或扫描系统浏览器）
            found_browser = None
            with state.lock:
                for tid, br in state.browsers.items():
                    task_email = state.tasks.get(tid, {}).get("email", "")
                    if task_email == email:
                        # 验证浏览器仍然存活
                        try:
                            _ = br.latest_tab.url
                            found_browser = br
                            break
                        except Exception:
                            # 浏览器已死，清理引用
                            del state.browsers[tid]
                            break

            if found_browser:
                try:
                    from .register import _check_payment_methods
                    tab = found_browser.latest_tab
                    has_card, detail = _check_payment_methods(tab, navigate=True)
                    result["has_card"] = has_card
                    result["detail"] = detail
                    # 持久化绑卡状态
                    _update_card_status(email, "bound" if has_card else "unbound", detail)
                except Exception as e:
                    result["error"] = str(e)[:80]
            else:
                no_browser_count += 1
                result["error"] = "无已打开的浏览器窗口，请使用「一键登录+检测」功能"

        except Exception as e:
            result["error"] = str(e)[:80]

        results.append(result)

    # 如果所有账号都没有浏览器，给出更明确的提示
    all_failed = all(r.get("error") for r in results)
    return {
        "ok": not all_failed,
        "results": results,
        "hint": "浏览器已关闭，请使用历史记录中的「一键登录+检测」按钮" if no_browser_count == len(results) else "",
    }


# ── 一键登录 + 检测绑卡 API ──

class LoginCheckRequest(BaseModel):
    accounts: list[dict] = []     # [{email, password}, ...]  空 = 全部历史账号
    browser: str = "chrome"       # chrome / edge / fingerprint
    goto_card_page: bool = True   # 未绑卡的自动跳转到绑卡页
    country: str = "JP"           # tokens 页选择的国家代码
    incognito: bool = True
    fullscreen: bool = False      # 是否最大化浏览器窗口


# 一键登录的进行中状态
_login_state = {
    "running": False,
    "progress": [],    # [{email, status, has_card, card_detail, error}, ...]
    "total": 0,
    "done": 0,
}


@app.post("/api/login-and-check")
async def login_and_check_api(req: LoginCheckRequest):
    """一键登录已注册账号并检测绑卡状态（新开浏览器，不依赖已打开的窗口）"""
    if _login_state["running"]:
        return {"ok": False, "error": "已有登录任务在运行中，请等待完成"}

    # 确定要登录的账号列表
    accounts = req.accounts
    if not accounts:
        # 没指定则用历史记录中的全部账号
        accounts = [{"email": h["email"], "password": h["password"]} for h in state.history]
    if not accounts:
        return {"ok": False, "error": "没有可登录的账号"}

    # 限制数量
    if len(accounts) > 20:
        return {"ok": False, "error": f"单次最多登录 20 个账号，当前 {len(accounts)} 个"}

    _login_state["running"] = True
    _login_state["total"] = len(accounts)
    _login_state["done"] = 0
    _login_state["progress"] = [
        {"email": a["email"], "status": "pending", "has_card": False, "card_detail": "", "error": ""}
        for a in accounts
    ]

    # 广播开始
    _broadcast_from_thread({
        "type": "login_started",
        "total": len(accounts),
        "progress": _login_state["progress"],
    })

    _done_counter = [0]  # 用列表包装以支持闭包修改
    _counter_lock = threading.Lock()

    def _run():
        try:
            def on_progress(i, total, result: LoginResult):
                # 并发模式下 i 不是递增的，用独立计数器
                with _counter_lock:
                    _done_counter[0] += 1
                    done_now = _done_counter[0]

                _login_state["done"] = done_now
                _login_state["progress"][i] = {
                    "email": result.email,
                    "status": "done" if result.login_ok else ("error" if result.error else "done"),
                    "login_ok": result.login_ok,
                    "has_card": result.has_card,
                    "card_detail": result.card_detail,
                    "error": result.error,
                    "port": result.port,
                    "country": result.country,
                    "country_name": result.country_name,
                }

                # 持久化绑卡状态 + 国家到 history
                if result.login_ok:
                    card_st = "bound" if result.has_card else "unbound"
                    _update_card_status(result.email, card_st, result.card_detail or "")
                    # 持久化国家信息
                    if result.country:
                        _update_country(result.email, result.country, result.country_name)
                elif result.error:
                    # 登录失败不覆盖已有的绑卡状态（可能之前检测过）
                    pass

                # 保留浏览器实例到全局（供一键填卡使用）
                if result.browser and result.port:
                    with state.lock:
                        login_tid = -(i + 1000)
                        state.browsers[login_tid] = result.browser
                        state.tasks[login_tid] = {
                            "task_id": login_tid,
                            "step": 9,
                            "step_label": "已登录" if result.login_ok else "登录失败",
                            "email": result.email,
                            "password": result.password,
                            "success": result.login_ok,
                            "error": result.error,
                        }

                # 广播进度
                _broadcast_from_thread({
                    "type": "login_progress",
                    "index": i,
                    "total": total,
                    "done": done_now,
                    "result": _login_state["progress"][i],
                    "progress": _login_state["progress"],
                })

            login_batch(
                accounts=accounts,
                browser_type=req.browser,
                goto_card_page=req.goto_card_page,
                country=req.country,
                incognito=req.incognito,
                fullscreen=req.fullscreen,
                on_progress=on_progress,
            )
        except Exception as e:
            logger.error(f"[LoginBatch] 异常: {e}", exc_info=True)
        finally:
            _login_state["running"] = False
            _broadcast_from_thread({
                "type": "login_completed",
                "progress": _login_state["progress"],
            })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {"ok": True, "total": len(accounts), "message": f"开始登录 {len(accounts)} 个账号..."}


@app.get("/api/login-status")
async def get_login_status():
    """获取一键登录的进度"""
    return {
        "running": _login_state["running"],
        "total": _login_state["total"],
        "done": _login_state["done"],
        "progress": _login_state["progress"],
    }


@app.post("/api/fill-card")
async def fill_card(req: FillCardRequest):
    """在已打开的浏览器窗口中一键填写银行卡信息（支持扫描到的任意浏览器）"""
    if not req.card_number:
        return {"ok": False, "error": "请输入卡号"}

    # 收集目标浏览器
    targets = []  # [(label, browser_instance), ...]

    if req.task_id > 0:
        # task_id > 10000 表示是通过端口指定的（前端约定：port 作为 task_id）
        if req.task_id >= 9000:
            # 当作端口号处理
            port = req.task_id
            try:
                browser = connect_browser_by_port(port)
                targets.append((f"port:{port}", browser))
            except Exception as e:
                return {"ok": False, "error": f"连接端口 {port} 失败: {str(e)}"}
        else:
            # FingerprintReg 自己注册的浏览器
            with state.lock:
                browser = state.browsers.get(req.task_id)
            if not browser:
                return {"ok": False, "error": f"窗口 #{req.task_id} 不存在或已关闭"}
            targets.append((f"#{req.task_id}", browser))
    else:
        # task_id == 0：填所有扫描到的浏览器
        scanned = scan_debug_browsers()
        if not scanned:
                # 回退到 FingerprintReg 自己的浏览器
            with state.lock:
                if not state.browsers:
                    return {"ok": False, "error": "没有扫描到任何浏览器窗口"}
                for tid, br in state.browsers.items():
                    targets.append((f"#{tid}", br))
        else:
            logger.info(f"[FillCard] 准备连接 {len(scanned)} 个浏览器...")
            for info in scanned:
                port = info["port"]
                try:
                    logger.info(f"[FillCard] 正在连接端口 {port}...")
                    browser = connect_browser_by_port(port)
                    targets.append((f"port:{port}", browser))
                    logger.info(f"[FillCard] 端口 {port} 连接成功")
                except Exception as e:
                    logger.warning(f"[FillCard] 连接端口 {port} 失败: {e}")

    if not targets:
        return {"ok": False, "error": "没有可用的浏览器窗口"}

    def _do_fill(label_browser):
        label, browser = label_browser
        try:
            r = fill_card_info(
                browser=browser,
                card_number=req.card_number,
                expiry_date=req.expiry_date,
                cvv=req.cvv,
                card_name=req.card_name,
            )
            r["label"] = label
            return r
        except Exception as e:
            return {"label": label, "ok": False, "message": str(e)}

    results = _parallel_exec(targets, _do_fill)
    success_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success_count > 0,
        "message": f"成功 {success_count}/{len(results)} 个窗口",
        "results": results,
    }


def _parallel_exec(targets: list, func, max_workers: int = 5) -> list:
    """并发执行操作，targets 为 [(label, browser), ...]，func 接收 (label, browser) 元组"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=min(len(targets), max_workers)) as pool:
        futures = {pool.submit(func, t): t for t in targets}
        try:
            for f in as_completed(futures, timeout=120):
                try:
                    results.append(f.result())
                except Exception as e:
                    label = futures[f][0]
                    results.append({"label": label, "ok": False, "message": str(e)})
        except TimeoutError:
            # 部分任务超时，记录未完成的
            for f, t in futures.items():
                if not f.done():
                    results.append({"label": t[0], "ok": False, "message": "操作超时"})
    return results


def _collect_browser_targets(task_id: int) -> tuple[list, str | None]:
    """
    根据 task_id 收集目标浏览器列表。
    task_id=0 表示所有窗口，>=9000 当作端口号，其余当作 FingerprintReg task_id。
    返回 (targets, error)，targets 为 [(label, browser), ...]。
    """
    targets = []
    if task_id > 0:
        if task_id >= 9000:
            try:
                browser = connect_browser_by_port(task_id)
                targets.append((f"port:{task_id}", browser))
            except Exception as e:
                return [], f"连接端口 {task_id} 失败: {str(e)}"
        else:
            with state.lock:
                browser = state.browsers.get(task_id)
            if not browser:
                return [], f"窗口 #{task_id} 不存在或已关闭"
            targets.append((f"#{task_id}", browser))
    else:
        scanned = scan_debug_browsers()
        if not scanned:
            with state.lock:
                if not state.browsers:
                    return [], "没有扫描到任何浏览器窗口"
                for tid, br in state.browsers.items():
                    targets.append((f"#{tid}", br))
        else:
            for info in scanned:
                try:
                    browser = connect_browser_by_port(info["port"])
                    targets.append((f"port:{info['port']}", browser))
                except Exception as e:
                    logger.warning(f"连接端口 {info['port']} 失败: {e}")
    return targets, None


class CardActionRequest(BaseModel):
    task_id: int = 0  # 0 = 所有窗口


@app.post("/api/clear-card")
async def clear_card_api(req: CardActionRequest):
    """一键清空所有浏览器窗口的银行卡表单"""
    targets, error = _collect_browser_targets(req.task_id)
    if error:
        return {"ok": False, "error": error}
    if not targets:
        return {"ok": False, "error": "没有可用的浏览器窗口"}

    def _do_clear(label_browser):
        label, browser = label_browser
        try:
            r = clear_card_info(browser)
            r["label"] = label
            return r
        except Exception as e:
            return {"label": label, "ok": False, "message": str(e)}

    results = _parallel_exec(targets, _do_clear)
    success_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success_count > 0,
        "message": f"成功清空 {success_count}/{len(results)} 个窗口",
        "results": results,
    }


@app.post("/api/confirm-card")
async def confirm_card_api(req: CardActionRequest):
    """一键点击所有浏览器窗口的 Confirm 按钮"""
    targets, error = _collect_browser_targets(req.task_id)
    if error:
        return {"ok": False, "error": error}
    if not targets:
        return {"ok": False, "error": "没有可用的浏览器窗口"}

    def _do_confirm(label_browser):
        label, browser = label_browser
        try:
            r = confirm_card(browser)
            r["label"] = label
            return r
        except Exception as e:
            return {"label": label, "ok": False, "message": str(e)}

    results = _parallel_exec(targets, _do_confirm)
    success_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success_count > 0,
        "message": f"成功确认 {success_count}/{len(results)} 个窗口",
        "results": results,
    }


# ── Worker 逻辑 ──

def _make_status_callback(task_id: int, country: str = ""):
    def callback(status: TaskStatus):
        task_dict = {
            "task_id": status.task_id,
            "step": status.step,
            "step_label": status.step_label,
            "email": status.email,
            "password": status.password,
            "success": status.success,
            "error": status.error,
        }
        with state.lock:
            state.tasks[task_id] = task_dict
            if status.success is not None:
                state.results.append(task_dict)

                # 成功的持久化保存
                if status.success:
                    # 国家代码 → 国家名称
                    from .register import _get_country_name
                    country_name = _get_country_name(country) if country else ""
                    account = {
                        "id": len(state.history) + 1,
                        "email": status.email,
                        "password": status.password,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "country": country.upper() if country else "",
                        "country_name": country_name,
                    }
                    state.history.append(account)
                    _save_account(account)
                    logger.info(f"[Task {task_id}] 账号已保存: {status.email} ({country})")

        _broadcast_from_thread({
            "type": "task_update",
            "task": task_dict,
        })

    return callback


def _run_batch(count: int, password: str, first_name: str, last_name: str, browser: str = "chrome", country: str = "JP", incognito: bool = True, auto_select_country: bool = True, auto_click_add_card: bool = True, ai_captcha: bool = False, fullscreen: bool = False):
    try:
        with ThreadPoolExecutor(max_workers=count) as executor:
            futures = {}
            for i in range(1, count + 1):
                if not state.running:
                    break
                future = executor.submit(
                    _run_single_task, i, password, first_name, last_name, browser, country, incognito,
                    auto_select_country, auto_click_add_card, ai_captcha, fullscreen
                )
                futures[future] = i
                if i < count:
                    import time as _t
                    _t.sleep(config.DELAY_BROWSER_STAGGER)

            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    task_id = futures[future]
                    logger.error(f"[Task {task_id}] 未捕获异常: {e}")
    except Exception as e:
        logger.error(f"[Batch] 批量任务异常退出: {e}")
    finally:
        # 确保 running 状态一定被重置
        state.running = False
        _broadcast_from_thread({
            "type": "completed",
            "results": state.results,
            "history": state.history,
        })
        logger.info("批量注册全部完成")


def _run_single_task(task_id: int, password: str, first_name: str, last_name: str, browser: str = "chrome", country: str = "JP", incognito: bool = True, auto_select_country: bool = True, auto_click_add_card: bool = True, ai_captcha: bool = False, fullscreen: bool = False):
    callback = _make_status_callback(task_id, country=country)
    result = register_one(
        task_id=task_id,
        password=password,
        first_name=first_name,
        last_name=last_name,
        browser_type=browser,
        country=country,
        on_status=callback,
        cancel_check=lambda: not state.running,
        incognito=incognito,
        auto_select_country=auto_select_country,
        auto_click_add_card=auto_click_add_card,
        ai_captcha=ai_captcha,
        fullscreen=fullscreen,
    )
    # 保留浏览器实例引用（无论成功失败），供一键填卡使用或用户手动操作
    if result.browser:
        with state.lock:
            state.browsers[task_id] = result.browser
        if result.success:
            logger.info(f"[Task {task_id}] 浏览器实例已保留（注册成功，可用于填卡）")
        else:
            logger.info(f"[Task {task_id}] 浏览器实例已保留（注册失败，保留供检查）")


def _run_batch_force(start_id: int, count: int, password: str, first_name: str, last_name: str, browser: str = "chrome", country: str = "JP", incognito: bool = True, auto_select_country: bool = True, auto_click_add_card: bool = True, ai_captcha: bool = False, fullscreen: bool = False):
    """强制启动模式的批量任务：不清空已有任务，追加新任务"""
    try:
        with ThreadPoolExecutor(max_workers=count) as executor:
            futures = {}
            for i in range(count):
                tid = start_id + i
                if not state.running:
                    break
                future = executor.submit(
                    _run_single_task, tid, password, first_name, last_name, browser, country, incognito,
                    auto_select_country, auto_click_add_card, ai_captcha, fullscreen
                )
                futures[future] = tid
                if i < count - 1:
                    import time as _t
                    _t.sleep(config.DELAY_BROWSER_STAGGER)

            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    task_id = futures[future]
                    logger.error(f"[Task {task_id}] 未捕获异常: {e}")
    except Exception as e:
        logger.error(f"[BatchForce] 批量任务异常退出: {e}")
    finally:
        # 检查是否还有进行中的任务
        has_pending = any(
            t.get("success") is None and t.get("step", 0) > 0
            for t in state.tasks.values()
        )
        if not has_pending:
            state.running = False

        _broadcast_from_thread({
            "type": "completed",
            "results": state.results,
            "history": state.history,
        })
        logger.info(f"强制启动批次完成 (start_id={start_id}, count={count})")
