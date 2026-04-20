"""
JetBrainsReg Web 控制面板后端
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
                       cleanup_stale_data_dirs,
                       TaskStatus, AccountResult)

logger = logging.getLogger("jetbrainsreg.server")

app = FastAPI(title="JetBrainsReg", version="0.3.0")

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


@app.on_event("startup")
async def _on_startup():
    global _event_loop
    _event_loop = asyncio.get_event_loop()
    # 加载历史成功记录
    state.history = _load_history()
    logger.info(f"已加载 {len(state.history)} 条历史成功记录")
    # 注意：不再在启动时自动清理浏览器数据目录，避免误杀正在运行的浏览器
    # 清理操作改为用户通过「关闭所有浏览器」按钮手动触发


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
    browser: str = "chrome"  # chrome / edge / brave
    country: str = "JP"      # tokens 页选择的国家代码


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
    return HTMLResponse("<h1>JetBrainsReg</h1><p>static/index.html not found</p>")


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
        args=(req.count, req.password, req.first_name, req.last_name, req.browser, req.country),
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


@app.post("/api/stop")
async def stop_registration():
    state.running = False
    _broadcast_from_thread({"type": "stopped"})
    return {"ok": True}


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


@app.post("/api/kill-all-browsers")
async def kill_all_browsers():
    """一键关闭所有由 JetBrainsReg 管理的浏览器（扫描到的 debug 端口浏览器 + 注册保留的浏览器）"""
    killed = 0
    errors = []

    # 1. 关闭注册时保留的浏览器
    with state.lock:
        for task_id, browser in list(state.browsers.items()):
            try:
                browser.quit()
                killed += 1
                logger.info(f"[KillAll] 已关闭 JetBrainsReg #{task_id}")
            except Exception as e:
                errors.append(f"JetBrainsReg #{task_id}: {e}")
        state.browsers.clear()

    # 2. 关闭所有扫描到的 debug 端口浏览器
    scanned = scan_debug_browsers()
    for info in scanned:
        port = info["port"]
        try:
            browser = connect_browser_by_port(port)
            browser.quit()
            killed += 1
            logger.info(f"[KillAll] 已关闭端口 {port} 浏览器")
        except Exception as e:
            errors.append(f"port:{port}: {e}")

    # 3. 清理残余的浏览器数据目录
    try:
        cleanup_stale_data_dirs()
    except Exception as e:
        logger.warning(f"[KillAll] 清理数据目录失败: {e}")

    msg = f"已关闭 {killed} 个浏览器"
    if errors:
        msg += f"，{len(errors)} 个失败"

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
        args=(start_id, req.count, req.password, req.first_name, req.last_name, req.browser, req.country),
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

    # 2. 合并 JetBrainsReg 自己注册时保留的浏览器
    with state.lock:
        for task_id, browser in list(state.browsers.items()):
            try:
                tab = browser.latest_tab
                url = tab.url or ""
                scanned.append({
                    "pid": 0,
                    "port": 0,
                    "browser": "jetbrainsreg",
                    "title": f"JetBrainsReg #{task_id}",
                    "url": url,
                    "task_id": task_id,
                    "email": state.tasks.get(task_id, {}).get("email", ""),
                })
            except Exception:
                del state.browsers[task_id]

    return scanned


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
            # JetBrainsReg 自己注册的浏览器
            with state.lock:
                browser = state.browsers.get(req.task_id)
            if not browser:
                return {"ok": False, "error": f"窗口 #{req.task_id} 不存在或已关闭"}
            targets.append((f"#{req.task_id}", browser))
    else:
        # task_id == 0：填所有扫描到的浏览器
        scanned = scan_debug_browsers()
        if not scanned:
            # 回退到 JetBrainsReg 自己的浏览器
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
    task_id=0 表示所有窗口，>=9000 当作端口号，其余当作 JetBrainsReg task_id。
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

def _make_status_callback(task_id: int):
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
                    account = {
                        "id": len(state.history) + 1,
                        "email": status.email,
                        "password": status.password,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    state.history.append(account)
                    _save_account(account)
                    logger.info(f"[Task {task_id}] 账号已保存: {status.email}")

        _broadcast_from_thread({
            "type": "task_update",
            "task": task_dict,
        })

    return callback


def _run_batch(count: int, password: str, first_name: str, last_name: str, browser: str = "chrome", country: str = "JP"):
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = {}
        for i in range(1, count + 1):
            if not state.running:
                break
            future = executor.submit(
                _run_single_task, i, password, first_name, last_name, browser, country
            )
            futures[future] = i
            # 错开浏览器启动时间，避免同时启动导致资源争抢和连接超时
            if i < count:
                import time as _t
                _t.sleep(config.DELAY_BROWSER_STAGGER)

        for future in futures:
            try:
                future.result()
            except Exception as e:
                task_id = futures[future]
                logger.error(f"[Task {task_id}] 未捕获异常: {e}")

    state.running = False

    _broadcast_from_thread({
        "type": "completed",
        "results": state.results,
        "history": state.history,
    })

    logger.info("批量注册全部完成")


def _run_single_task(task_id: int, password: str, first_name: str, last_name: str, browser: str = "chrome", country: str = "JP"):
    callback = _make_status_callback(task_id)
    result = register_one(
        task_id=task_id,
        password=password,
        first_name=first_name,
        last_name=last_name,
        browser_type=browser,
        country=country,
        on_status=callback,
        cancel_check=lambda: not state.running,
    )
    # 保留浏览器实例引用（无论成功失败），供一键填卡使用或用户手动操作
    if result.browser:
        with state.lock:
            state.browsers[task_id] = result.browser
        if result.success:
            logger.info(f"[Task {task_id}] 浏览器实例已保留（注册成功，可用于填卡）")
        else:
            logger.info(f"[Task {task_id}] 浏览器实例已保留（注册失败，保留供检查）")


def _run_batch_force(start_id: int, count: int, password: str, first_name: str, last_name: str, browser: str = "chrome", country: str = "JP"):
    """强制启动模式的批量任务：不清空已有任务，追加新任务"""
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = {}
        for i in range(count):
            tid = start_id + i
            if not state.running:
                break
            future = executor.submit(
                _run_single_task, tid, password, first_name, last_name, browser, country
            )
            futures[future] = tid
            # 错开浏览器启动时间
            if i < count - 1:
                import time as _t
                _t.sleep(config.DELAY_BROWSER_STAGGER)

        for future in futures:
            try:
                future.result()
            except Exception as e:
                task_id = futures[future]
                logger.error(f"[Task {task_id}] 未捕获异常: {e}")

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
