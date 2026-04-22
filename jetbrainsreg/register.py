"""
FingerprintReg 全自动注册流程（v2 — 集成指纹 + 全自动化）
DrissionPage 控制浏览器完成 9 步：
  1. Accept All (Cookie 弹窗) — 三重 Cookie 守护
  2. Continue with email
  3. 填邮箱 → Continue（Enter 键 + 按钮双保险）
  4. reCAPTCHA checkbox ("I'm not a robot")
  5. ★ 等待用户手动完成图片验证 ★（自动检测完成）
  5b. 自动点击 Continue 提交表单（多策略强化）
  6. 6 位邮箱验证码（支持 OTP 码 + 验证链接双模式）
  7. First name + Last name + Password + 自动勾选协议 → Create account
  8. 自动跳转 tokens 页 → 选日本 → 弹出 Add credit card
"""
import logging
import os
import random
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from DrissionPage import Chromium, ChromiumOptions

from . import config
from . import email_service
from . import captcha_solver
from . import captcha_service

logger = logging.getLogger("jetbrainsreg.register")


# ── 随机真人英文名（移植自 批量注册JB保留窗口.py） ──

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Barbara", "William", "Elizabeth", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Christopher", "Nancy", "Daniel", "Lisa",
    "Matthew", "Margaret", "Anthony", "Betty", "Donald", "Sandra", "Mark", "Ashley",
    "Paul", "Dorothy", "Steven", "Kimberly", "Andrew", "Emily", "Kenneth", "Donna",
    "Joshua", "Michelle", "Kevin", "Carol", "Brian", "Amanda", "George", "Melissa",
    "Edward", "Deborah", "Ronald", "Stephanie", "Timothy", "Rebecca", "Jason", "Laura",
    "Jeffrey", "Helen", "Ryan", "Sharon", "Jacob", "Cynthia", "Gary", "Kathleen",
    "Nicholas", "Amy", "Eric", "Shirley", "Jonathan", "Angela", "Stephen", "Anna",
    "Larry", "Ruth", "Justin", "Brenda", "Scott", "Pamela", "Brandon", "Nicole",
    "Frank", "Katherine", "Benjamin", "Virginia", "Gregory", "Catherine", "Samuel",
    "Christine", "Raymond", "Samantha", "Patrick", "Debra", "Alexander", "Janet",
    "Jack", "Rachel", "Dennis", "Carolyn", "Jerry", "Emma", "Tyler", "Maria",
    "Aaron", "Heather", "Henry", "Diane", "Douglas", "Julie", "Adam", "Joyce",
    "Peter", "Victoria", "Nathan", "Kelly", "Zachary", "Christina", "Walter", "Joan",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill",
    "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
    "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz",
    "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales",
    "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson",
    "Bailey", "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward",
    "Richardson", "Watson", "Brooks", "Chavez", "Wood", "James", "Bennett", "Gray",
    "Mendoza", "Ruiz", "Hughes", "Price", "Alvarez", "Castillo", "Sanders", "Patel",
    "Myers", "Long", "Ross", "Foster", "Jimenez", "Powell", "Jenkins", "Perry",
]

TOKENS_URL = "https://account.jetbrains.com/licenses/tokens"


_name_rng_lock = threading.Lock()
_name_rng = random.Random()  # 独立的 Random 实例，避免线程竞争


def _random_name() -> tuple[str, str]:
    """线程安全的随机名字生成（每次调用重新 seed 以确保唯一性）"""
    with _name_rng_lock:
        _name_rng.seed(os.urandom(8))
        return _name_rng.choice(FIRST_NAMES), _name_rng.choice(LAST_NAMES)


# ── 数据结构 ──

@dataclass
class AccountResult:
    """注册结果"""
    email: str
    password: str
    success: bool
    error: str = ""
    browser: object = None  # 成功时保留浏览器实例引用（Chromium）


@dataclass
class TaskStatus:
    """单个注册任务的实时状态"""
    task_id: int = 0
    step: int = 0          # 当前步骤 1-9, 0=未开始
    step_label: str = ""   # 当前步骤描述
    email: str = ""
    password: str = ""
    success: Optional[bool] = None   # None=进行中, True/False=完成
    error: str = ""


# 状态回调类型: (task_id, status) → void
StatusCallback = Callable[[TaskStatus], None]


def _noop_callback(status: TaskStatus):
    pass


# ── 指纹参数生成（移植自 批量注册JB保留窗口.py） ──

def _make_fp_args(seed: int) -> tuple[list[str], dict]:
    """
    根据种子生成 fingerprint-chromium 的启动参数和指纹信息摘要。
    返回 (args_list, fp_info_dict)。
    支持通过 config.FINGERPRINT_TOGGLES 控制各项指纹特性的开关。
    """
    toggles = config.FINGERPRINT_TOGGLES
    rnd = random.Random(seed)

    # 读取各项参数候选值
    plat, plat_ver = rnd.choice(config.FINGERPRINT_PLATFORMS)
    brand, brand_ver = rnd.choice(config.FINGERPRINT_BRANDS)
    tz = rnd.choice(config.FINGERPRINT_TIMEZONES)
    cpu = rnd.choice(config.FINGERPRINT_CPU_CORES)
    lang, accept_lang = rnd.choice(config.FINGERPRINT_LANGUAGES)
    memory = rnd.choice(config.FINGERPRINT_MEMORY_SIZES)

    args = []
    fp_info = {"seed": seed}

    # ── 核心指纹种子（总开关） ──
    if toggles.get("fp_enabled", True):
        args.append(f"--fingerprint={seed}")

    # ── 操作系统平台 ──
    if toggles.get("fp_platform", True):
        args.append(f"--fingerprint-platform={plat}")
        if plat_ver:
            args.append(f"--fingerprint-platform-version={plat_ver}")
        fp_info["platform"] = plat
    else:
        fp_info["platform"] = "(native)"

    # ── 浏览器品牌 (User-Agent / UA Data) ──
    if toggles.get("fp_brand", True):
        args.append(f"--fingerprint-brand={brand}")
        if brand_ver:
            args.append(f"--fingerprint-brand-version={brand_ver}")
        fp_info["brand"] = brand
    else:
        fp_info["brand"] = "(native)"

    # ── CPU 核心数 ──
    if toggles.get("fp_cpu", True):
        args.append(f"--fingerprint-hardware-concurrency={cpu}")
        fp_info["cpu"] = cpu
    else:
        fp_info["cpu"] = "(native)"

    # ── 时区 ──
    if toggles.get("fp_timezone", True):
        args.append(f"--timezone={tz}")
        fp_info["timezone"] = tz
    else:
        fp_info["timezone"] = "(native)"

    # ── 语言 ──
    if toggles.get("fp_language", True):
        args.append(f"--lang={lang}")
        args.append(f"--accept-lang={accept_lang}")
        fp_info["language"] = lang
    else:
        # 默认英文，防止 JetBrains 页面变成其他语言
        args.append("--lang=en-US")
        args.append("--accept-lang=en-US,en")
        fp_info["language"] = "en-US"

    # ── 内存大小 (navigator.deviceMemory) ──
    if toggles.get("fp_memory", True):
        # fingerprint-chromium 通过 --fingerprint 种子自动生成内存值
        # 无需额外参数，但记录到 fp_info 供日志
        fp_info["memory"] = f"{memory}GB"
    else:
        fp_info["memory"] = "(native)"

    # ── WebRTC 策略 ──
    if toggles.get("fp_webrtc", True):
        args.append("--disable-non-proxied-udp")
        fp_info["webrtc"] = "disabled-non-proxied-udp"
    else:
        fp_info["webrtc"] = "(native)"

    # ── 收集需要 disable 的指纹伪装模块 (--disable-spoofing) ──
    disable_spoofing = []
    if not toggles.get("fp_canvas", True):
        disable_spoofing.append("canvas")
    if not toggles.get("fp_audio", True):
        disable_spoofing.append("audio")
    if not toggles.get("fp_font", True):
        disable_spoofing.append("font")
    if not toggles.get("fp_clientrects", True):
        disable_spoofing.append("clientrects")
    if not toggles.get("fp_gpu", True):
        disable_spoofing.append("gpu")

    if disable_spoofing:
        args.append(f"--disable-spoofing={','.join(disable_spoofing)}")
        fp_info["disabled_spoofing"] = disable_spoofing

    # ── 反自动化检测 ──
    if toggles.get("fp_automation", True):
        args.append("--disable-blink-features=AutomationControlled")
        args.append("--test-type")

    # ── Webdriver 隐藏 ──
    if toggles.get("fp_webdriver", True):
        # fingerprint-chromium 默认已设 navigator.webdriver=false
        # --disable-blink-features=AutomationControlled 也有同样效果
        fp_info["webdriver"] = "hidden"
    else:
        fp_info["webdriver"] = "(native)"

    # ── 通用浏览器参数（不受开关影响） ──
    args.extend([
        "--disable-infobars",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
        "--disable-session-crashed-bubble",
        "--disable-save-password-bubble",
    ])

    return args, fp_info


def _is_fingerprint_enabled() -> bool:
    """检查指纹浏览器是否可用"""
    path = getattr(config, "FINGERPRINT_BROWSER_PATH", None)
    return bool(path) and os.path.isfile(path)


# ── 浏览器创建 + 清理 ──

_PROJECT_DIR = Path(__file__).parent.parent
_BROWSER_DATA_DIR = _PROJECT_DIR / "browser_data"
_BROWSER_DATA_DIR.mkdir(exist_ok=True)

_next_port = 0  # 0 = 尚未初始化，首次使用时自动扫描
_port_lock = threading.Lock()


def _init_port_range():
    """扫描系统中已使用的端口和残留的数据目录，确定安全的起始端口"""
    global _next_port
    max_used = 9599  # 基线

    # 1. 扫描 browser_data 目录中的端口号（避免与残留目录冲突）
    if _BROWSER_DATA_DIR.exists():
        for sub in _BROWSER_DATA_DIR.iterdir():
            if not sub.is_dir():
                continue
            name = sub.name
            port = None
            if name.isdigit():
                port = int(name)
            elif name.startswith("fp_"):
                parts = name.split("_")
                if len(parts) >= 2 and parts[1].isdigit():
                    port = int(parts[1])
            if port and port > max_used:
                max_used = port

    # 2. 扫描已运行的浏览器进程的端口（避免连接到别人的浏览器）
    try:
        import subprocess
        ps_script = (
            'Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\' or name=\'msedge.exe\'" '
            '| Select-Object CommandLine '
            '| ForEach-Object { $_.CommandLine }'
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            timeout=10, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="ignore")
        for line in raw.splitlines():
            m = re.search(r'--remote-debugging-port=(\d+)', line)
            if m:
                p = int(m.group(1))
                if p > max_used:
                    max_used = p
    except Exception:
        pass

    _next_port = max_used + 1
    logger.info(f"[Port] 端口分配起始值: {_next_port}")


def _alloc_port() -> int:
    global _next_port
    with _port_lock:
        if _next_port == 0:
            _init_port_range()
        port = _next_port
        _next_port += 1
    return port


def reset_port_counter():
    """重置端口计数器，下次分配时重新扫描系统端口。
    应在 KillAll 浏览器后调用，确保新批次不会与已杀死的旧端口冲突。
    """
    global _next_port
    with _port_lock:
        _next_port = 0
    logger.info("[Port] 端口计数器已重置，下次分配时重新扫描")


def _cleanup_data_dir(data_dir: Path | None):
    """安全删除浏览器数据目录"""
    if data_dir and data_dir.exists() and data_dir != _BROWSER_DATA_DIR:
        try:
            shutil.rmtree(data_dir, ignore_errors=True)
            logger.info(f"[Cleanup] 已删除 {data_dir.name}")
        except Exception as e:
            logger.warning(f"[Cleanup] 删除 {data_dir} 失败: {e}")


def _close_browser_and_cleanup(browser, data_dir: Path | None = None):
    """关闭浏览器并清理其数据目录（仅在明确需要时调用，如用户手动关闭）"""
    if browser:
        try:
            browser.quit()
        except Exception:
            pass
    _cleanup_data_dir(data_dir)


def _kill_browser_on_port(port: int):
    """强杀监听指定 debug 端口的浏览器进程，防止僵尸进程"""
    import subprocess
    try:
        ps_script = (
            f'Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\' or name=\'msedge.exe\'" '
            f'| Where-Object {{ $_.CommandLine -match \'--remote-debugging-port={port}\' }} '
            f'| Select-Object -ExpandProperty ProcessId'
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            timeout=8, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="ignore")
        for line in raw.strip().splitlines():
            pid = line.strip()
            if pid.isdigit():
                subprocess.run(["taskkill", "/F", "/PID", pid], timeout=5, capture_output=True)
                logger.info(f"[Browser] 强杀端口 {port} 的残留进程 PID={pid}")
    except Exception as e:
        logger.debug(f"[Browser] 清理端口 {port} 进程失败: {e}")


def _safe_browser_check(browser) -> bool:
    """安全检测浏览器是否仍然存活（不会导致浏览器被杀）"""
    if not browser:
        return False
    try:
        _ = browser.latest_tab.url
        return True
    except Exception:
        return False


def _safe_ele(tab, selector: str, timeout: float = 5):
    """安全查找元素，超时或异常返回 None 而非抛异常"""
    try:
        return tab.ele(selector, timeout=timeout)
    except Exception:
        return None


def _safe_run_js(tab, script: str, default=None):
    """安全执行 JS，异常返回 default"""
    try:
        return tab.run_js(script)
    except Exception:
        return default


def _safe_get(tab, url: str, timeout: float = 30, retries: int = 2) -> bool:
    """安全导航到 URL，超时/异常返回 False 但不抛异常。
    对连接断开等临时错误支持自动重试。
    """
    for attempt in range(1, retries + 1):
        try:
            tab.get(url)
            tab.wait.doc_loaded(timeout=timeout)
            return True
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"[SafeGet] 导航到 {url[:60]} 失败 (attempt {attempt}/{retries}): {e}")
            # 如果是连接断开，等一下再重试（浏览器可能还在启动中）
            if attempt < retries and ("连接已断开" in err_msg or "disconnected" in err_msg.lower()):
                time.sleep(3)
                continue
            return False
    return False


def cleanup_stale_data_dirs():
    """
    启动时清理孤立的 browser_data 子目录。
    扫描 browser_data/ 下所有子目录，检查其对应端口是否仍有浏览器进程在运行，
    如果没有则删除。
    """
    if not _BROWSER_DATA_DIR.exists():
        return
    active_ports = set()
    try:
        browsers = scan_debug_browsers()
        active_ports = {b["port"] for b in browsers}
    except Exception:
        return  # 扫描失败就不清理，避免误删

    cleaned = 0
    for sub in _BROWSER_DATA_DIR.iterdir():
        if not sub.is_dir():
            continue
        # 从目录名提取端口号：普通模式 "9600"，指纹模式 "fp_9600_123456"
        name = sub.name
        port = None
        if name.isdigit():
            port = int(name)
        elif name.startswith("fp_"):
            parts = name.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                port = int(parts[1])

        if port is not None and port not in active_ports:
            _cleanup_data_dir(sub)
            cleaned += 1

    if cleaned:
        logger.info(f"[Cleanup] 启动清理：删除了 {cleaned} 个孤立的浏览器数据目录")


def _find_browser_path(browser_type: str) -> str | None:
    """查找浏览器可执行文件路径"""
    candidates = {
        "chrome": [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ],
        "edge": [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ],
        "brave": [
            os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ],
    }
    for path in candidates.get(browser_type, []):
        if os.path.isfile(path):
            return path
    return None


def _create_browser(browser_type: str = "chrome", fp_seed: int | None = None, max_retries: int = 3, incognito: bool = True, fullscreen: bool = False) -> tuple:
    """
    创建浏览器实例（带重试，防止指纹浏览器启动慢导致连接超时）。
    返回 (Chromium实例, fp_info字典或None, data_dir路径)
    """
    port = _alloc_port()

    fp_info = None
    data_dir = None

    for attempt in range(1, max_retries + 1):
        co = ChromiumOptions()
        co.set_local_port(port)

        # 当 browser_type 为 "fingerprint" 时，强制使用指纹模式
        use_fingerprint = (fp_seed is not None and _is_fingerprint_enabled()) or browser_type == "fingerprint"
        if use_fingerprint and fp_seed is None:
            fp_seed = random.randint(10_000_000, 2_000_000_000)

        if use_fingerprint and _is_fingerprint_enabled():
            # ── 指纹模式 ──
            co.set_browser_path(config.FINGERPRINT_BROWSER_PATH)
            data_dir = _BROWSER_DATA_DIR / f"fp_{port}_{fp_seed}"
            data_dir.mkdir(parents=True, exist_ok=True)
            co.set_user_data_path(str(data_dir))

            fp_args, fp_info = _make_fp_args(fp_seed)
            for arg in fp_args:
                co.set_argument(arg)

            if attempt == 1:
                logger.info(f"[Browser] 指纹模式 seed={fp_seed} "
                             f"{fp_info['platform']}/{fp_info['brand']}/{fp_info['timezone']}")
        else:
            # ── 普通模式 ──
            data_dir = _BROWSER_DATA_DIR / str(port)
            data_dir.mkdir(parents=True, exist_ok=True)
            co.set_user_data_path(str(data_dir))
            co.set_argument("--disable-blink-features=AutomationControlled")
            co.set_argument("--no-first-run")
            co.set_argument("--no-default-browser-check")
            co.set_argument("--lang=en-US")

            if browser_type != "chrome":
                path = _find_browser_path(browser_type)
                if path:
                    co.set_browser_path(path)
                    if attempt == 1:
                        logger.info(f"[Browser] 使用 {browser_type}: {path}")
                else:
                    if attempt == 1:
                        logger.warning(f"[Browser] 未找到 {browser_type}，使用默认 Chrome")

        if incognito:
            co.incognito()
        co.set_argument("--disable-popup-blocking")
        if fullscreen:
            co.set_argument("--start-maximized")

        try:
            browser = Chromium(co)
            logger.info(f"[Browser] 端口 {port} 浏览器启动成功 (attempt {attempt})")
            return browser, fp_info, data_dir
        except Exception as e:
            logger.warning(f"[Browser] 端口 {port} 启动失败 (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                wait_sec = 5 * attempt
                logger.info(f"[Browser] 等待 {wait_sec}s 后重试...")
                time.sleep(wait_sec)

                # 重试：直接连接已启动的浏览器（进程可能已在后台就绪）
                try:
                    browser = Chromium(f"127.0.0.1:{port}")
                    logger.info(f"[Browser] 端口 {port} 重连成功")
                    return browser, fp_info, data_dir
                except Exception:
                    logger.debug(f"[Browser] 端口 {port} 重连也失败")
                    # 强杀残留进程（防止后台僵尸进程打开空白窗口）
                    _kill_browser_on_port(port)
                    # 不换端口，不删数据目录，用同一个端口重试
            else:
                _kill_browser_on_port(port)
                raise


# ═══════════════════════════════════════════════════════════
#  Cookie 三重守护（移植自 批量注册JB保留窗口.py）
# ═══════════════════════════════════════════════════════════

# JS 注入脚本：在每个页面自动处理 Cookie 弹窗
COOKIE_KILLER_JS = """
(function() {
    if (window.__cookieKillerInit) return;
    window.__cookieKillerInit = true;
    const tryKill = () => {
        try {
            if (window.cookiehub && typeof window.cookiehub.allow === 'function') window.cookiehub.allow();
            const btn = document.querySelector('button.ch2-allow-all-btn, .ch2-btn-primary');
            if (btn) btn.click();
            document.querySelectorAll('button').forEach(b => {
                const t = (b.textContent||'').trim().toLowerCase();
                if (t === 'accept all' || t === 'accept all cookies') b.click();
            });
        } catch(e) {}
    };
    const activate = () => {
        tryKill();
        try { const obs = new MutationObserver(() => tryKill());
              obs.observe(document.documentElement, {childList:true, subtree:true}); } catch(e) {}
        let c=0; const iv=setInterval(()=>{tryKill();c++;if(c>120)clearInterval(iv);},500);
    };
    if (document.readyState==='loading') document.addEventListener('DOMContentLoaded',activate);
    else activate();
})();
"""


def _inject_cookie_killer(tab):
    """注入 Cookie 自动处理脚本"""
    try:
        tab.run_js(COOKIE_KILLER_JS)
    except Exception:
        pass


def _dismiss_cookie_banner(tab) -> bool:
    """主动尝试关闭 Cookie 弹窗（多种策略）"""
    # 策略1：调用 cookiehub JS API
    try:
        result = tab.run_js("""
            try { if (window.cookiehub && typeof window.cookiehub.allow==='function'){window.cookiehub.allow();return 'api';}} catch(e){}
            const b=document.querySelector('button.ch2-allow-all-btn,.ch2-btn-primary');if(b){b.click();return 'dom';}
            for(const x of document.querySelectorAll('button')){const t=(x.textContent||'').trim().toLowerCase();
            if(t==='accept all'){x.click();return 'txt';}} return null;
        """)
        if result:
            return True
    except Exception:
        pass

    # 策略2：DrissionPage 文本查找
    try:
        btn = tab.ele("text:Accept All", timeout=2)
        if btn:
            btn.click()
            return True
    except Exception:
        pass

    return False


# ═══════════════════════════════════════════════════════════
#  Step 1: Cookie 弹窗（强化版）
# ═══════════════════════════════════════════════════════════

def _handle_cookie_consent(tab) -> bool:
    logger.info("[Step 1] 处理 Cookie 弹窗...")
    _inject_cookie_killer(tab)
    for _ in range(3):
        if _dismiss_cookie_banner(tab):
            logger.info("[Step 1] 已处理 Cookie 弹窗")
            time.sleep(0.5)
            return True
        time.sleep(0.8)
    logger.info("[Step 1] 未发现 Cookie 弹窗或已处理，继续")
    return True


# ═══════════════════════════════════════════════════════════
#  Step 2: Continue with email
# ═══════════════════════════════════════════════════════════

def _click_continue_with_email(tab) -> bool:
    logger.info("[Step 2] 点击 Continue with email...")
    try:
        tab.wait.doc_loaded(timeout=config.PAGE_TIMEOUT)
    except Exception:
        pass

    # 先检查浏览器连接是否正常
    try:
        _ = tab.url
    except Exception as e:
        logger.error(f"[Step 2] 浏览器连接已断开: {e}")
        return False

    try:
        btn = tab.ele("text:Continue with email", timeout=config.PAGE_TIMEOUT)
        if not btn:
            logger.warning("[Step 2] 未找到按钮，可能已在邮箱输入页")
            return True
        btn.click()
        time.sleep(config.DELAY_CLICK)
        logger.info("[Step 2] 已点击 Continue with email")
    except Exception as e:
        err_msg = str(e)
        if "连接已断开" in err_msg or "disconnected" in err_msg.lower() or "no browser" in err_msg.lower():
            logger.error(f"[Step 2] 浏览器连接已断开: {e}")
            return False
        logger.warning(f"[Step 2] 异常（尝试继续）: {e}")
    return True


# ═══════════════════════════════════════════════════════════
#  Step 3: 填写邮箱（强化：Enter 键优先 + 重试）
# ═══════════════════════════════════════════════════════════

def _fill_email(tab, email: str) -> bool:
    """填写邮箱并点击 Continue（触发 reCAPTCHA 加载）"""
    logger.info(f"[Step 3] 填写邮箱: {email}")

    # 先检查浏览器连接是否正常
    try:
        _ = tab.url
    except Exception as e:
        logger.error(f"[Step 3] 浏览器连接已断开，无法填写邮箱: {e}")
        return False

    try:
        tab.wait.doc_loaded(timeout=15)
    except Exception:
        pass

    email_input = None
    for selector in ["@name=email", "@placeholder=Email", "@type=email", "tag:input"]:
        try:
            email_input = tab.ele(selector, timeout=8)
            if email_input:
                break
        except Exception as e:
            err_msg = str(e)
            if "连接已断开" in err_msg or "disconnected" in err_msg.lower():
                logger.error(f"[Step 3] 浏览器连接已断开: {e}")
                return False

    if not email_input:
        logger.error("[Step 3] 未找到邮箱输入框")
        return False

    try:
        email_input.clear()
        email_input.input(email)
        time.sleep(config.DELAY_INPUT)
    except Exception as e:
        logger.error(f"[Step 3] 输入邮箱失败: {e}")
        return False

    # 点击 Continue 触发 reCAPTCHA（多策略 + 验证是否生效）
    for click_try in range(3):
        # JS 直接点击（最可靠，绕过任何视觉遮挡）
        try:
            tab.run_js("""
                const btn = document.querySelector('button[type="submit"]');
                if (btn) btn.click();
            """)
            logger.info(f"[Step 3] JS 点击 Continue（第 {click_try + 1} 次）")
        except Exception:
            # 降级到 DrissionPage 点击
            try:
                submit_btn = tab.ele("@type=submit", timeout=3)
                if submit_btn:
                    submit_btn.click()
                    logger.info("[Step 3] DrissionPage 点击 Continue")
            except Exception:
                pass

        # 等待 dialog[open] 出现（reCAPTCHA 容器）或页面跳转
        for wait_i in range(8):
            time.sleep(0.8)
            try:
                state = tab.run_js("""
                    if (document.querySelector('dialog[open]')) return 'dialog';
                    if (document.querySelector('input[name="otp-1"]')
                        || document.querySelector('input[type="password"]')
                        || document.querySelectorAll('input[maxlength="1"]').length >= 4) return 'next_page';
                    return '';
                """)
                if state == 'dialog':
                    logger.info("[Step 3] reCAPTCHA dialog 已出现")
                    return True
                if state == 'next_page':
                    logger.info("[Step 3] 已直接跳到下一步（无 reCAPTCHA）")
                    return True
            except Exception:
                pass

        logger.warning(f"[Step 3] 第 {click_try + 1} 次点击后 dialog 未出现，重试...")

    time.sleep(2)
    return True


# ═══════════════════════════════════════════════════════════
#  Step 4: reCAPTCHA checkbox
# ═══════════════════════════════════════════════════════════

def _click_recaptcha_checkbox(tab) -> bool:
    """点击 'I'm not a robot' checkbox，含页面刷新等待和重试"""
    logger.info("[Step 4] 等待 reCAPTCHA 加载...")

    # 检查 dialog 是否已经出现（Step 3 触发的）
    try:
        has_dialog = tab.run_js("return !!document.querySelector('dialog[open]')")
        if has_dialog:
            logger.info("[Step 4] dialog 已存在，跳过等待")
        else:
            try:
                tab.wait.doc_loaded(timeout=config.PAGE_TIMEOUT)
            except Exception:
                pass
            time.sleep(3)
    except Exception:
        time.sleep(3)

    # 处理 Cookie 弹窗
    _inject_cookie_killer(tab)
    _dismiss_cookie_banner(tab)

    # 最多尝试 2 轮（第 1 轮正常查找，第 2 轮关闭 dialog 重新触发）
    for big_round in range(2):
        anchor_frame = None
        search_attempts = 4 if big_round == 0 else 3

        for attempt in range(1, search_attempts + 1):
            logger.info(f"[Step 4] 查找 reCAPTCHA iframe（轮次 {big_round+1}，第 {attempt}/{search_attempts} 次）...")
            try:
                anchor_frame = tab.get_frame("@title=reCAPTCHA", timeout=8)
            except Exception:
                pass

            if not anchor_frame:
                try:
                    iframes = tab.eles("tag:iframe")
                    for ifr in iframes:
                        src = (ifr.attr("src") or "").lower()
                        title = (ifr.attr("title") or "").lower()
                        if ("recaptcha" in src and "anchor" in src) or title == "recaptcha":
                            try:
                                anchor_frame = tab.get_frame(ifr)
                                break
                            except Exception:
                                pass
                except Exception:
                    pass

            if anchor_frame:
                _mark_recaptcha_seen()
                break

            if _captcha_is_done(tab):
                logger.info("[Step 4] 等待中检测到页面已跳过 reCAPTCHA")
                return True

            # 检查 dialog 是否打开但内部为空（reCAPTCHA 加载失败）
            try:
                dialog_empty = tab.run_js("""
                    const d = document.querySelector('dialog[open]');
                    if (!d) return false;
                    const inner = d.innerHTML;
                    return !inner.includes('recaptcha') && !inner.includes('iframe');
                """)
                if dialog_empty and attempt >= 2:
                    logger.warning(f"[Step 4] dialog 已打开但 reCAPTCHA 未加载（空 dialog），将关闭重试")
                    break  # 跳出内循环，进入 big_round 重试
            except Exception:
                pass

            logger.warning(f"[Step 4] 第 {attempt} 次未找到 iframe，等待...")
            time.sleep(2.5)

        if anchor_frame:
            break  # 找到了，跳出大循环

        # 没找到 iframe，尝试关闭 dialog 并重新点击 Continue 触发 reCAPTCHA 重新加载
        if big_round == 0:
            logger.info("[Step 4] reCAPTCHA 未加载，关闭 dialog 重新触发...")
            try:
                # 关闭 dialog
                tab.run_js("""
                    const d = document.querySelector('dialog[open]');
                    if (d) { d.close(); d.remove(); }
                """)
                time.sleep(1)
                # 重新点击 Continue
                tab.run_js("""
                    const btn = document.querySelector('button[type="submit"]');
                    if (btn) btn.click();
                """)
                time.sleep(3)
            except Exception as e:
                logger.warning(f"[Step 4] 重新触发失败: {e}")

    if not anchor_frame:
        logger.warning("[Step 4] 未找到 reCAPTCHA iframe，可能 Continue 未生效，将尝试重新提交")
        return True  # 返回 True 让流程继续

    checkbox = None
    for cb_try in range(1, 4):
        try:
            checkbox = anchor_frame.ele("#recaptcha-anchor", timeout=10)
        except Exception:
            pass
        if not checkbox:
            try:
                checkbox = anchor_frame.ele(".recaptcha-checkbox", timeout=5)
            except Exception:
                pass
        if checkbox:
            break
        logger.warning(f"[Step 4] checkbox 第 {cb_try} 次未找到，等 3 秒...")
        time.sleep(3)

    if not checkbox:
        logger.error("[Step 4] 多次重试后仍未找到 checkbox")
        return False

    checkbox.click()
    logger.info("[Step 4] 已点击 'I'm not a robot'")
    time.sleep(3)

    if _captcha_is_done(tab):
        logger.info("[Step 4] reCAPTCHA 直接通过（无图片验证）")
        return True

    return True


# ═══════════════════════════════════════════════════════════
#  Step 5: 等待用户手动完成验证码 (半自动核心)
# ═══════════════════════════════════════════════════════════

def _wait_for_manual_captcha(tab, cancel_flag: Callable[[], bool] | None = None) -> bool:
    """
    等待用户手动完成 reCAPTCHA 图片验证，无时限。
    cancel_flag: 可选的取消检查函数，返回 True 时退出等待。
    如果检测到 dialog 打开但 reCAPTCHA 未加载（空 dialog），自动尝试刷新。
    """
    if _captcha_is_done(tab):
        logger.info("[Step 5] reCAPTCHA 已通过（无需手动操作）")
        return True

    # 先检查 reCAPTCHA 是否真的存在（dialog 打开但内容为空 = 加载失败）
    try:
        has_recaptcha = tab.run_js("""
            const iframes = document.querySelectorAll('iframe');
            for (const f of iframes) {
                if ((f.src || '').includes('recaptcha') || (f.title || '').toLowerCase().includes('recaptcha'))
                    return true;
            }
            return false;
        """)
        if not has_recaptcha:
            logger.warning("[Step 5] 未检测到 reCAPTCHA iframe，尝试关闭 dialog 重新触发...")
            # 尝试修复：关闭空 dialog，重新点击 Continue
            for fix_try in range(3):
                try:
                    tab.run_js("""
                        const d = document.querySelector('dialog[open]');
                        if (d) { d.close(); d.remove(); }
                    """)
                    time.sleep(1)
                    tab.run_js("document.querySelector('button[type=\"submit\"]')?.click()")
                    time.sleep(4)
                    # 检查 reCAPTCHA 是否加载了
                    loaded = tab.run_js("""
                        const iframes = document.querySelectorAll('iframe');
                        for (const f of iframes) {
                            if ((f.src || '').includes('recaptcha')) return true;
                        }
                        return false;
                    """)
                    if loaded:
                        logger.info(f"[Step 5] 第 {fix_try+1} 次重试后 reCAPTCHA 加载成功")
                        # 自动点击 "I'm not a robot"
                        try:
                            anchor_frame = tab.get_frame("@title=reCAPTCHA", timeout=5)
                            if anchor_frame:
                                cb = anchor_frame.ele("#recaptcha-anchor", timeout=5) or anchor_frame.ele(".recaptcha-checkbox", timeout=3)
                                if cb:
                                    cb.click()
                                    logger.info("[Step 5] 已自动点击 'I'm not a robot'")
                                    time.sleep(2)
                        except Exception:
                            pass
                        break
                    if _has_left_email_page(tab):
                        logger.info("[Step 5] 页面已跳转，无需验证码")
                        return True
                except Exception as e:
                    logger.debug(f"[Step 5] 修复尝试 {fix_try+1} 失败: {e}")
                    time.sleep(2)
    except Exception:
        pass

    if _captcha_is_done(tab):
        logger.info("[Step 5] reCAPTCHA 已通过")
        return True

    logger.info("[Step 5] 请在浏览器中手动完成验证码（无时限，慢慢来）...")
    poll_count = 0
    reload_attempted = False

    while True:
        poll_count += 1
        if cancel_flag and cancel_flag():
            logger.info("[Step 5] 收到取消信号，停止等待")
            return False
        if _captcha_is_done(tab):
            logger.info(f"[Step 5] 验证码已通过（轮询 {poll_count} 次）")
            return True
        if _has_left_email_page(tab):
            logger.info("[Step 5] 页面已跳转（可能不需要验证码）")
            return True

        # 每 30 次轮询（约 60 秒）检查一次：如果 dialog 仍然是空的，重新加载页面
        if poll_count % 30 == 0 and not reload_attempted:
            try:
                has_rc = tab.run_js("""
                    return document.querySelectorAll('iframe[src*="recaptcha"]').length > 0
                """)
                if not has_rc:
                    logger.warning("[Step 5] 等待 60s 后 reCAPTCHA 仍未加载，刷新页面重试...")
                    tab.run_js("location.reload()")
                    time.sleep(5)
                    reload_attempted = True
            except Exception:
                pass

        time.sleep(config.DELAY_CAPTCHA_POLL)


# 跟踪 reCAPTCHA 是否曾经出现过（per-thread，用 threading.local）
_thread_local = threading.local()


def _mark_recaptcha_seen():
    """标记当前线程的任务已看到 reCAPTCHA iframe"""
    _thread_local.recaptcha_seen = True


def _was_recaptcha_seen() -> bool:
    """当前线程的任务是否曾看到过 reCAPTCHA"""
    return getattr(_thread_local, 'recaptcha_seen', False)


def _reset_recaptcha_seen():
    """重置标记（新任务开始时调用）"""
    _thread_local.recaptcha_seen = False


def _captcha_is_done(tab) -> bool:
    """
    检查 reCAPTCHA 验证码是否已通过。
    核心逻辑：用一次 JS 调用检测多种信号，避免频繁 DrissionPage 查找。
    """
    try:
        result = tab.run_js("""
            // 如果 dialog[open] 还在且内含 reCAPTCHA，验证还没完成
            const dialog = document.querySelector('dialog[open]');
            if (dialog) {
                const hasRecaptcha = dialog.querySelector('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"]');
                if (hasRecaptcha) return 'dialog_blocking';
            }

            // 已进入 OTP 页面
            if (document.querySelector('input[name="otp-1"]')) return 'otp';
            const ml = document.querySelectorAll('input[maxlength="1"]');
            if (ml.length >= 4) return 'otp_ml';

            // 已进入注册表单
            if (document.querySelector('input[type="password"]')) return 'password';

            // 页面文字
            const txt = (document.body.textContent || '').toLowerCase();
            if (txt.includes('verification code') || txt.includes('enter the code')
                || txt.includes('confirm your email') || txt.includes('check your email'))
                return 'text_match';

            return '';
        """)

        if result == 'dialog_blocking':
            return False
        if result and result != '':
            return True
    except Exception:
        pass

    # 降级：reCAPTCHA checkbox aria-checked=true
    try:
        anchor_frame = tab.get_frame("@title=reCAPTCHA", timeout=2)
        if anchor_frame:
            _mark_recaptcha_seen()
            anchor = anchor_frame.ele("#recaptcha-anchor", timeout=1)
            if anchor and anchor.attr("aria-checked") == "true":
                return True
    except Exception:
        pass

    # 降级：reCAPTCHA iframe 消失（仅在确认曾经出现过后才判定）
    if _was_recaptcha_seen():
        try:
            has_dialog = tab.run_js("return !!document.querySelector('dialog[open]')")
            if not has_dialog:
                # dialog 关了且之前看到过 reCAPTCHA → 验证通过
                return True
        except Exception:
            pass

    return False


# ═══════════════════════════════════════════════════════════
#  Step 5-Platform: 打码平台 Token 注入
#  提取 sitekey → 调用打码平台 → 获取 gRecaptchaResponse → JS 注入
#  （移植自 baiqi-GhostReg）
# ═══════════════════════════════════════════════════════════

def _extract_sitekey(tab) -> str:
    """从页面中提取 reCAPTCHA sitekey（从 iframe src 的 k= 参数中提取）"""
    try:
        sitekey = tab.run_js("""
            for (const f of document.querySelectorAll('iframe')) {
                const src = f.src || '';
                if (src.includes('recaptcha') && src.includes('/anchor')) {
                    const m = src.match(/[?&]k=([^&]+)/);
                    if (m) return m[1];
                }
            }
            // 降级：查找 data-sitekey 属性
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            return '';
        """)
        if sitekey:
            logger.info(f"[Platform] 提取到 sitekey: {sitekey[:20]}...")
        return sitekey or ""
    except Exception as e:
        logger.warning(f"[Platform] 提取 sitekey 失败: {e}")
        return ""


def _inject_recaptcha_token(tab, token: str) -> bool:
    """
    将打码平台返回的 gRecaptchaResponse token 注入到页面中。
    核心步骤：填 textarea → 触发回调 → 关闭 dialog → 移除 reCAPTCHA iframe
    """
    import json as _json
    safe_token = _json.dumps(token)
    try:
        tab.run_js(f'window.__ghostreg_token = {safe_token};')
    except Exception as e:
        logger.warning(f"[Platform] 设置 token 变量失败: {e}")
        return False

    try:
        result = tab.run_js("""
            const token = window.__ghostreg_token;
            let status = [];

            // 1. 填充所有 g-recaptcha-response textarea
            const textareas = document.querySelectorAll('textarea[id*="g-recaptcha-response"]');
            for (const ta of textareas) {
                ta.innerHTML = token;
                ta.value = token;
                ta.style.display = 'block';
            }
            status.push('filled:' + textareas.length);

            // 2. 触发 reCAPTCHA 回调
            let callbackFired = false;
            try {
                const clients = ___grecaptcha_cfg.clients;
                for (const cid in clients) {
                    const client = clients[cid];
                    function findCb(obj, depth) {
                        if (depth > 8 || !obj || typeof obj !== 'object') return null;
                        for (const k of ['callback', 'success-callback', 'resolve']) {
                            if (typeof obj[k] === 'function') return obj[k];
                        }
                        for (const k in obj) {
                            if (typeof obj[k] === 'object' && obj[k] !== null) {
                                const found = findCb(obj[k], depth + 1);
                                if (found) return found;
                            }
                        }
                        return null;
                    }
                    const cb = findCb(client, 0);
                    if (cb) {
                        cb(token);
                        callbackFired = true;
                        status.push('callback_ok');
                        break;
                    }
                }
            } catch(e) { status.push('callback_err:' + e.message); }

            // 3. 全局回调降级
            if (!callbackFired) {
                try {
                    if (typeof onRecaptchaSuccess === 'function') {
                        onRecaptchaSuccess(token);
                        callbackFired = true;
                        status.push('global_cb');
                    }
                } catch(e) {}
            }

            // 4. 强制关闭 dialog
            const dialogs = document.querySelectorAll('dialog[open]');
            for (const d of dialogs) {
                d.removeAttribute('open');
                d.close && d.close();
            }
            status.push('dialog_closed:' + dialogs.length);

            // 5. 隐藏/移除 reCAPTCHA iframe
            const iframes = document.querySelectorAll('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"], iframe[src*="recaptcha"]');
            let hidden = 0;
            for (const f of iframes) {
                f.style.display = 'none';
                f.style.visibility = 'hidden';
                f.style.width = '0';
                f.style.height = '0';
                let parent = f.parentElement;
                for (let i = 0; i < 5 && parent; i++) {
                    if (parent.style && (parent.style.position === 'fixed' || parent.style.position === 'absolute'
                        || parent.style.zIndex > 1000 || getComputedStyle(parent).position === 'fixed')) {
                        parent.style.display = 'none';
                        break;
                    }
                    parent = parent.parentElement;
                }
                hidden++;
            }
            status.push('iframes_hidden:' + hidden);

            // 6. 移除所有 reCAPTCHA 遮罩层
            const overlays = document.querySelectorAll('div[style*="z-index"][style*="position: fixed"], div[style*="z-index"][style*="position:fixed"]');
            let removed = 0;
            for (const o of overlays) {
                const zi = parseInt(getComputedStyle(o).zIndex);
                if (zi > 1000000) {
                    o.style.display = 'none';
                    removed++;
                }
            }
            status.push('overlays_removed:' + removed);

            return status.join('|');
        """)
        logger.info(f"[Platform] Token 注入结果: {result}")
        return True
    except Exception as e:
        logger.warning(f"[Platform] Token 注入失败: {e}")
        return False


def _cleanup_recaptcha_overlays(tab):
    """清理 reCAPTCHA 残留的 dialog/iframe/遮罩层，防止遮挡后续操作"""
    try:
        tab.run_js("""
            // 关闭所有 dialog
            document.querySelectorAll('dialog[open]').forEach(d => {
                d.removeAttribute('open');
                try { d.close(); } catch(e) {}
            });
            // 隐藏 reCAPTCHA iframe
            document.querySelectorAll('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"], iframe[src*="recaptcha"]').forEach(f => {
                f.style.display = 'none';
                f.style.width = '0';
                f.style.height = '0';
            });
            // 隐藏 Google reCAPTCHA 遮罩层（z-index > 1000000 的 fixed div）
            document.querySelectorAll('div').forEach(d => {
                const s = getComputedStyle(d);
                if (s.position === 'fixed' && parseInt(s.zIndex) > 1000000) {
                    d.style.display = 'none';
                }
            });
        """)
    except Exception:
        pass


def _solve_with_platform(tab, cancel_flag: Callable[[], bool] | None = None) -> bool:
    """
    使用打码平台解决 reCAPTCHA v2：
    1. 从页面提取 sitekey
    2. 调用打码平台 API 获取 token
    3. 注入 token 到页面
    """
    if not captcha_service.is_enabled():
        logger.warning("[Platform] 打码平台未配置")
        return False

    logger.info("[Platform] 开始打码平台流程...")

    # 提取 sitekey（最多重试 3 次）
    sitekey = ""
    for attempt in range(3):
        sitekey = _extract_sitekey(tab)
        if sitekey:
            break
        logger.warning(f"[Platform] 第 {attempt+1} 次未提取到 sitekey，等 1 秒...")
        time.sleep(1)

    if not sitekey:
        logger.error("[Platform] 无法提取 sitekey")
        return False

    try:
        page_url = tab.url or config.SIGNUP_URL
    except Exception:
        page_url = config.SIGNUP_URL

    # 调用打码平台（10-80 秒）
    logger.info(f"[Platform] 调用打码平台... (sitekey={sitekey[:20]}...)")
    try:
        token = captcha_service.solve_recaptcha_v2(page_url, sitekey)
    except captcha_service.CaptchaServiceError as e:
        logger.error(f"[Platform] 打码平台失败: {e}")
        return False

    if not token:
        logger.error("[Platform] 打码平台未返回 token")
        return False

    # 注入 token + 关闭 dialog + 清理 iframe
    logger.info(f"[Platform] 获得 token（{len(token)} 字符），注入并清理...")
    if not _inject_recaptcha_token(tab, token):
        logger.error("[Platform] Token 注入失败")
        return False

    time.sleep(0.5)

    if _captcha_is_done(tab) or _has_left_email_page(tab):
        logger.info("[Platform] Token 注入后已通过")
        return True

    logger.info("[Platform] Token 已注入，等待外层提交 Continue")
    return True


# ═══════════════════════════════════════════════════════════
#  Step 5-AI: 纯 CDP 截图 + AI 坐标点击（零 iframe 操作）
#  CDP 截图瞬间完成 → AI 返回坐标 → tab.actions 绝对坐标点击
#  （移植自 baiqi-GhostReg）
# ═══════════════════════════════════════════════════════════

def _cdp_screenshot(tab) -> bytes | None:
    """CDP 截整页，瞬间完成，不会挂死"""
    import base64 as _b64
    try:
        r = tab.run_cdp("Page.captureScreenshot", format="png")
        if r and "data" in r:
            return _b64.b64decode(r["data"])
    except Exception as e:
        logger.warning(f"[AI] CDP 截图失败: {e}")
    return None


def _has_challenge_visible(tab) -> bool:
    """用 JS 检测 bframe 是否可见（不进 iframe，不会挂）"""
    try:
        result = tab.run_js("""
            for (const f of document.querySelectorAll('iframe')) {
                if ((f.src||'').includes('bframe') || (f.title||'').includes('recaptcha challenge')) {
                    const r = f.getBoundingClientRect();
                    if (r.width > 50 && r.height > 50) return true;
                }
            }
            return false;
        """)
        return bool(result)
    except Exception:
        return False


def _solve_recaptcha_with_ai(tab, cancel_flag: Callable[[], bool] | None = None) -> bool:
    """
    AI 自动识别 reCAPTCHA：纯 CDP 截图 + 坐标点击，零 iframe 操作。
    整页截图 → AI 看图识别要点击的格子 → 返回坐标 → tab.actions 点击。
    """
    round_num = 0
    try:
        vp = tab.run_js("return {w: window.innerWidth, h: window.innerHeight}")
        page_w, page_h = vp["w"], vp["h"]
    except Exception:
        page_w, page_h = 1920, 1080

    while True:
        round_num += 1
        if cancel_flag and cancel_flag():
            logger.info("[AI] 取消")
            return False
        if _captcha_is_done(tab):
            logger.info(f"[AI] 已通过（第 {round_num} 轮前）")
            return True

        logger.info(f"[AI] ===== 第 {round_num} 轮 =====")

        if not _has_challenge_visible(tab):
            logger.warning("[AI] 弹窗不可见，等 2 秒")
            time.sleep(2)
            if _captcha_is_done(tab):
                return True
            continue

        time.sleep(0.5)
        img = _cdp_screenshot(tab)
        if not img:
            time.sleep(1)
            continue
        logger.info(f"[AI] 截图 {len(img)} bytes")

        coords = captcha_solver.solve_click(img)
        if not coords:
            logger.warning("[AI] AI 无坐标返回")
            time.sleep(2)
            continue

        logger.info(f"[AI] {len(coords)} 个坐标: {coords!r}")

        clicked = 0
        for nx, ny in coords:
            ax = (page_w / 1000) * nx
            ay = (page_h / 1000) * ny
            try:
                tab.actions.move_to((ax, ay)).click()
                clicked += 1
                logger.info(f"[AI] 点击 ({nx},{ny})→({ax:.0f},{ay:.0f})")
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"[AI] 点击失败: {e}")

        if clicked == 0:
            time.sleep(2)
            continue

        # 点 Verify 按钮
        time.sleep(1)
        try:
            btn_info = tab.run_js("""
                for (const f of document.querySelectorAll('iframe')) {
                    if ((f.src||'').includes('bframe')) {
                        const r = f.getBoundingClientRect();
                        if (r.y >= 0 && r.height > 50)
                            return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height - 35});
                    }
                }
                return '';
            """)
            if btn_info:
                import json as _json
                pos = _json.loads(btn_info)
                if pos["y"] > 0:
                    tab.actions.move_to((pos["x"], pos["y"])).click()
                    logger.info(f"[AI] Verify ({pos['x']:.0f},{pos['y']:.0f})")
                else:
                    logger.warning(f"[AI] Verify 坐标异常 y={pos['y']}")
        except Exception as e:
            logger.warning(f"[AI] Verify 失败: {e}")

        time.sleep(2)
        if _captcha_is_done(tab):
            logger.info(f"[AI] 通过！（第 {round_num} 轮）")
            return True


# ═══════════════════════════════════════════════════════════
#  Step 5b: Post-captcha Continue 强化提交
#  （移植自 批量注册JB保留窗口.py 的 click_continue_react）
# ═══════════════════════════════════════════════════════════

def _has_left_email_page(tab) -> bool:
    """检测是否已离开初始邮箱输入页（到了 OTP / 注册表单等下一步）"""
    try:
        # 用一次 JS 检测多个条件，比逐个 DrissionPage 查找快得多
        result = tab.run_js("""
            // OTP 输入框（JetBrains 用 name="otp-1"）
            if (document.querySelector('input[name="otp-1"]')) return 'otp';
            // maxlength=1 的 OTP
            const ml = document.querySelectorAll('input[maxlength="1"]');
            if (ml.length >= 4) return 'otp_ml';
            // 注册表单
            if (document.querySelector('input[name="firstName"]')
                || document.querySelector('input[placeholder="First name"]')) return 'reg';
            // 密码输入框
            if (document.querySelector('input[type="password"]')) return 'pwd';
            // 页面文字
            const txt = (document.body.textContent || '').toLowerCase();
            if (txt.includes('confirm your email') || txt.includes('enter the code')
                || txt.includes('check your') || txt.includes('we sent')) return 'txt';
            return '';
        """)
        return bool(result)
    except Exception:
        pass
    return False


def _click_continue_after_captcha(tab) -> bool:
    """
    验证码完成后，自动提交 Continue 表单。
    核心难点：reCAPTCHA 在 <dialog open=""> 内，验证通过后 dialog 关闭，
    但 React 不会自动提交表单，需要再次点击 Continue 按钮。
    """
    logger.info("[Step 5b] 自动提交 Continue...")

    # ── 阶段1：等 dialog 关闭（最多 30 秒） ──
    dialog_was_open = False
    for wait_i in range(30):
        try:
            state = tab.run_js("""
                const d = document.querySelector('dialog[open]');
                if (d) {
                    // 检查 dialog 内是否还有 reCAPTCHA iframe
                    const hasRecaptcha = !!d.querySelector('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"]');
                    return hasRecaptcha ? 'captcha_active' : 'dialog_open';
                }
                return 'closed';
            """)
            if state and 'captcha' in str(state):
                dialog_was_open = True
            if state == 'closed':
                if dialog_was_open:
                    logger.info(f"[Step 5b] dialog 已关闭（等待 {wait_i}s）")
                break
        except Exception:
            break
        time.sleep(1)

    time.sleep(0.5)

    # ── 阶段2：多策略提交 Continue（最多 25 次） ──
    for attempt in range(25):
        if _has_left_email_page(tab):
            logger.info(f"[Step 5b] 已进入下一步（第 {attempt + 1} 次）")
            return True

        # 策略A：JS 一体化提交（最可靠 — 移除 dialog 遮挡 + requestSubmit + btn.click）
        try:
            result = tab.run_js("""
                // 强制移除残留的 dialog 遮挡
                const dialog = document.querySelector('dialog[open]');
                if (dialog) dialog.removeAttribute('open');

                const form = document.querySelector('form');
                if (!form) return 'no_form';

                // 方式1: requestSubmit（React 能捕获）
                try {
                    if (typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return 'requestSubmit';
                    }
                } catch(e) {}

                // 方式2: 直接点击 submit 按钮
                const btn = form.querySelector('button[type="submit"]');
                if (btn) {
                    btn.click();
                    return 'btn_click';
                }

                // 方式3: form.submit()
                try { form.submit(); return 'form_submit'; } catch(e) {}
                return 'failed';
            """)
            if result and result in ('requestSubmit', 'btn_click', 'form_submit'):
                if attempt == 0:
                    logger.info(f"[Step 5b] JS 提交: {result}")
                time.sleep(2)
                if _has_left_email_page(tab):
                    return True
        except Exception:
            pass

        # 策略B：DrissionPage 点击 submit 按钮（dialog 已在策略A中移除）
        try:
            submit_btn = tab.ele("@type=submit", timeout=2)
            if submit_btn:
                submit_btn.click()
                if attempt == 0:
                    logger.info("[Step 5b] DrissionPage click submit")
                time.sleep(2)
                if _has_left_email_page(tab):
                    return True
        except Exception:
            pass

        # 策略C：CDP 在 email input 上发 Enter 键
        try:
            tab.run_js("""
                const input = document.querySelector('input[name="email"]');
                if (input) {
                    input.focus();
                    input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', keyCode:13, bubbles:true}));
                    input.dispatchEvent(new KeyboardEvent('keypress', {key:'Enter', code:'Enter', keyCode:13, bubbles:true}));
                    input.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', code:'Enter', keyCode:13, bubbles:true}));
                }
            """)
            time.sleep(2)
            if _has_left_email_page(tab):
                return True
        except Exception:
            pass

        if attempt > 0 and attempt % 5 == 0:
            logger.info(f"[Step 5b] 第 {attempt + 1} 次尝试...")

        time.sleep(1)

    logger.warning("[Step 5b] Continue 提交超时")
    return False


# ═══════════════════════════════════════════════════════════
#  Step 6: 邮箱验证码（支持 OTP 码 + 验证链接双模式）
# ═══════════════════════════════════════════════════════════

def _extract_jb_link_or_code(html: str):
    """
    从邮件 HTML 中提取 JetBrains 验证链接或 6 位码。
    返回 ("LINK", url) 或 ("CODE", "123456") 或 None。
    移植自 批量注册JB保留窗口.py。
    """
    # 先找验证链接
    link_patterns = [
        r'href="(https://account\.jetbrains\.com/[^"]*(?:confirm|verify|signup|activate|reg|token)[^"]*)"',
        r'href="(https://[^"]*jetbrains[^"]*(?:confirm|verify|token|activate)[^"]*)"',
    ]
    for p in link_patterns:
        m = re.search(p, html, re.I)
        if m:
            return ("LINK", m.group(1).replace("&amp;", "&"))

    # 再找 6 位码
    code = email_service.extract_verification_code(html)
    if code:
        return ("CODE", code)

    return None


def _fill_verification_code(tab, email: str, task_id: int = 0, email_start_ts: int = 0, cancel_check: Callable = None) -> bool:
    """
    轮询邮箱获取验证码并填入。
    使用 email_service.poll_verification_code 统一轮询（支持 YYDS Mail API）。
    cancel_check: 可选的取消检查函数，返回 True 表示应停止。
    """
    tag = f"[Task {task_id} Step 6]"
    logger.info(f"{tag} 等待页面加载...")

    try:
        tab.wait.doc_loaded(timeout=15)
    except Exception:
        pass
    time.sleep(1.5)

    _inject_cookie_killer(tab)

    try:
        logger.info(f"{tag} 当前 URL: {tab.url}")
    except Exception:
        pass

    # 使用 email_service 统一的轮询函数获取验证码
    logger.info(f"{tag} 开始轮询邮箱验证码: {email}")
    mail_result = None

    try:
        # 先尝试用 poll_verification_code 获取纯验证码
        code = email_service.poll_verification_code(email, cancel_check=cancel_check)
        if code:
            mail_result = ("CODE", code)
            logger.info(f"{tag} 获取到验证码: {code}")
    except email_service.CancelledError:
        logger.info(f"{tag} 邮件轮询被取消")
        return False
    except TimeoutError:
        logger.error(f"{tag} 邮件轮询超时，未收到验证码")
        return False
    except Exception as e:
        logger.error(f"{tag} 轮询异常: {e}")
        # 如果 poll 失败，回退到手动轮询模式查找验证链接
        logger.info(f"{tag} 尝试回退查找验证链接...")
        try:
            deadline = time.time() + 60  # 额外等 60 秒找链接
            while time.time() < deadline:
                # 回退轮询也要检查取消
                if cancel_check and cancel_check():
                    logger.info(f"{tag} 回退轮询被取消")
                    return False
                try:
                    mails = email_service.get_mails(email)
                    for mail in mails:
                        content = mail.get("content", "") or mail.get("html", "") or mail.get("text", "")
                        if content:
                            res = _extract_jb_link_or_code(content)
                            if res:
                                mail_result = res
                                break
                    if mail_result:
                        break
                except Exception:
                    pass
                time.sleep(config.EMAIL_POLL_INTERVAL)
        except Exception:
            pass

    if not mail_result:
        logger.error(f"{tag} 邮件轮询超时，未收到验证")
        return False

    kind, payload = mail_result
    logger.info(f"{tag} 收到 {kind}: {str(payload)[:60]}")

    if kind == "LINK":
        logger.info(f"{tag} 打开验证链接...")
        try:
            tab.get(payload)
            tab.wait.doc_loaded(timeout=30)
            time.sleep(3)
        except Exception as e:
            logger.warning(f"{tag} 打开链接失败: {e}")
        return True

    # OTP 码模式
    code = str(payload).strip()
    logger.info(f"{tag} 获取到验证码: {code}")
    time.sleep(1)

    _force_dom_reflow(tab)

    # 等 OTP 输入框出现
    otp_found = False
    for wait_round in range(15):
        try:
            first_otp = tab.ele("@name=otp-1", timeout=2)
            if first_otp:
                otp_found = True
                break
        except Exception:
            pass
        try:
            code_inputs = tab.eles("input[maxlength='1']", timeout=2)
            if code_inputs and len(code_inputs) >= 4:
                otp_found = True
                break
        except Exception:
            pass
        if wait_round % 3 == 2:
            _force_dom_reflow(tab)
        time.sleep(1)

    if not otp_found:
        logger.warning(f"{tag} OTP 输入框未出现，尝试强制重排后再找...")
        _force_dom_reflow(tab)
        time.sleep(1)

    filled_ok = _fill_otp_by_name(tab, code)
    if not filled_ok:
        filled_ok = _fill_otp_by_maxlength(tab, code)
    if not filled_ok:
        filled_ok = _fill_otp_single_input(tab, code)
    if not filled_ok:
        filled_ok = _fill_otp_fallback(tab, code)

    if not filled_ok:
        logger.error(f"{tag} 未找到验证码输入框")
        return False

    # 填入后验证：检查页面是否接受了验证码（等几秒看是否出现错误提示）
    time.sleep(2)
    try:
        page_text = tab.run_js("return document.body.innerText") or ""
        if "invalid" in page_text.lower() or "incorrect" in page_text.lower() or "expired" in page_text.lower():
            logger.warning(f"{tag} 验证码可能错误（页面提示 invalid/incorrect/expired），code={code}")
        elif "error" in page_text.lower() and "otp" in page_text.lower():
            logger.warning(f"{tag} 验证码填入后页面有 OTP 错误提示")
    except Exception:
        pass

    return True


def _force_dom_reflow(tab):
    """
    强制浏览器 DOM 重排。
    解决 React SPA 页面中，元素已渲染到 virtual DOM 但尚未真正 layout 的问题。
    这就是为什么打开 F12 DevTools 会突然触发填入的原因。
    """
    try:
        tab.run_js("""
            void document.body.offsetHeight;
            void document.body.getBoundingClientRect();
            window.dispatchEvent(new Event('resize'));
        """)
    except Exception:
        pass
    time.sleep(0.3)


def _fill_otp_by_name(tab, code: str) -> bool:
    """
    JetBrains 专用：通过 input[name="otp-1"] ~ input[name="otp-6"] 填入。
    多策略逐步尝试，每次填入后回读验证，确保正确性。
    """
    try:
        first_otp = tab.ele("@name=otp-1", timeout=3)
        if not first_otp:
            return False
    except Exception:
        return False

    logger.info(f"[Step 6] 找到 otp-1 ~ otp-6 输入框，开始填入 {len(code)} 位验证码")

    # ── 策略 A：JS 直接设置 value + 触发 React 合成事件（最可靠）──
    try:
        result = tab.run_js(f"""
            const code = '{code}';
            let filled = 0;
            for (let i = 0; i < Math.min(code.length, 6); i++) {{
                const inp = document.querySelector('input[name="otp-' + (i+1) + '"]');
                if (inp) {{
                    // React 内部属性 hack：直接修改 value 并触发事件
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(inp, code[i]);
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    filled++;
                }}
            }}
            return filled;
        """)
        if result and result >= len(code):
            logger.info(f"[Step 6] 策略A JS React hack 填入 {result} 位")
            time.sleep(0.5)
            # 回读验证
            filled = _read_otp_values(tab, len(code))
            if filled == code:
                logger.info(f"[Step 6] OTP 验证通过 (策略A): {filled}")
                return True
            logger.info(f"[Step 6] 策略A 回读: '{filled}'，继续尝试其他策略")
    except Exception as e:
        logger.debug(f"[Step 6] 策略A 异常: {e}")

    # ── 策略 B：清空后逐个 clear + input + 触发事件 ──
    try:
        for i, ch in enumerate(code[:6]):
            inp = tab.ele(f"@name=otp-{i+1}", timeout=1)
            if inp:
                inp.clear()
                time.sleep(0.05)
                inp.click()
                time.sleep(0.1)
                inp.input(ch)
                try:
                    tab.run_js(f"""
                        const el = document.querySelector('input[name="otp-{i+1}"]');
                        if (el) {{
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            setter.call(el, '{ch}');
                            el.dispatchEvent(new Event('input', {{bubbles:true}}));
                            el.dispatchEvent(new Event('change', {{bubbles:true}}));
                        }}
                    """)
                except Exception:
                    pass
                time.sleep(0.1)
        time.sleep(0.5)
        filled = _read_otp_values(tab, len(code))
        if filled == code:
            logger.info(f"[Step 6] OTP 验证通过 (策略B): {filled}")
            return True
        logger.info(f"[Step 6] 策略B 回读: '{filled}'，继续尝试")
    except Exception as e:
        logger.debug(f"[Step 6] 策略B 异常: {e}")

    # ── 策略 C：CDP 键盘逐字符输入（点击第一个框，然后逐个键入）──
    try:
        first_otp.click()
        time.sleep(0.3)
        for ch in code[:6]:
            # 使用 char 类型事件让 React 正确捕获字符输入
            tab.run_cdp('Input.dispatchKeyEvent', type='keyDown', text=ch,
                        key=ch, code=f'Digit{ch}', windowsVirtualKeyCode=ord(ch))
            tab.run_cdp('Input.dispatchKeyEvent', type='char', text=ch,
                        key=ch, code=f'Digit{ch}', windowsVirtualKeyCode=ord(ch))
            tab.run_cdp('Input.dispatchKeyEvent', type='keyUp', text=ch,
                        key=ch, code=f'Digit{ch}', windowsVirtualKeyCode=ord(ch))
            time.sleep(config.DELAY_OTP_CHAR)
        time.sleep(config.DELAY_INPUT)
        filled = _read_otp_values(tab, len(code))
        if filled == code:
            logger.info(f"[Step 6] OTP 验证通过 (策略C CDP): {filled}")
            return True
        if filled and len(filled) >= 4:
            logger.info(f"[Step 6] OTP 部分填入 (策略C): '{filled}' (期望 '{code}')，视为成功")
            return True
        logger.info(f"[Step 6] 策略C 回读: '{filled}'")
    except Exception as e:
        logger.debug(f"[Step 6] 策略C 异常: {e}")

    # 最后回读一次 — 有些 React 受控组件 value 不通过 attr 暴露
    filled = _read_otp_values(tab, len(code))
    if filled and len(filled) >= 4:
        logger.info(f"[Step 6] OTP 最终回读: '{filled}'，视为成功")
        return True

    # 如果所有策略都无法确认填入，仍返回 True（输入框已找到，可能是 React 状态问题）
    # 但记录警告让日志可追踪
    logger.warning(f"[Step 6] OTP 所有策略执行完毕但无法确认填入结果，继续流程")
    return True


def _read_otp_values(tab, expected_len: int = 6) -> str:
    """回读 OTP 输入框的值"""
    filled = ""
    for i in range(1, expected_len + 1):
        try:
            inp = tab.ele(f"@name=otp-{i}", timeout=0.5)
            if inp:
                val = inp.attr("value") or ""
                filled += val
        except Exception:
            pass
    return filled


def _fill_otp_by_maxlength(tab, code: str) -> bool:
    """通过 input[maxlength='1'] 选择器查找并填入 OTP"""
    code_inputs = None
    try:
        code_inputs = tab.eles("input[maxlength='1']", timeout=3)
    except Exception:
        pass
    if not code_inputs:
        try:
            code_inputs = tab.eles("input[autocomplete='one-time-code']", timeout=2)
        except Exception:
            pass

    if not code_inputs or len(code_inputs) < 4:
        return False

    count = min(len(code), len(code_inputs))
    logger.info(f"[Step 6] 找到 {len(code_inputs)} 个 maxlength=1 输入框")

    for i in range(count):
        try:
            code_inputs[i].click()
            time.sleep(0.15)
            code_inputs[i].input(code[i])
            time.sleep(0.3)
        except Exception:
            pass

    logger.info(f"[Step 6] 已逐个填入 {count} 位验证码")
    return True


def _fill_otp_single_input(tab, code: str) -> bool:
    """单输入框模式"""
    for selector in ["input[placeholder*='code']", "input[placeholder*='Code']",
                     "@name=code", "input[inputmode='numeric']",
                     "input[autocomplete='one-time-code']"]:
        try:
            inp = tab.ele(selector, timeout=2)
            if inp:
                inp.clear()
                inp.input(code)
                logger.info(f"[Step 6] 已填入验证码（单输入框: {selector}）")
                return True
        except Exception:
            pass
    return False


def _fill_otp_fallback(tab, code: str) -> bool:
    """兜底：找所有可见 input"""
    all_inputs = []
    try:
        all_inputs = tab.eles("tag:input")
    except Exception:
        pass
    visible_inputs = [inp for inp in all_inputs
                      if inp.attr("type") in ("text", "tel", "number", None)]
    if len(visible_inputs) >= 6:
        for i, ch in enumerate(code[:6]):
            try:
                visible_inputs[i].input(ch)
                time.sleep(0.1)
            except Exception:
                pass
        logger.info("[Step 6] 兜底方式已填入验证码")
        return True
    return False


# ═══════════════════════════════════════════════════════════
#  Step 7: 姓名 + 密码 + 勾选协议 → Create account
# ═══════════════════════════════════════════════════════════

def _fill_profile_and_submit(tab, password: str, first_name: str, last_name: str) -> bool:
    """填写姓名密码并提交，返回 True 仅当确认页面已离开注册表单"""
    logger.info(f"[Step 7] 填写: {first_name} {last_name}")

    first_input = (
        tab.ele("@placeholder=First name", timeout=config.PAGE_TIMEOUT)
        or tab.ele("@name=firstName", timeout=5)
    )
    if first_input:
        first_input.clear()
        first_input.input(first_name)

    last_input = (
        tab.ele("@placeholder=Last name", timeout=5)
        or tab.ele("@name=lastName", timeout=5)
    )
    if last_input:
        last_input.clear()
        last_input.input(last_name)

    pwd_input = (
        tab.ele("@placeholder=Password", timeout=5)
        or tab.ele("@type=password", timeout=5)
    )
    if pwd_input:
        pwd_input.clear()
        pwd_input.input(password)

    time.sleep(0.5)

    # 自动勾选所有未勾选的 checkbox（协议、通知等）
    try:
        checkboxes = tab.eles("@type=checkbox", timeout=2)
        if checkboxes:
            for cb in checkboxes:
                try:
                    if not cb.attr("checked"):
                        cb.click()
                        time.sleep(0.2)
                except Exception:
                    pass
    except Exception:
        pass

    time.sleep(0.5)

    # 提交 Create account（多策略 + 反复重试 + 验证页面跳转）
    # 最多尝试 20 次，每次用不同策略，每次之后检查是否已离开表单页
    for attempt in range(20):
        # 检查是否已离开注册表单
        try:
            on_form = tab.run_js("""
                return !!document.querySelector('input[name="firstName"]');
            """)
            if not on_form:
                logger.info(f"[Step 7] 注册提交成功（第 {attempt + 1} 次检查）")
                return True
        except Exception:
            pass

        # 策略A（优先）：JS requestSubmit — React 表单最可靠的提交方式
        try:
            result = tab.run_js("""
                const form = document.querySelector('form');
                if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                    return 'requestSubmit';
                }
                const btn = document.querySelector('button[type="submit"]');
                if (btn) { btn.click(); return 'btn_click'; }
                return 'none';
            """)
            if result and result != 'none':
                if attempt == 0:
                    logger.info(f"[Step 7] JS 提交: {result}")
                time.sleep(1.5)
                continue
        except Exception:
            pass

        # 策略B：DrissionPage 点击 submit 按钮
        try:
            submit_btn = tab.ele("@type=submit", timeout=2)
            if submit_btn:
                submit_btn.click()
                if attempt == 0:
                    logger.info("[Step 7] DrissionPage click submit")
                time.sleep(1.5)
                continue
        except Exception:
            pass

        # 策略C：DrissionPage 文字匹配点击
        try:
            btn = tab.ele("text:Create account", timeout=2)
            if btn:
                btn.click()
                if attempt == 0:
                    logger.info("[Step 7] DrissionPage click 'Create account'")
                time.sleep(1.5)
                continue
        except Exception:
            pass

        time.sleep(1)

    # 最终检查
    try:
        still = tab.run_js("return !!document.querySelector('input[name=\"firstName\"]')")
        if not still:
            logger.info("[Step 7] 注册提交最终确认成功")
            return True
    except Exception:
        pass

    logger.warning("[Step 7] 20 次尝试后仍在注册表单页，提交失败")
    return False


# ═══════════════════════════════════════════════════════════
#  Step 8: tokens 页 → 选日本 → Add credit card
#  （移植自 批量注册JB保留窗口.py 的 setup_tokens_page）
# ═══════════════════════════════════════════════════════════

_COUNTRY_NAMES = {
    "JP": "Japan", "US": "United States", "GB": "United Kingdom",
    "DE": "Germany", "FR": "France", "KR": "Republic of Korea", "SG": "Singapore",
    "CA": "Canada", "AU": "Australia", "NL": "Netherlands", "SE": "Sweden",
    "CH": "Switzerland", "IN": "India", "BR": "Brazil", "IT": "Italy",
    "ES": "Spain", "PT": "Portugal", "AT": "Austria", "BE": "Belgium",
    "DK": "Denmark", "FI": "Finland", "NO": "Norway", "IE": "Ireland",
    "PL": "Poland", "CZ": "Czech Republic", "GR": "Greece", "IL": "Israel",
    "NZ": "New Zealand", "MX": "Mexico", "AR": "Argentina", "CL": "Chile",
    "CO": "Colombia", "PE": "Peru", "PH": "Philippines", "TH": "Thailand",
    "MY": "Malaysia", "ID": "Indonesia", "VN": "Vietnam", "ZA": "South Africa",
    "EG": "Egypt", "NG": "Nigeria", "KE": "Kenya", "TR": "Türkiye",
    "RO": "Romania", "HU": "Hungary", "BG": "Bulgaria", "HR": "Croatia",
    "SK": "Slovakia", "SI": "Slovenia", "LT": "Lithuania", "LV": "Latvia",
    "EE": "Estonia", "IS": "Iceland", "MT": "Malta", "CY": "Cyprus",
    "LU": "Luxembourg", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
    "QA": "Qatar", "KW": "Kuwait", "BH": "Bahrain", "OM": "Oman",
    "JO": "Jordan", "UY": "Uruguay", "PA": "Panama", "CR": "Costa Rica",
    "EC": "Ecuador", "DO": "Dominican Republic", "GT": "Guatemala",
    "BD": "Bangladesh", "PK": "Pakistan", "LK": "Sri Lanka", "NP": "Nepal",
    "KH": "Cambodia", "MM": "Myanmar", "MN": "Mongolia", "KZ": "Kazakhstan",
    "UZ": "Uzbekistan", "GE": "Georgia", "AM": "Armenia", "AZ": "Azerbaijan",
    "RS": "Serbia", "BA": "Bosnia and Herzegovina", "ME": "Montenegro",
    "MK": "North Macedonia", "AL": "Albania", "MD": "Moldova",
    "TT": "Trinidad and Tobago", "JM": "Jamaica", "MU": "Mauritius",
    "GH": "Ghana", "TZ": "Tanzania", "SN": "Senegal",
}


def _get_country_name(code: str) -> str:
    return _COUNTRY_NAMES.get(code.upper(), code)


def _setup_tokens_page(tab, country_code: str = "JP",
                       do_select_country: bool = True,
                       do_click_add_card: bool = True) -> bool:
    """注册成功后：导航到 tokens 页 → 选国家 → 点 Add credit card
    
    Args:
        do_select_country: False 时跳过选国家，只导航到 tokens 页
        do_click_add_card: False 时跳过点 Add credit card
    """
    logger.info(f"[Step 8] 跳转到 tokens 页（国家={country_code}，自动选国家={do_select_country}，自动点绑卡={do_click_add_card}）...")

    try:
        tab.get(TOKENS_URL)
        tab.wait.doc_loaded(timeout=30)
    except Exception as e:
        logger.warning(f"[Step 8] 导航失败: {e}")
    time.sleep(2)

    _inject_cookie_killer(tab)
    _dismiss_cookie_banner(tab)

    if not do_select_country and not do_click_add_card:
        logger.info("[Step 8] 自动选国家和自动点绑卡均已关闭，仅导航到 tokens 页")
        return True

    # 等待 SPA 渲染出可操作元素
    _wait_tokens_page_ready(tab)

    # 判断当前页面状态
    page_state = _detect_tokens_state(tab)
    logger.info(f"[Step 8] 页面状态: {page_state}")

    if page_state == 'has_add_card':
        # 已有国家且直接显示 Add credit card
        if do_click_add_card:
            return _click_add_credit_card(tab)
        logger.info("[Step 8] Add credit card 可用，但自动点绑卡已关闭")
        return True

    if do_select_country:
        # 需要选国家
        if page_state == 'has_country_modal':
            pass  # 弹窗已打开，直接选
        else:
            _click_select_country(tab)
            time.sleep(1)

        _select_country_in_modal(tab, country_code)
        _click_save_button(tab)
        time.sleep(2)
    else:
        logger.info("[Step 8] 自动选国家已关闭，跳过")

    # 点 Add credit card
    if do_click_add_card:
        return _click_add_credit_card(tab)
    else:
        logger.info("[Step 8] 自动点绑卡已关闭，跳过")
        return True


def _detect_tokens_state(tab) -> str:
    """检测 tokens 页面当前状态"""
    try:
        state = tab.run_js("""
            const txt = document.body.textContent || '';
            const html = document.body.innerHTML || '';
            // 已有 "Add credit card" 可点击
            const addBtn = document.querySelector('a[href*="credit"], button');
            let hasAddCard = false;
            if (addBtn) {
                document.querySelectorAll('a, button').forEach(el => {
                    if ((el.textContent||'').trim() === 'Add credit card' && el.offsetParent !== null)
                        hasAddCard = true;
                });
            }
            if (hasAddCard) return 'has_add_card';
            // 弹窗中有 select[name=country]（modal 已打开）
            const modal = document.querySelector('.modal.in, .modal[style*="display: block"]');
            if (modal && modal.querySelector('select[name="country"]')) return 'has_country_modal';
            // 有 "Select country" 链接
            if (txt.includes('Select country')) return 'need_select_country';
            // 有 Change 链接（国家已设但想改）
            if (html.includes('Change') && html.includes('country')) return 'has_country_set';
            return 'unknown';
        """)
        return state or 'unknown'
    except Exception:
        return 'unknown'


def _click_select_country(tab):
    """点击 Select country / Change 链接打开国家弹窗"""
    try:
        # 优先用 JS 精准点击（比 DrissionPage ele 查找快得多）
        clicked = tab.run_js("""
            // 优先找 "Select country"
            const links = document.querySelectorAll('a, button, span');
            for (const el of links) {
                const t = (el.textContent||'').trim();
                if (t === 'Select country' || t === 'Select') {
                    el.click(); return 'select';
                }
            }
            // 备选：找 country 旁边的 Change
            const labels = document.querySelectorAll('label, .control-label');
            for (const lbl of labels) {
                if ((lbl.textContent||'').includes('Country')) {
                    const row = lbl.closest('.form-group') || lbl.parentElement;
                    if (row) {
                        const change = row.querySelector('a[data-toggle="modal"], a.link');
                        if (change) { change.click(); return 'change'; }
                    }
                }
            }
            return '';
        """)
        if clicked:
            logger.info(f"[Step 8] 已点击 {clicked}")
            time.sleep(1.5)
        else:
            logger.info("[Step 8] 未找到 Select country / Change 链接")
    except Exception as e:
        logger.info(f"[Step 8] 点击 Select country 失败: {e}")


def _select_country_in_modal(tab, country_code: str):
    """在已打开的弹窗中选择国家（快速重试）"""
    cc = country_code.upper()
    # 快速重试等 modal 中的 select 渲染
    for attempt in range(8):
        try:
            result = tab.run_js(f"""
                // 找到可见 modal 中的 select
                const modals = document.querySelectorAll('.modal');
                let sel = null;
                for (const m of modals) {{
                    if (m.classList.contains('in') || getComputedStyle(m).display !== 'none') {{
                        sel = m.querySelector('select[name="country"]');
                        if (sel) break;
                    }}
                }}
                if (!sel) sel = document.querySelector('select[name="country"]');
                if (!sel) return 'no_select';

                // 设置值
                sel.value = '{cc}';
                sel.dispatchEvent(new Event('change', {{bubbles: true}}));

                // 更新 Chosen UI
                const opt = sel.querySelector('option[value="{cc}"]');
                const name = opt ? opt.textContent.trim() : '{cc}';
                const chosenSpan = document.querySelector('.chosen-single span');
                if (chosenSpan) chosenSpan.textContent = name;
                try {{
                    if (window.jQuery) {{
                        jQuery(sel).val('{cc}').trigger('chosen:updated').trigger('change');
                    }}
                }} catch(e) {{}}
                return sel.value;
            """)
            if result and result != 'no_select':
                logger.info(f"[Step 8] 国家已选择: {result}")
                return
        except Exception:
            pass
        if attempt < 7:
            time.sleep(0.5)  # 快速重试，0.5 秒间隔

    logger.warning(f"[Step 8] select 元素始终未就绪，尝试 Chosen UI")
    # 兜底：通过 Chosen UI 搜索选择
    country_name = _get_country_name(cc)
    try:
        chosen = tab.ele(".chosen-container .chosen-single", timeout=2)
        if chosen:
            chosen.click()
            time.sleep(0.3)
            search = tab.ele(".chosen-container .chosen-search input", timeout=2)
            if search:
                search.input(country_name)
                time.sleep(0.5)
                result_item = tab.ele(".chosen-results li", timeout=2)
                if result_item and country_name.lower() in (result_item.text or "").lower():
                    result_item.click()
                    logger.info(f"[Step 8] Chosen UI 选择了 {country_name}")
    except Exception as e:
        logger.warning(f"[Step 8] Chosen UI 操作也失败: {e}")


def _click_save_button(tab):
    """点击弹窗中的 Save 按钮"""
    try:
        clicked = tab.run_js("""
            // 找可见 modal 中的 Save
            const modals = document.querySelectorAll('.modal');
            for (const m of modals) {
                if (m.classList.contains('in') || getComputedStyle(m).display !== 'none') {
                    const btn = m.querySelector('button.btn-primary, button[type="submit"]');
                    if (btn && (btn.textContent||'').trim() === 'Save') {
                        btn.click(); return 'modal_save';
                    }
                }
            }
            // 兜底：任意可见 Save
            const all = document.querySelectorAll('button.btn-primary');
            for (const b of all) {
                if ((b.textContent||'').trim() === 'Save' && b.offsetParent !== null) {
                    b.click(); return 'fallback_save';
                }
            }
            return '';
        """)
        if clicked:
            logger.info(f"[Step 8] 已点击 Save ({clicked})")
            time.sleep(2)
        else:
            logger.info("[Step 8] 未找到 Save 按钮（可能不需要）")
    except Exception as e:
        logger.warning(f"[Step 8] 点击 Save 失败: {e}")


def _click_add_credit_card(tab) -> bool:
    """点击 Add credit card 按钮（快速重试）"""
    for attempt in range(6):
        try:
            clicked = tab.run_js("""
                const els = document.querySelectorAll('a, button, span');
                for (const el of els) {
                    if ((el.textContent||'').trim() === 'Add credit card' && el.offsetParent !== null) {
                        el.click(); return true;
                    }
                }
                return false;
            """)
            if clicked:
                logger.info(f"[Step 8] 已点击 Add credit card (尝试 {attempt+1})")
                time.sleep(1.5)
                return True
        except Exception:
            pass
        time.sleep(1)

    logger.warning("[Step 8] Add credit card 按钮未找到")
    return False


def _wait_tokens_page_ready(tab, timeout: int = 15):
    """
    等待 tokens 页面 SPA 核心内容渲染完成。
    检测可操作元素（而非泛文本），快速轮询。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ready = tab.run_js("""
                // 有可操作按钮/链接 = 页面就绪
                const els = document.querySelectorAll('a, button, span');
                for (const el of els) {
                    const t = (el.textContent||'').trim();
                    if (t === 'Add credit card' || t === 'Select country'
                        || t === 'Select' || t === 'Change') {
                        if (el.offsetParent !== null) return true;
                    }
                }
                // 或者已有 country select
                if (document.querySelector('select[name="country"]')) return true;
                return false;
            """)
            if ready:
                logger.info("[Step 8] tokens 页面已就绪")
                return
        except Exception:
            pass
        time.sleep(0.8)
    logger.warning("[Step 8] tokens 页面加载超时，继续操作")


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

def register_one(
    task_id: int = 0,
    password: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    browser_type: str = "chrome",
    country: str = "JP",
    on_status: StatusCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    incognito: bool = True,
    auto_select_country: bool = True,
    auto_click_add_card: bool = True,
    ai_captcha: bool = False,
    fullscreen: bool = False,
) -> AccountResult:
    """
    执行一次完整的全自动注册流程。

    Args:
        task_id: 任务编号（前端展示用）
        password: 密码
        first_name / last_name: 姓名（None 时自动随机生成）
        country: tokens 页面选择的国家代码（如 JP, US, DE）
        browser_type: 浏览器类型 chrome / edge / brave
        on_status: 状态回调，每步更新时调用
        cancel_check: 可选的取消检查函数，返回 True 表示应停止
        ai_captcha: 是否启用全自动验证码（True=打码平台/AI自动, False=手动）
    """
    if password is None:
        password = config.DEFAULT_PASSWORD

    # 随机真人英文名：当使用默认值或未指定时，每个任务独立随机
    # 这样批量注册的每个账号名字都不同，不会看起来关联
    use_random = (
        first_name is None
        or last_name is None
        or first_name == config.DEFAULT_FIRST_NAME
        or last_name == config.DEFAULT_LAST_NAME
    )
    if use_random:
        first_name, last_name = _random_name()

    if on_status is None:
        on_status = _noop_callback

    status = TaskStatus(task_id=task_id, password=password)

    def _update(step: int, label: str):
        status.step = step
        status.step_label = label
        on_status(status)

    def _is_cancelled() -> bool:
        """检查是否应该取消任务"""
        if cancel_check and cancel_check():
            return True
        return False

    browser = None
    data_dir = None

    def _fail(error_msg: str) -> AccountResult:
        """统一失败处理：标记失败但绝不关闭浏览器（保留窗口供用户检查/手动操作）"""
        status.success = False
        status.error = error_msg
        on_status(status)
        # 重要：绝对不调用 browser.quit()！
        # 浏览器窗口保留，用户可以手动操作或通过「关闭所有浏览器」按钮统一关闭
        logger.warning(f"[Task {task_id}] 失败但保留浏览器窗口: {error_msg}")
        try:
            if browser:
                tab = browser.latest_tab
                tab.run_js(f'document.title = "#{task_id} FAILED - {error_msg[:30]}"')
        except Exception:
            pass
        return AccountResult(email=status.email, password=password,
                             success=False, error=error_msg, browser=browser)

    def _check_browser_alive() -> bool:
        """检查浏览器是否仍然存活"""
        return _safe_browser_check(browser)

    try:
        # 重置 reCAPTCHA 跟踪标记（每个任务独立）
        _reset_recaptcha_seen()

        # 0. 申请临时邮箱
        _update(0, "申请邮箱...")
        email = email_service.apply_email()
        status.email = email
        logger.info(f"[Task {task_id}] 邮箱: {email}")

        # 启动浏览器（如指纹浏览器可用，自动生成独立指纹）
        _update(0, "启动浏览器...")
        fp_seed = random.randint(10_000_000, 2_000_000_000) if _is_fingerprint_enabled() else None
        browser, fp_info, data_dir = _create_browser(browser_type, fp_seed=fp_seed, incognito=incognito, fullscreen=fullscreen)
        if fp_info:
            logger.info(f"[Task {task_id}] 指纹浏览器已启动 seed={fp_info['seed']} "
                         f"{fp_info['platform']}/{fp_info['brand']}")
        tab = browser.latest_tab
        logger.info(f"[Task {task_id}] 浏览器已启动 ({first_name} {last_name})，导航到注册页...")

        # 设置窗口标题
        try:
            tab.run_js(f'document.title = "#{task_id} 启动中..."')
        except Exception:
            pass

        if not _safe_get(tab, config.SIGNUP_URL, timeout=config.PAGE_TIMEOUT):
            logger.warning(f"[Task {task_id}] 注册页加载可能不完整，继续尝试...")
        time.sleep(config.DELAY_PAGE_NAV)

        if _is_cancelled():
            return _fail("用户停止了任务")
        if not _check_browser_alive():
            return _fail("浏览器进程已退出")

        # Step 1
        _update(1, "Cookie 弹窗")
        if not _handle_cookie_consent(tab):
            return _fail("Cookie 弹窗处理失败")

        # Step 2
        if _is_cancelled():
            return _fail("用户停止了任务")
        if not _check_browser_alive():
            return _fail("浏览器进程已退出")
        _update(2, "Continue with email")
        if not _click_continue_with_email(tab):
            return _fail("浏览器连接断开或未找到 Continue with email")

        # Step 3
        if _is_cancelled():
            return _fail("用户停止了任务")
        if not _check_browser_alive():
            return _fail("浏览器进程已退出")
        _update(3, "填写邮箱")
        # 记录邮箱提交时间（用于 Step 6 过滤旧邮件）
        email_submit_ts = int(time.time() * 1000)
        if not _fill_email(tab, email):
            return _fail("填写邮箱失败")

        # Step 4 — reCAPTCHA
        _update(4, "点击人机验证")
        try:
            tab.run_js(f'document.title = "#{task_id} 人机验证"')
        except Exception:
            pass

        # 尝试点击 reCAPTCHA checkbox（如果 iframe 存在）
        if not _click_recaptcha_checkbox(tab):
            return _fail("reCAPTCHA checkbox 失败")

        # 如果 reCAPTCHA 从未出现过，说明 Step 3 的 Continue 可能没生效
        # 重新尝试提交，然后等 reCAPTCHA 出现
        if not _was_recaptcha_seen() and not _has_left_email_page(tab):
            logger.info("[Step 4] reCAPTCHA 未出现，重新提交 Continue 触发加载...")
            # 再次点击 Continue
            try:
                submit_btn = tab.ele("@type=submit", timeout=3)
                if submit_btn:
                    submit_btn.click()
                    logger.info("[Step 4] 已重新点击 Continue")
                    time.sleep(3)
            except Exception:
                pass
            # 等待 reCAPTCHA iframe 出现（最多 30 秒）
            for retry in range(15):
                try:
                    frame = tab.get_frame("@title=reCAPTCHA", timeout=2)
                    if frame:
                        _mark_recaptcha_seen()
                        logger.info("[Step 4] reCAPTCHA iframe 已出现，尝试点击 checkbox...")
                        try:
                            checkbox = frame.ele("#recaptcha-anchor", timeout=5)
                            if checkbox:
                                checkbox.click()
                                logger.info("[Step 4] 已点击 reCAPTCHA checkbox")
                                time.sleep(3)
                        except Exception:
                            pass
                        break
                except Exception:
                    pass
                if _has_left_email_page(tab):
                    logger.info("[Step 4] 已离开邮箱页（无需 reCAPTCHA）")
                    break
                time.sleep(2)

        # Step 5 — 验证码处理（三级自动化：打码平台 → AI → 手动）
        if not _has_left_email_page(tab) and not _captcha_is_done(tab):
            if ai_captcha and captcha_service.is_enabled():
                # ── 打码平台自动模式 ──
                _update(5, "打码平台识别中...")
                platform_ok = _solve_with_platform(tab, cancel_flag=_is_cancelled)
                if not platform_ok and not _captcha_is_done(tab):
                    # 打码平台失败 → 降级到 AI
                    if config.AI_CAPTCHA_ENABLED:
                        logger.warning("[Step 5] 打码平台失败，降级到 AI 模式")
                        _update(5, "AI 识别验证码中...")
                        ai_ok = _solve_recaptcha_with_ai(tab, cancel_flag=_is_cancelled)
                        if not ai_ok and not _captcha_is_done(tab):
                            # AI 也失败 → 降级到手动
                            logger.warning("[Step 5] AI 也失败，降级到手动模式")
                            _update(5, "自动识别失败，请手动完成验证码")
                            if not _wait_for_manual_captcha(tab, cancel_flag=_is_cancelled):
                                if _is_cancelled():
                                    return _fail("用户停止了任务")
                                return _fail("验证码未完成")
                    else:
                        # AI 未启用 → 直接手动
                        logger.warning("[Step 5] 打码平台失败，请手动完成验证码")
                        _update(5, "打码失败，请手动完成验证码")
                        if not _wait_for_manual_captcha(tab, cancel_flag=_is_cancelled):
                            if _is_cancelled():
                                return _fail("用户停止了任务")
                            return _fail("验证码未完成")
            elif ai_captcha and not captcha_service.is_enabled() and config.AI_CAPTCHA_ENABLED:
                # ── 未配置打码平台但 AI 可用 ──
                _update(5, "AI 识别验证码中...")
                ai_ok = _solve_recaptcha_with_ai(tab, cancel_flag=_is_cancelled)
                if not ai_ok and not _captcha_is_done(tab):
                    logger.warning("[Step 5] AI 失败，降级到手动模式")
                    _update(5, "AI 识别失败，请手动完成验证码")
                    if not _wait_for_manual_captcha(tab, cancel_flag=_is_cancelled):
                        if _is_cancelled():
                            return _fail("用户停止了任务")
                        return _fail("验证码未完成")
            elif ai_captcha:
                # ── 勾选了自动但什么都没配置 → 提示后转手动 ──
                logger.warning("[Step 5] 未配置打码平台且 AI 未启用，降级到手动模式")
                _update(5, "未配置自动验证码，请手动完成")
                if not _wait_for_manual_captcha(tab, cancel_flag=_is_cancelled):
                    if _is_cancelled():
                        return _fail("用户停止了任务")
                    return _fail("验证码未完成（未配置自动验证码）")
            else:
                # ── 手动模式 ──
                _update(5, "请手动完成验证码")
                if not _wait_for_manual_captcha(tab, cancel_flag=_is_cancelled):
                    if _is_cancelled():
                        return _fail("用户停止了任务")
                    return _fail("验证码超时（用户未操作）")

        # Step 5b — 验证码完成后，自动点击 Continue 提交表单
        if not _has_left_email_page(tab):
            _update(5, "自动提交...")
            # 清理 reCAPTCHA 残留遮罩（打码平台/AI 模式可能留下 dialog/overlay）
            _cleanup_recaptcha_overlays(tab)
            _click_continue_after_captcha(tab)

        # 等待跳转到 OTP / 下一步页面
        logger.info("[Flow] 等待 OTP/下一步页面...")
        for wait_i in range(60):
            if _is_cancelled():
                return _fail("用户停止了任务")
            if _has_left_email_page(tab):
                logger.info("[Flow] 已进入下一步")
                break
            if wait_i > 0 and wait_i % 10 == 0:
                logger.info(f"[Flow] 仍在等待... ({wait_i}s)")
                if not _check_browser_alive():
                    return _fail("浏览器进程已退出")
                _click_continue_after_captcha(tab)
            time.sleep(1)
        time.sleep(2)

        # Step 6
        if _is_cancelled():
            return _fail("用户停止了任务")
        if not _check_browser_alive():
            return _fail("浏览器进程已退出")
        _update(6, "等待邮箱验证码")
        try:
            tab.run_js(f'document.title = "#{task_id} 等验证码..."')
        except Exception:
            pass
        if not _fill_verification_code(tab, email, task_id=task_id, email_start_ts=email_submit_ts, cancel_check=_is_cancelled):
            if _is_cancelled():
                return _fail("用户停止了任务")
            return _fail("填写验证码失败")

        # Step 7 — 清理 reCAPTCHA 残留遮罩后再填写
        if _is_cancelled():
            return _fail("用户停止了任务")
        if not _check_browser_alive():
            return _fail("浏览器进程已退出")
        _update(7, "填写密码并注册")
        _cleanup_recaptcha_overlays(tab)
        try:
            tab.run_js(f'document.title = "#{task_id} 注册中..."')
        except Exception:
            pass
        if not _fill_profile_and_submit(tab, password, first_name, last_name):
            return _fail("提交注册失败（页面未跳转，Create account 可能未生效）")

        # Step 7 已验证页面离开了注册表单，可以继续
        logger.info(f"[Task {task_id}] 注册表单已提交成功")

        # Step 8 — tokens 页 + 选国家 + Add credit card
        if _is_cancelled():
            return _fail("用户停止了任务")
        if not _check_browser_alive():
            return _fail("浏览器进程已退出")
        _update(8, "设置国家/支付方式")
        _setup_tokens_page(tab, country_code=country,
                           do_select_country=auto_select_country,
                           do_click_add_card=auto_click_add_card)

        try:
            tab.run_js(f'document.title = "#{task_id} ✅ 完成 - 请填写信用卡"')
        except Exception:
            pass

        status.success = True
        status.step_label = "✅ 注册成功"
        on_status(status)
        logger.info(f"[Task {task_id}] 注册完成: {email} / {first_name} {last_name}")
        return AccountResult(email=email, password=password, success=True, browser=browser)

    except Exception as e:
        logger.error(f"[Task {task_id}] 异常: {e}", exc_info=True)
        return _fail(str(e))
    # 注意：成功时不关浏览器、不清理数据，保留页面让用户查看/填卡


# ═══════════════════════════════════════════════════════════
#  扫描并连接系统中已运行的浏览器（Chrome / Edge）
# ═══════════════════════════════════════════════════════════

def scan_debug_browsers() -> list[dict]:
    """
    扫描系统中所有带 --remote-debugging-port 的 Chrome/Edge 主进程。
    返回 [{"pid": int, "port": int, "browser": "chrome"|"edge", "title": str, "url": str}, ...]
    """
    import subprocess
    results = []
    seen_ports = set()

    ps_script = (
        'Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\' or name=\'msedge.exe\'" '
        '| Select-Object ProcessId,Name,CommandLine '
        '| ForEach-Object { $_.ProcessId.ToString() + \"|\" + $_.Name + \"|\" + $_.CommandLine }'
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            timeout=15, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"[Scan] PowerShell 扫描失败: {e}")
        return results

    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue

        pid_str, proc_name, cmd_line = parts
        try:
            pid = int(pid_str)
        except ValueError:
            continue

        if not cmd_line:
            continue
        if "--type=" in cmd_line:
            continue

        m = re.search(r'--remote-debugging-port=(\d+)', cmd_line)
        if not m:
            continue

        port = int(m.group(1))
        if port in seen_ports:
            continue
        seen_ports.add(port)

        browser_name = "edge" if "msedge" in proc_name.lower() else "chrome"
        results.append({
            "pid": pid,
            "port": port,
            "browser": browser_name,
            "title": "",
            "url": "",
        })

    import httpx
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_page_info(info):
        try:
            transport = httpx.HTTPTransport(local_address="0.0.0.0")
            with httpx.Client(timeout=2, proxy=None, transport=transport) as client:
                resp = client.get(f"http://127.0.0.1:{info['port']}/json/list")
                pages = resp.json()
                for page in pages:
                    if page.get("type") == "page":
                        info["title"] = page.get("title", "")
                        info["url"] = page.get("url", "")
                        break
        except Exception:
            pass

    if results:
        with ThreadPoolExecutor(max_workers=min(len(results), 10)) as pool:
            futures = [pool.submit(_fetch_page_info, info) for info in results]
            try:
                for f in as_completed(futures, timeout=8):
                    try:
                        f.result()
                    except Exception:
                        pass
            except TimeoutError:
                logger.debug("[Scan] 部分浏览器 DevTools 查询超时，跳过")

    logger.info(f"[Scan] 扫描到 {len(results)} 个浏览器实例")
    return results


def connect_browser_by_port(port: int):
    """通过 debug 端口连接已有浏览器，返回 Chromium 实例"""
    browser = Chromium(f"127.0.0.1:{port}")
    return browser


def _open_single_browser(port: int, browser_type: str, url: str, max_retries: int = 2, fullscreen: bool = False) -> dict:
    """打开单个浏览器窗口（供并发调用），自动启用指纹（如果可用），带重试"""
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            co = ChromiumOptions()
            co.set_local_port(port)
            co.incognito()
            co.set_argument("--disable-popup-blocking")
            if fullscreen:
                co.set_argument("--start-maximized")

            fp_info = None
            use_fp = _is_fingerprint_enabled() and browser_type in ("fingerprint", "chrome", "edge")
            if browser_type == "fingerprint":
                use_fp = _is_fingerprint_enabled()
            if use_fp:
                fp_seed = random.randint(10_000_000, 2_000_000_000)
                co.set_browser_path(config.FINGERPRINT_BROWSER_PATH)
                fp_profile_dir = _BROWSER_DATA_DIR / f"fp_{port}_{fp_seed}"
                fp_profile_dir.mkdir(parents=True, exist_ok=True)
                co.set_user_data_path(str(fp_profile_dir))
                fp_args, fp_info = _make_fp_args(fp_seed)
                for arg in fp_args:
                    co.set_argument(arg)
            else:
                data_dir = _BROWSER_DATA_DIR / str(port)
                data_dir.mkdir(parents=True, exist_ok=True)
                co.set_user_data_path(str(data_dir))
                co.set_argument("--disable-blink-features=AutomationControlled")
                co.set_argument("--no-first-run")
                co.set_argument("--no-default-browser-check")
                co.set_argument("--lang=en-US")
                if browser_type != "chrome":
                    path = _find_browser_path(browser_type)
                    if path:
                        co.set_browser_path(path)

            browser = Chromium(co)
            tab = browser.latest_tab

            if url:
                tab.get(url)

            fp_msg = f" 指纹={fp_info['seed']}" if fp_info else ""
            logger.info(f"[OpenBrowser] 已打开浏览器 端口={port}{fp_msg}")
            return {"port": port, "ok": True, "message": f"已打开 (端口 {port}){fp_msg}"}

        except Exception as e:
            last_err = str(e)
            logger.warning(f"[OpenBrowser] 端口={port} 启动失败 (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(5)
                # 尝试直接连接（浏览器进程可能已启动但连接超时）
                try:
                    browser = Chromium(f"127.0.0.1:{port}")
                    tab = browser.latest_tab
                    if url:
                        tab.get(url)
                    logger.info(f"[OpenBrowser] 端口={port} 重连成功")
                    return {"port": port, "ok": True, "message": f"已打开 (端口 {port}, 重连)"}
                except Exception:
                    pass

    return {"port": port, "ok": False, "message": last_err}


def open_browsers(count: int = 1, browser_type: str = "chrome", url: str = "", fullscreen: bool = False) -> list[dict]:
    """
    批量打开带 debug 端口的浏览器窗口。
    逐个启动并等待，避免同时启动导致资源争抢和连接超时。
    """
    # 使用全局端口分配器（避免与注册任务的端口冲突）
    ports = [_alloc_port() for _ in range(count)]

    results = []
    for i, port in enumerate(ports):
        result = _open_single_browser(port, browser_type, url, fullscreen=fullscreen)
        results.append(result)
        # 错开启动：每个浏览器之间等 2 秒，让前一个完成初始化
        if i < len(ports) - 1:
            time.sleep(2)

    return results


# ═══════════════════════════════════════════════════════════
#  一键填卡：在已打开的浏览器中自动填写银行卡信息
# ═══════════════════════════════════════════════════════════

def fill_card_info(
    browser,
    card_number: str,
    expiry_date: str,
    cvv: str,
    card_name: str,
) -> dict:
    """
    在已打开的浏览器窗口中自动填写 JetBrains Add credit card 表单。
    """
    filled = {}

    try:
        tab = browser.latest_tab
        logger.info(f"[FillCard] 当前页面: {tab.url}")

        adyen_filled = _fill_adyen_iframes(tab, card_number, expiry_date, cvv)
        filled.update(adyen_filled)

        if not filled.get("card_number"):
            direct_filled = _fill_card_direct(tab, card_number, expiry_date, cvv)
            filled.update(direct_filled)

        name_filled = _fill_card_name(tab, card_name)
        filled.update(name_filled)

        success_count = sum(1 for v in filled.values() if v)
        total_fields = 4
        if success_count >= 3:
            return {"ok": True, "message": f"已填写 {success_count}/{total_fields} 个字段", "filled": filled}
        elif success_count > 0:
            return {"ok": True, "message": f"部分填写 {success_count}/{total_fields} 个字段，请检查", "filled": filled}
        else:
            return {"ok": False, "message": "未找到任何支付表单字段，请确认已打开 Add credit card 页面", "filled": filled}

    except Exception as e:
        logger.error(f"[FillCard] 异常: {e}", exc_info=True)
        return {"ok": False, "message": f"填写失败: {str(e)}", "filled": filled}


def _fill_adyen_iframes(tab, card_number: str, expiry_date: str, cvv: str) -> dict:
    """在 JetBrains/Adyen 的 iframe 中填写卡号、到期日、CVV。"""
    result = {"card_number": False, "expiry": False, "cvv": False}

    iframe_map = [
        ("@title=Iframe for card number", card_number, "card_number", "卡号"),
        ("@title=Iframe for expiry date", expiry_date, "expiry", "到期日"),
        ("@title=Iframe for security code", cvv, "cvv", "CVV"),
    ]

    for selector, value, key, label in iframe_map:
        if not value:
            continue
        try:
            frame = tab.get_frame(selector, timeout=3)
            if not frame:
                logger.warning(f"[FillCard] 未找到 {label} iframe: {selector}")
                continue

            inp = _find_input_in_frame(frame)
            if not inp:
                logger.warning(f"[FillCard] {label} iframe 内未找到可见 input")
                continue

            _type_into_input(inp, value, frame=frame)
            result[key] = True
            logger.info(f"[FillCard] 已填写{label} (Adyen iframe)")
            time.sleep(0.3)

        except Exception as e:
            logger.warning(f"[FillCard] 填写{label}异常: {e}")

    if not any(result.values()):
        logger.info("[FillCard] 精确匹配失败，尝试按顺序填写 Adyen iframe...")
        try:
            iframes = tab.eles("tag:iframe")
            adyen_iframes = [ifr for ifr in iframes
                             if "adyen" in (ifr.attr("src") or "").lower()
                             or "adyen" in (ifr.attr("class") or "").lower()]
            logger.info(f"[FillCard] 找到 {len(adyen_iframes)} 个 Adyen iframe")

            fields = [(card_number, "card_number"), (expiry_date, "expiry"), (cvv, "cvv")]
            for i, (value, key) in enumerate(fields):
                if i < len(adyen_iframes) and not result[key]:
                    try:
                        frame = tab.get_frame(adyen_iframes[i])
                        if frame:
                            inp = _find_input_in_frame(frame)
                            if inp:
                                _type_into_input(inp, value, frame=frame)
                                result[key] = True
                                logger.info(f"[FillCard] 已填写 {key} (顺序)")
                                time.sleep(0.3)
                    except Exception as e:
                        logger.debug(f"[FillCard] 顺序填写 {key} 异常: {e}")
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════
#  一键清空：清除 Adyen 表单所有字段
# ═══════════════════════════════════════════════════════════

def _clear_input_in_frame(frame, inp):
    """用 CDP Ctrl+A → Backspace 清空 Adyen iframe 内的输入框"""
    inp.click()
    time.sleep(0.2)
    frame.run_cdp('Input.dispatchKeyEvent', type='keyDown',
                  key='a', code='KeyA', windowsVirtualKeyCode=65, modifiers=2)
    frame.run_cdp('Input.dispatchKeyEvent', type='keyUp',
                  key='a', code='KeyA', windowsVirtualKeyCode=65, modifiers=2)
    time.sleep(0.1)
    frame.run_cdp('Input.dispatchKeyEvent', type='keyDown',
                  key='Backspace', code='Backspace', windowsVirtualKeyCode=8)
    frame.run_cdp('Input.dispatchKeyEvent', type='keyUp',
                  key='Backspace', code='Backspace', windowsVirtualKeyCode=8)
    time.sleep(0.3)


def clear_card_info(browser) -> dict:
    """清空 Add credit card 表单的所有字段"""
    cleared = {}
    try:
        tab = browser.latest_tab

        for selector, key, label in [
            ("@title=Iframe for card number", "card_number", "卡号"),
            ("@title=Iframe for expiry date", "expiry", "到期日"),
            ("@title=Iframe for security code", "cvv", "CVV"),
        ]:
            try:
                frame = tab.get_frame(selector, timeout=2)
                if frame:
                    inp = _find_input_in_frame(frame)
                    if inp:
                        _clear_input_in_frame(frame, inp)
                        cleared[key] = True
                        logger.info(f"[ClearCard] 已清空{label}")
            except Exception as e:
                logger.debug(f"[ClearCard] 清空{label}异常: {e}")
                cleared[key] = False

        try:
            name_inp = tab.ele("@name=holderName", timeout=1)
            if name_inp:
                name_inp.click()
                time.sleep(0.1)
                name_inp.clear()
                cleared["card_name"] = True
                logger.info("[ClearCard] 已清空持卡人姓名")
        except Exception:
            cleared["card_name"] = False

        success = sum(1 for v in cleared.values() if v)
        return {"ok": success > 0, "message": f"已清空 {success}/4 个字段", "cleared": cleared}
    except Exception as e:
        return {"ok": False, "message": f"清空失败: {str(e)}", "cleared": cleared}


# ═══════════════════════════════════════════════════════════
#  一键确认：点击蓝色 Confirm 按钮
# ═══════════════════════════════════════════════════════════

def confirm_card(browser) -> dict:
    """点击 Add credit card 页面的蓝色 Confirm 按钮"""
    try:
        tab = browser.latest_tab
        current_url = tab.url or ""
        logger.info(f"[ConfirmCard] 当前页面: {current_url}")

        btn = None
        try:
            btns = tab.eles("tag:button", timeout=3)
            for b in btns:
                cls = b.attr("class") or ""
                txt = (b.text or "").strip()
                if "add-credit-card" in cls and txt == "Confirm":
                    btn = b
                    break
        except Exception:
            pass

        if not btn:
            try:
                btn = tab.ele("text:Confirm", timeout=2)
            except Exception:
                pass

        if not btn:
            try:
                btn = tab.ele("@type=submit", timeout=2)
            except Exception:
                pass

        if not btn:
            return {"ok": False, "message": "未找到 Confirm 按钮"}

        btn.click()
        logger.info("[ConfirmCard] 已点击 Confirm 按钮")
        time.sleep(0.5)
        return {"ok": True, "message": "已点击 Confirm"}

    except Exception as e:
        logger.warning(f"[ConfirmCard] 异常: {e}")
        return {"ok": False, "message": f"点击失败: {str(e)}"}


def _fill_card_direct(tab, card_number: str, expiry_date: str, cvv: str) -> dict:
    """在主页面直接查找输入框填写（非 iframe 模式）"""
    result = {"card_number": False, "expiry": False, "cvv": False}

    for sel in ["input[name*='cardNumber']", "input[name*='card_number']",
                "input[placeholder*='Card number']", "input[placeholder*='card number']",
                "input[data-fieldtype='encryptedCardNumber']",
                "input[aria-label*='Card number']", "input[autocomplete='cc-number']"]:
        try:
            inp = tab.ele(sel, timeout=2)
            if inp:
                _type_into_input(inp, card_number)
                result["card_number"] = True
                logger.info(f"[FillCard] 已填写卡号 (直接: {sel})")
                break
        except Exception:
            pass

    for sel in ["input[name*='expiry']", "input[name*='Expiry']",
                "input[placeholder*='MM/YY']", "input[placeholder*='Expiry']",
                "input[data-fieldtype='encryptedExpiryDate']",
                "input[aria-label*='Expiry']", "input[autocomplete='cc-exp']"]:
        try:
            inp = tab.ele(sel, timeout=2)
            if inp:
                _type_into_input(inp, expiry_date)
                result["expiry"] = True
                logger.info(f"[FillCard] 已填写到期日 (直接: {sel})")
                break
        except Exception:
            pass

    for sel in ["input[name*='security']", "input[name*='cvv']", "input[name*='cvc']",
                "input[placeholder*='Security']", "input[placeholder*='CVV']", "input[placeholder*='CVC']",
                "input[data-fieldtype='encryptedSecurityCode']",
                "input[aria-label*='Security']", "input[autocomplete='cc-csc']"]:
        try:
            inp = tab.ele(sel, timeout=2)
            if inp:
                _type_into_input(inp, cvv)
                result["cvv"] = True
                logger.info(f"[FillCard] 已填写 CVV (直接: {sel})")
                break
        except Exception:
            pass

    return result


def _fill_card_name(tab, card_name: str) -> dict:
    """填写持卡人姓名"""
    result = {"card_name": False}

    for sel in ["@name=holderName", "@name=cardHolder",
                "@name=name_on_card", "@name=card-name",
                "@placeholder=Name on card",
                "@autocomplete=cc-name",
                "@aria-label=Name on card"]:
        try:
            inp = tab.ele(sel, timeout=0.5)
            if inp:
                _type_into_input(inp, card_name)
                result["card_name"] = True
                logger.info(f"[FillCard] 已填写持卡人姓名 (直接: {sel})")
                return result
        except Exception:
            pass

    try:
        label = tab.ele("text:Name on card", timeout=2)
        if label:
            parent = label.parent()
            if parent:
                inp = parent.ele("tag:input", timeout=2)
                if inp:
                    _type_into_input(inp, card_name)
                    result["card_name"] = True
                    logger.info("[FillCard] 已填写持卡人姓名 (label 关联)")
    except Exception:
        pass

    return result


def _find_input_in_frame(frame) -> object:
    """在 Adyen iframe 内查找真正的可见输入框"""
    try:
        inputs = frame.eles("tag:input", timeout=2)
        for inp in inputs:
            if inp.attr("aria-hidden") != "true" and inp.attr("type") != "hidden":
                return inp
    except Exception:
        pass
    return None


def _type_into_input(inp, value: str, frame=None):
    """
    Adyen 支付表单输入：点击激活 → CDP Input.insertText 一次性写入。
    """
    try:
        inp.click()
        time.sleep(0.3)
    except Exception:
        pass

    cdp_target = frame if frame else inp
    try:
        cdp_target.run_cdp('Input.insertText', text=value)
        time.sleep(0.5)
    except Exception:
        for ch in value:
            try:
                cdp_target.run_cdp('Input.dispatchKeyEvent', type='keyDown', text=ch)
                cdp_target.run_cdp('Input.dispatchKeyEvent', type='keyUp', text=ch)
                time.sleep(0.03)
            except Exception:
                try:
                    inp.input(ch)
                except Exception:
                    pass
        time.sleep(0.3)


# ═══════════════════════════════════════════════════════════
#  一键登录 + 检测绑卡状态
# ═══════════════════════════════════════════════════════════

LOGIN_URL = "https://account.jetbrains.com/login"
PAYMENT_METHODS_URL = "https://account.jetbrains.com/licenses/payment-methods"
ADD_CARD_URL = "https://account.jetbrains.com/licenses/tokens"


PROFILE_URL = "https://account.jetbrains.com/profile-details"


@dataclass
class LoginResult:
    """登录+检测结果"""
    email: str
    password: str
    login_ok: bool = False
    has_card: bool = False
    browser: object = None    # 保留浏览器实例
    port: int = 0             # debug 端口
    error: str = ""
    card_detail: str = ""     # 绑卡详情（如卡号末四位）
    country: str = ""         # 国家代码（如 JP, US）
    country_name: str = ""    # 国家名称（如 Japan）


def login_and_check(
    email: str,
    password: str,
    browser_type: str = "chrome",
    goto_card_page: bool = True,
    country: str = "JP",
    incognito: bool = True,
    fullscreen: bool = False,
) -> LoginResult:
    """
    一键登录已注册的 JetBrains 账号并检测绑卡状态。
    流程：
      1. 启动浏览器 → 导航到登录页
      2. 选择 "Continue with email" → 填写邮箱
      3. 填写密码 → 点击 Sign In
      4. 等待登录成功（检测页面跳转）
      5. 导航到 payment-methods 页 → 检测是否已绑卡
      6. 如果 goto_card_page=True 且未绑卡 → 跳转到 tokens 页面准备绑卡
    """
    result = LoginResult(email=email, password=password)
    browser = None
    data_dir = None

    try:
        # 1. 启动浏览器
        logger.info(f"[Login] 启动浏览器，登录 {email}...")
        browser, fp_info, data_dir = _create_browser(browser_type, incognito=incognito, fullscreen=fullscreen)
        tab = browser.latest_tab
        result.browser = browser

        # 获取 debug 端口
        try:
            result.port = browser.address.split(":")[-1]
            result.port = int(result.port)
        except Exception:
            result.port = 0

        # 设置窗口标题
        try:
            tab.run_js(f'document.title = "登录中: {email[:30]}..."')
        except Exception:
            pass

        # 2. 导航到登录页
        if not _safe_get(tab, LOGIN_URL, timeout=30):
            result.error = "登录页加载失败"
            return result
        time.sleep(2)

        # 处理 Cookie
        _inject_cookie_killer(tab)
        _dismiss_cookie_banner(tab)

        # 3. 点击 "Continue with email"
        try:
            btn = tab.ele("text:Continue with email", timeout=8)
            if btn:
                btn.click()
                time.sleep(1.5)
                logger.info("[Login] 已点击 Continue with email")
        except Exception:
            logger.info("[Login] 未找到 Continue with email，可能已在邮箱输入页")

        # 4. 填写邮箱
        email_input = None
        for selector in ["@name=email", "@placeholder=Email", "@type=email", "tag:input"]:
            try:
                email_input = tab.ele(selector, timeout=5)
                if email_input:
                    break
            except Exception:
                pass

        if not email_input:
            result.error = "未找到邮箱输入框"
            return result

        email_input.clear()
        email_input.input(email)
        time.sleep(0.5)

        # 点击 Continue/Submit
        try:
            tab.run_js("""
                const btn = document.querySelector('button[type="submit"]');
                if (btn) btn.click();
            """)
        except Exception:
            try:
                submit_btn = tab.ele("@type=submit", timeout=3)
                if submit_btn:
                    submit_btn.click()
            except Exception:
                pass

        time.sleep(2)

        # 5. 填写密码
        # 等待密码页面加载（快速检测，最多 ~12 秒）
        pwd_input = None
        for wait in range(8):
            try:
                pwd_input = tab.ele("@type=password", timeout=1.5)
                if pwd_input:
                    break
            except Exception:
                pass

            # 快速检测账号不存在 / 页面异常
            try:
                page_state = tab.run_js("""
                    const txt = (document.body.innerText || '').toLowerCase();
                    // 账号不存在
                    if (txt.includes('no account found') || txt.includes('account not found')
                        || txt.includes("we couldn't find") || txt.includes('does not exist')
                        || txt.includes('no user found') || txt.includes('user not found')
                        || txt.includes('not registered'))
                        return 'no_account';
                    // 需要邮箱验证码登录（passwordless / magic link）
                    if (txt.includes('check your email') || txt.includes('verification code')
                        || txt.includes('enter the code') || txt.includes('we sent'))
                        return 'otp_login';
                    // reCAPTCHA 出现
                    if (document.querySelector('iframe[title*="reCAPTCHA"]'))
                        return 'captcha';
                    return '';
                """)
                if page_state == 'no_account':
                    result.error = "账号不存在"
                    try:
                        tab.run_js(f'document.title = "✗ {email[:25]} - 账号不存在"')
                    except Exception:
                        pass
                    return result
                if page_state == 'otp_login':
                    result.error = "该账号需要邮箱验证码登录，无法自动登录"
                    try:
                        tab.run_js(f'document.title = "⚠ {email[:25]} - 需验证码登录"')
                    except Exception:
                        pass
                    return result
            except Exception:
                pass

            time.sleep(0.5)

        if not pwd_input:
            # 最后一次检查页面状态给出更精确的错误
            error_detail = "未找到密码输入框"
            try:
                page_text = tab.run_js("return (document.body.innerText || '').substring(0, 500)") or ""
                if "captcha" in page_text.lower() or "robot" in page_text.lower():
                    error_detail = "登录页需要人机验证，请手动完成"
                elif "check your" in page_text.lower() or "code" in page_text.lower():
                    error_detail = "该账号需要邮箱验证码登录"
                else:
                    error_detail = "账号可能不存在或页面异常"
            except Exception:
                pass
            result.error = error_detail
            try:
                tab.run_js(f'document.title = "✗ {email[:25]} - {error_detail[:20]}"')
            except Exception:
                pass
            return result

        pwd_input.clear()
        pwd_input.input(password)
        time.sleep(0.5)

        # 点击 Sign In
        for sign_try in range(3):
            try:
                tab.run_js("""
                    const form = document.querySelector('form');
                    if (form && typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                    } else {
                        const btn = document.querySelector('button[type="submit"]');
                        if (btn) btn.click();
                    }
                """)
            except Exception:
                try:
                    btn = tab.ele("@type=submit", timeout=2)
                    if btn:
                        btn.click()
                except Exception:
                    pass

            time.sleep(3)

            # 检查是否登录成功（不在登录页了）
            try:
                current_url = tab.url or ""
                if "/login" not in current_url and "signup" not in current_url:
                    logger.info(f"[Login] 登录成功，当前 URL: {current_url}")
                    result.login_ok = True
                    break
                # 检查是否有错误提示
                page_text = tab.run_js("return (document.body.innerText || '').toLowerCase()") or ""
                if "incorrect" in page_text or "invalid" in page_text or "wrong" in page_text:
                    result.error = "密码错误或账号不存在"
                    try:
                        tab.run_js(f'document.title = "✗ {email[:25]} - 密码错误"')
                    except Exception:
                        pass
                    return result
            except Exception:
                pass

        if not result.login_ok:
            # 最后再检查一次
            try:
                current_url = tab.url or ""
                if "/login" not in current_url and "signup" not in current_url:
                    result.login_ok = True
                else:
                    result.error = "登录超时（可能需要验证码或两步验证）"
                    try:
                        tab.run_js(f'document.title = "⚠ {email[:25]} - 需手动登录"')
                    except Exception:
                        pass
                    return result
            except Exception:
                result.error = "无法确认登录状态"
                return result

        # 6. 检测国家
        logger.info(f"[Login] {email} 登录成功，检测国家和绑卡状态...")
        try:
            tab.run_js(f'document.title = "检测中: {email[:25]}..."')
        except Exception:
            pass

        result.country, result.country_name = _check_country(tab)
        if result.country:
            logger.info(f"[Login] {email} 国家: {result.country_name} ({result.country})")

        # 7. 检测绑卡状态
        result.has_card, result.card_detail = _check_payment_methods(tab)

        # 7. 跳转到目标页面
        if goto_card_page:
            if result.has_card:
                # 已绑卡 → 停留在 payment-methods 让用户看
                try:
                    tab.run_js(f'document.title = "✓ {email[:20]} - 已绑卡 {result.card_detail}"')
                except Exception:
                    pass
                logger.info(f"[Login] {email} 已绑卡: {result.card_detail}")
            else:
                # 未绑卡 → 跳转到 tokens 页面准备绑卡
                logger.info(f"[Login] {email} 未绑卡，跳转到绑卡页...")
                _setup_tokens_page(tab, country_code=country)
                try:
                    tab.run_js(f'document.title = "✗ {email[:20]} - 未绑卡 - 请填卡"')
                except Exception:
                    pass
        else:
            try:
                status_text = "已绑卡" if result.has_card else "未绑卡"
                tab.run_js(f'document.title = "{email[:25]} - {status_text}"')
            except Exception:
                pass

        return result

    except Exception as e:
        logger.error(f"[Login] {email} 异常: {e}", exc_info=True)
        result.error = str(e)
        return result


def _check_country(tab) -> tuple[str, str]:
    """
    导航到 profile-details 页面，读取账号的 Country/Region。
    返回 (country_code, country_name)，如 ("JP", "Japan")。
    """
    try:
        tab.get(PROFILE_URL)
        tab.wait.doc_loaded(timeout=15)
    except Exception as e:
        logger.warning(f"[CheckCountry] 导航到 profile-details 失败: {e}")
        return "", ""

    time.sleep(2)

    try:
        result = tab.run_js("""
            // 方法1: 从 select[name="country"] 的 selected option 读取
            const sel = document.querySelector('select[name="country"]');
            if (sel) {
                const opt = sel.querySelector('option[selected]');
                if (opt) {
                    return JSON.stringify({code: opt.value, name: opt.textContent.trim()});
                }
                // 备选: selected index
                if (sel.selectedIndex >= 0) {
                    const o = sel.options[sel.selectedIndex];
                    return JSON.stringify({code: o.value, name: o.textContent.trim()});
                }
            }
            // 方法2: 从页面文本匹配 "国家名 (XX)" 格式
            const txt = document.body.textContent || '';
            const m = txt.match(/Country\\/Region[\\s\\S]*?([A-Z][a-zA-Z\\s().'-]+?)\\s*\\(([A-Z]{2})\\)/);
            if (m) {
                return JSON.stringify({code: m[2], name: m[1].trim()});
            }
            return '';
        """)

        if result:
            import json as _json
            data = _json.loads(result)
            code = data.get("code", "")
            name = data.get("name", "")
            if code:
                return code.upper(), name
    except Exception as e:
        logger.warning(f"[CheckCountry] 解析国家失败: {e}")

    return "", ""


def _check_payment_methods(tab, navigate: bool = True) -> tuple[bool, str]:
    """
    检测是否已绑卡。通过 tokens 页面（而非 payment-methods 空白假页面）。

    Args:
        tab: 浏览器标签页
        navigate: 是否导航到 tokens 页面。False 时只检测当前页面内容（用于后台监测，
                  避免打断用户正在操作的页面）
    返回 (has_card: bool, detail: str)
    """
    if navigate:
        try:
            tab.get(ADD_CARD_URL)
            tab.wait.doc_loaded(timeout=20)
        except Exception as e:
            logger.warning(f"[CheckCard] 导航到 tokens 页失败: {e}")
            return False, ""

        time.sleep(3)

        # 处理 Cookie 弹窗
        _inject_cookie_killer(tab)
        _dismiss_cookie_banner(tab)

        # 等待 tokens 页面 SPA 内容加载
        for wait_i in range(10):
            try:
                has_content = tab.run_js("""
                    const txt = document.body.textContent || '';
                    return txt.includes('Add credit card') || txt.includes('Saved Card')
                        || txt.includes('credit card') || txt.includes('Tokens')
                        || txt.includes('Select country');
                """)
                if has_content:
                    break
            except Exception:
                pass
            time.sleep(1.5)

    # 分析页面内容（tokens 页和 payment-methods 页通用的检测逻辑）
    try:
        result = tab.run_js("""
            const txt = document.body.textContent || '';
            const html = document.body.innerHTML || '';

            // 检测已绑卡: 卡号末四位 •••• 1234
            const savedCardMatch = html.match(/[•·*]{2,}\\s*(\\d{4})/);
            if (savedCardMatch) {
                return JSON.stringify({has_card: true, detail: '****' + savedCardMatch[1]});
            }

            // Saved Card 文字（tokens 页绑卡后会显示）
            if (/Saved Cards?/i.test(txt) && !/0\\s+Saved Cards/i.test(txt) && !/No saved cards/i.test(txt)) {
                return JSON.stringify({has_card: true, detail: '已绑卡'});
            }

            // savedCreditCard 元素
            if (html.includes('savedCreditCard') || html.includes('card-last-four')) {
                const lastFour = html.match(/last.*?four.*?(\\d{4})/i) || html.match(/card-last-four.*?(\\d{4})/);
                const detail = lastFour ? '****' + lastFour[1] : '已绑卡';
                return JSON.stringify({has_card: true, detail: detail});
            }

            // Visa/Mastercard 等品牌标志 + 数字
            if (/(?:Visa|Master|Mastercard|AmEx|American Express|Discover|JCB|UnionPay).*?\\d{4}/i.test(txt)) {
                const brand = txt.match(/(Visa|Master(?:card)?|AmEx|American Express|Discover|JCB|UnionPay)/i);
                const num = txt.match(/(?:Visa|Master|Mastercard|AmEx|American Express|Discover|JCB|UnionPay).*?(\\d{4})/i);
                const detail = (brand ? brand[1] + ' ' : '') + (num ? '****' + num[1] : '');
                return JSON.stringify({has_card: true, detail: detail.trim()});
            }

            // Adyen iframe 存在 = 正在填卡表单中（还没绑成功）
            if (html.includes('adyen') || document.querySelector('iframe[title*="card"]')) {
                return JSON.stringify({has_card: false, detail: ''});
            }

            // "Add credit card" 按钮存在 = 还没绑卡
            if (/Add credit card/i.test(txt)) {
                return JSON.stringify({has_card: false, detail: ''});
            }

            return JSON.stringify({has_card: false, detail: ''});
        """)

        if result:
            import json as _json
            data = _json.loads(result)
            return data.get("has_card", False), data.get("detail", "")
    except Exception as e:
        logger.warning(f"[CheckCard] 分析绑卡状态失败: {e}")

    return False, ""


def login_batch(
    accounts: list[dict],
    browser_type: str = "chrome",
    goto_card_page: bool = True,
    country: str = "JP",
    incognito: bool = True,
    fullscreen: bool = False,
    on_progress: Callable | None = None,
    max_workers: int = 0,
) -> list[LoginResult]:
    """
    批量一键登录并检测绑卡状态（并发执行，一次性打开所有浏览器）。
    accounts: [{"email": "...", "password": "..."}, ...]
    on_progress: 可选回调 (index, total, result) → void
    max_workers: 最大并发数。0 = 等于账号数（全部同时打开）
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(accounts)
    # 结果列表，按原索引存放
    results: list[LoginResult | None] = [None] * total
    # 并发数：默认全部同时，但上限 10 避免系统资源爆炸
    workers = max_workers if max_workers > 0 else min(total, 10)

    # 进度计数器（线程安全）
    _done_count = [0]
    _done_lock = threading.Lock()

    def _run_one(index: int, acc: dict) -> tuple[int, LoginResult]:
        email = acc.get("email", "")
        password = acc.get("password", "")

        if not email or not password:
            return index, LoginResult(email=email, password=password, error="邮箱或密码为空")

        logger.info(f"[LoginBatch] ({index+1}/{total}) 并发登录 {email}...")
        # 错开浏览器启动，避免同时创建进程导致资源竞争
        stagger = index * config.DELAY_BROWSER_STAGGER
        if stagger > 0:
            time.sleep(stagger)

        r = login_and_check(
            email=email,
            password=password,
            browser_type=browser_type,
            goto_card_page=goto_card_page,
            country=country,
            incognito=incognito,
            fullscreen=fullscreen,
        )
        return index, r

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_one, i, acc): i
            for i, acc in enumerate(accounts)
        }

        for future in as_completed(futures):
            try:
                idx, r = future.result()
            except Exception as e:
                idx = futures[future]
                acc = accounts[idx]
                r = LoginResult(
                    email=acc.get("email", ""),
                    password=acc.get("password", ""),
                    error=f"未捕获异常: {str(e)[:80]}",
                )

            results[idx] = r

            with _done_lock:
                _done_count[0] += 1

            if on_progress:
                on_progress(idx, total, r)

    # 清理 None（理论上不会有）
    return [r if r else LoginResult(email="?", password="?", error="未执行") for r in results]
