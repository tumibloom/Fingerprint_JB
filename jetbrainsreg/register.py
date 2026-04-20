"""
JetBrainsReg 全自动注册流程（v2 — 集成指纹 + 全自动化）
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
    """
    rnd = random.Random(seed)
    plat, plat_ver = rnd.choice(config.FINGERPRINT_PLATFORMS)
    brand, brand_ver = rnd.choice(config.FINGERPRINT_BRANDS)
    tz = rnd.choice(config.FINGERPRINT_TIMEZONES)
    cpu = rnd.choice([2, 4, 6, 8, 12, 16])

    args = [
        f"--fingerprint={seed}",
        f"--fingerprint-platform={plat}",
        f"--fingerprint-brand={brand}",
        f"--fingerprint-hardware-concurrency={cpu}",
        f"--timezone={tz}",
        "--lang=en-US",
        "--accept-lang=en-US,en",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--test-type",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
        "--disable-session-crashed-bubble",
        "--disable-save-password-bubble",
    ]
    if plat_ver:
        args.append(f"--fingerprint-platform-version={plat_ver}")
    if brand_ver:
        args.append(f"--fingerprint-brand-version={brand_ver}")

    fp_info = {"seed": seed, "platform": plat, "brand": brand,
               "timezone": tz, "cpu": cpu}
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


def _safe_get(tab, url: str, timeout: float = 30) -> bool:
    """安全导航到 URL，超时/异常返回 False 但不抛异常"""
    try:
        tab.get(url)
        tab.wait.doc_loaded(timeout=timeout)
        return True
    except Exception as e:
        logger.warning(f"[SafeGet] 导航到 {url[:60]} 失败: {e}")
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


def _create_browser(browser_type: str = "chrome", fp_seed: int | None = None, max_retries: int = 3) -> tuple:
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

        co.incognito()
        co.set_argument("--disable-popup-blocking")

        try:
            browser = Chromium(co)
            logger.info(f"[Browser] 端口 {port} 浏览器启动成功 (attempt {attempt})")
            return browser, fp_info, data_dir
        except Exception as e:
            logger.warning(f"[Browser] 端口 {port} 启动失败 (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                # 浏览器进程可能已在后台启动但连接超时，等待后重试连接
                wait_sec = 5 * attempt  # 5s, 10s
                logger.info(f"[Browser] 等待 {wait_sec}s 后重试...")
                time.sleep(wait_sec)

                # 重试时尝试直接连接已启动的浏览器（不再启动新进程）
                try:
                    browser = Chromium(f"127.0.0.1:{port}")
                    logger.info(f"[Browser] 端口 {port} 重连成功")
                    return browser, fp_info, data_dir
                except Exception:
                    logger.debug(f"[Browser] 端口 {port} 重连也失败，继续重试")
                    # 清理可能残留的进程和数据
                    _cleanup_data_dir(data_dir)
                    data_dir = None
                    # 分配新端口重试
                    port = _alloc_port()
            else:
                raise  # 最后一次重试仍然失败，抛出异常


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
    try:
        btn = tab.ele("text:Continue with email", timeout=config.PAGE_TIMEOUT)
        if not btn:
            logger.warning("[Step 2] 未找到按钮，可能已在邮箱输入页")
            return True
        btn.click()
        time.sleep(config.DELAY_CLICK)
        logger.info("[Step 2] 已点击 Continue with email")
    except Exception as e:
        logger.warning(f"[Step 2] 异常（尝试继续）: {e}")
    return True


# ═══════════════════════════════════════════════════════════
#  Step 3: 填写邮箱（强化：Enter 键优先 + 重试）
# ═══════════════════════════════════════════════════════════

def _fill_email(tab, email: str) -> bool:
    """填写邮箱并点击 Continue（触发 reCAPTCHA 加载）"""
    logger.info(f"[Step 3] 填写邮箱: {email}")
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
        except Exception:
            pass

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

    anchor_frame = None
    for attempt in range(1, 7):
        logger.info(f"[Step 4] 查找 reCAPTCHA iframe（第 {attempt}/6 次）...")
        try:
            anchor_frame = tab.get_frame("@title=reCAPTCHA", timeout=10)
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

        logger.warning(f"[Step 4] 第 {attempt} 次未找到 iframe，等待...")
        time.sleep(2.5)

    if not anchor_frame:
        # reCAPTCHA 从未出现 — 可能 Continue 没触发，或网络问题
        # 不报错，回到主流程让 Step 5b 再尝试提交
        logger.warning("[Step 4] 未找到 reCAPTCHA iframe，可能 Continue 未生效，将尝试重新提交")
        return True  # 返回 True 让流程继续，Step 5b 会处理

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
    """
    if _captcha_is_done(tab):
        logger.info("[Step 5] reCAPTCHA 已通过（无需手动操作）")
        return True

    logger.info("[Step 5] 请在浏览器中手动完成验证码（无时限，慢慢来）...")
    poll_count = 0

    while True:
        poll_count += 1
        if cancel_flag and cancel_flag():
            logger.info("[Step 5] 收到取消信号，停止等待")
            return False
        if _captcha_is_done(tab):
            logger.info(f"[Step 5] 验证码已通过（轮询 {poll_count} 次）")
            return True
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


def _fill_verification_code(tab, email: str) -> bool:
    logger.info("[Step 6] 等待页面加载...")

    try:
        tab.wait.doc_loaded(timeout=15)
    except Exception:
        pass
    time.sleep(1.5)

    # 每次导航后重新注入 Cookie killer
    _inject_cookie_killer(tab)

    try:
        logger.info(f"[Step 6] 当前 URL: {tab.url}")
    except Exception:
        pass

    # 轮询邮件，同时支持链接和验证码
    logger.info(f"[Step 6] 开始轮询邮箱: {email}")
    deadline = time.time() + config.EMAIL_POLL_TIMEOUT
    attempt = 0
    mail_result = None
    seen_mail_ids = set()  # 避免重复处理已检查过的邮件

    while time.time() < deadline:
        attempt += 1
        try:
            mails = email_service.get_mails(email)
        except Exception as e:
            logger.warning(f"[Step 6] 第{attempt}次轮询网络错误: {e}")
            time.sleep(config.EMAIL_POLL_INTERVAL)
            continue

        if mails:
            for mail in mails:
                # 跳过已处理的邮件
                mail_id = mail.get("id", "")
                if mail_id and mail_id in seen_mail_ids:
                    continue
                if mail_id:
                    seen_mail_ids.add(mail_id)

                # 获取邮件内容（content 字段，由 get_mails 补充）
                content = mail.get("content", "") or mail.get("html", "") or mail.get("text", "")
                subject = mail.get("subject", "") or ""

                # 必须有实际内容才处理（避免 content 为空时误匹配 subject 中的数字）
                if not content or len(content.strip()) < 20:
                    logger.warning(f"[Step 6] 邮件 {mail_id} 内容为空或过短，跳过")
                    continue

                # 检查是否是 JetBrains 相关邮件
                full_text_lower = (subject + " " + content).lower()
                if "jetbrains" not in full_text_lower and "account" not in full_text_lower:
                    continue

                # 提取验证码/链接时只从 content 中提取（不从 subject 提取，避免误匹配）
                res = _extract_jb_link_or_code(content)
                if res:
                    mail_result = res
                    logger.info(f"[Step 6] 从邮件 {mail_id} (subject: {subject[:40]}) 提取到结果")
                    break

        if mail_result:
            break

        if attempt % 5 == 0:
            logger.info(f"[Step 6] 第{attempt}次轮询，尚未收到验证邮件... (已检查 {len(seen_mail_ids)} 封)")

        time.sleep(config.EMAIL_POLL_INTERVAL)

    if not mail_result:
        logger.error(f"[Step 6] 邮件轮询超时（{config.EMAIL_POLL_TIMEOUT}s），未收到验证")
        return False

    kind, payload = mail_result
    logger.info(f"[Step 6] 收到 {kind}: {str(payload)[:60]}")

    if kind == "LINK":
        # 验证链接模式：直接导航
        logger.info("[Step 6] 打开验证链接...")
        try:
            tab.get(payload)
            tab.wait.doc_loaded(timeout=30)
            time.sleep(3)
        except Exception as e:
            logger.warning(f"[Step 6] 打开链接失败: {e}")
        return True

    # OTP 码模式
    code = str(payload).strip()
    logger.info(f"[Step 6] 获取到验证码: {code}")
    time.sleep(1)

    # 强制 DOM 重排，解决 React SPA 渲染延迟导致元素不可见的问题
    # （打开 F12 能触发填入就是因为 DevTools 强制了重排）
    _force_dom_reflow(tab)

    # 等 OTP 输入框出现（多种选择器，JetBrains 用 name="otp-1" ~ "otp-6"）
    otp_found = False
    for wait_round in range(15):
        # 方式1：JetBrains 专用 otp-N 输入框
        try:
            first_otp = tab.ele("@name=otp-1", timeout=2)
            if first_otp:
                otp_found = True
                break
        except Exception:
            pass
        # 方式2：maxlength=1 的单字符输入框
        try:
            code_inputs = tab.eles("input[maxlength='1']", timeout=2)
            if code_inputs and len(code_inputs) >= 4:
                otp_found = True
                break
        except Exception:
            pass
        # 每隔几轮强制重排一次
        if wait_round % 3 == 2:
            _force_dom_reflow(tab)
        time.sleep(1)

    if not otp_found:
        logger.warning("[Step 6] OTP 输入框未出现，尝试强制重排后再找...")
        _force_dom_reflow(tab)
        time.sleep(1)

    # ── 填入方式1：JetBrains otp-1 ~ otp-6（CDP 键盘逐字符输入，最可靠）──
    filled_ok = _fill_otp_by_name(tab, code)

    # ── 填入方式2：maxlength=1 输入框组 ──
    if not filled_ok:
        filled_ok = _fill_otp_by_maxlength(tab, code)

    # ── 填入方式3：单输入框 ──
    if not filled_ok:
        filled_ok = _fill_otp_single_input(tab, code)

    # ── 填入方式4：兜底 — 找所有可见 input ──
    if not filled_ok:
        filled_ok = _fill_otp_fallback(tab, code)

    if not filled_ok:
        logger.error("[Step 6] 未找到验证码输入框")
        return False

    time.sleep(2)
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


def _setup_tokens_page(tab, country_code: str = "JP") -> bool:
    """注册成功后：导航到 tokens 页 → 选国家 → 点 Add credit card"""
    logger.info(f"[Step 8] 跳转到 tokens 页（国家={country_code}）...")

    try:
        tab.get(TOKENS_URL)
        tab.wait.doc_loaded(timeout=30)
    except Exception as e:
        logger.warning(f"[Step 8] 导航失败: {e}")
    time.sleep(3)

    _inject_cookie_killer(tab)
    _dismiss_cookie_banner(tab)

    # 等待页面 SPA 内容真正渲染完成（tokens 页是 SPA 异步加载）
    _wait_tokens_page_ready(tab)

    # 第一步：点击 "Select country" 链接
    try:
        select_link = (
            tab.ele("text:Select country", timeout=5)
            or tab.ele("text:Select", timeout=3)
        )
        if select_link:
            select_link.click()
            logger.info("[Step 8] 已点击 Select country")
            time.sleep(2)
    except Exception as e:
        logger.info(f"[Step 8] 未找到 Select country（可能已设置）: {e}")

    # 第二步：选国家（带重试 — 弹窗/select 可能需要时间渲染）
    # 从 select 元素动态获取国家名（无需本地维护完整映射）
    selected = 'no_select'
    cc = country_code.upper()
    for country_attempt in range(5):
        try:
            selected = tab.run_js(f"""
                const sel = document.querySelector('select[name="country"]');
                if (!sel) return 'no_select';
                sel.value = '{cc}';
                sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                // 从 option 中获取国家全名
                const opt = sel.querySelector('option[value="{cc}"]');
                const name = opt ? opt.textContent.trim() : '{cc}';
                const chosenSpan = document.querySelector('.chosen-single span');
                if (chosenSpan) chosenSpan.textContent = name;
                try {{
                    if (window.jQuery) {{
                        jQuery('select[name="country"]').val('{cc}').trigger('chosen:updated').trigger('change');
                    }}
                }} catch(e) {{}}
                return sel.value;
            """)
            if selected and selected != 'no_select':
                break
        except Exception:
            pass
        if country_attempt < 4:
            logger.info(f"[Step 8] select 元素未就绪，等待重试 ({country_attempt + 1}/5)...")
            time.sleep(2)

    logger.info(f"[Step 8] 国家选择结果: {selected}")

    if selected != cc:
        # 通过 Chosen UI 下拉搜索选择（获取国家名用于搜索）
        country_name = _get_country_name(cc)
        try:
            chosen = tab.ele(".chosen-container .chosen-single", timeout=3)
            if chosen:
                chosen.click()
                time.sleep(0.5)
                search = tab.ele(".chosen-container .chosen-search input", timeout=3)
                if search:
                    search.input(country_name)
                    time.sleep(0.5)
                    result_item = tab.ele(".chosen-results li", timeout=3)
                    if result_item and country_name.lower() in (result_item.text or "").lower():
                        result_item.click()
                        logger.info(f"[Step 8] 通过 Chosen UI 选择了 {country_name}")
                        time.sleep(0.5)
        except Exception as e:
            logger.warning(f"[Step 8] Chosen UI 操作失败: {e}")

    # 第三步：点击 Save
    save_clicked = False
    try:
        # 精确匹配模态框内的 Save 按钮
        save_btn = tab.ele("text:Save", timeout=5)
        if save_btn:
            save_btn.click()
            save_clicked = True
            logger.info("[Step 8] 已点击 Save")
            time.sleep(3)
    except Exception:
        pass

    if not save_clicked:
        # JS 兜底
        try:
            tab.run_js("""
                const modals = document.querySelectorAll('.modal');
                for (const m of modals) {
                    if (m.classList.contains('in') || m.style.display === 'block' || getComputedStyle(m).display !== 'none') {
                        const btn = m.querySelector('button.btn-primary');
                        if (btn && btn.textContent.trim() === 'Save') { btn.click(); return; }
                    }
                }
                const allSave = document.querySelectorAll('button.btn-primary');
                for (const b of allSave) {
                    if (b.textContent.trim() === 'Save' && b.offsetParent !== null) { b.click(); return; }
                }
            """)
            logger.info("[Step 8] JS 兜底点击 Save")
            time.sleep(2)
        except Exception as e:
            logger.warning(f"[Step 8] 点击 Save 失败: {e}")

    time.sleep(1.5)

    # 第四步：点击 "Add credit card"
    try:
        tab.wait.doc_loaded(timeout=15)
        time.sleep(1.5)

        for attempt in range(10):
            try:
                add_card = tab.ele("text:Add credit card", timeout=2)
                if add_card:
                    add_card.click()
                    logger.info("[Step 8] 已点击 Add credit card")
                    time.sleep(2)
                    return True
            except Exception:
                pass
            time.sleep(1.5)

        # JS 兜底
        tab.run_js("""
            const links = document.querySelectorAll('a, button');
            for (const l of links) {
                if ((l.textContent||'').trim().includes('Add credit card')) { l.click(); return; }
            }
        """)
        logger.info("[Step 8] JS 兜底点击 Add credit card")
        time.sleep(2)
        return True
    except Exception as e:
        logger.warning(f"[Step 8] 点击 Add credit card 失败: {e}")
        return False


def _wait_tokens_page_ready(tab, timeout: int = 20):
    """
    等待 tokens 页面 SPA 内容真正加载完成。
    tokens 页通过 JS 异步渲染，doc_loaded 完成不代表内容已就绪。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # 检查是否有 tokens 页面特征元素
            has_content = tab.run_js("""
                return !!(
                    document.querySelector('select[name="country"]')
                    || document.querySelector('.chosen-container')
                    || document.querySelector('a[href*="credit"]')
                    || (document.body.textContent || '').includes('Select country')
                    || (document.body.textContent || '').includes('Add credit card')
                    || (document.body.textContent || '').includes('Tokens')
                );
            """)
            if has_content:
                logger.info("[Step 8] tokens 页面内容已就绪")
                return
        except Exception:
            pass
        time.sleep(1.5)
    logger.warning("[Step 8] tokens 页面内容加载超时，继续尝试操作")


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
        browser, fp_info, data_dir = _create_browser(browser_type, fp_seed=fp_seed)
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

        # Step 1
        _update(1, "Cookie 弹窗")
        if not _handle_cookie_consent(tab):
            return _fail("Cookie 弹窗处理失败")

        # Step 2
        if _is_cancelled():
            return _fail("用户停止了任务")
        _update(2, "Continue with email")
        if not _click_continue_with_email(tab):
            return _fail("未找到 Continue with email")

        # Step 3
        if _is_cancelled():
            return _fail("用户停止了任务")
        _update(3, "填写邮箱")
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

        # Step 5 — 等待用户手动完成验证码（如果需要）
        if not _has_left_email_page(tab) and not _captcha_is_done(tab):
            _update(5, "⏳ 请手动完成验证码")
            if not _wait_for_manual_captcha(tab, cancel_flag=_is_cancelled):
                if _is_cancelled():
                    return _fail("用户停止了任务")
                return _fail("验证码超时（用户未操作）")

        # Step 5b — 验证码完成后，自动点击 Continue 提交表单
        if not _has_left_email_page(tab):
            _update(5, "自动提交...")
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
        if not _fill_verification_code(tab, email):
            return _fail("填写验证码失败")

        # Step 7
        if _is_cancelled():
            return _fail("用户停止了任务")
        if not _check_browser_alive():
            return _fail("浏览器进程已退出")
        _update(7, "填写密码并注册")
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
        _setup_tokens_page(tab, country_code=country)

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


def _open_single_browser(port: int, browser_type: str, url: str, max_retries: int = 2) -> dict:
    """打开单个浏览器窗口（供并发调用），自动启用指纹（如果可用），带重试"""
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            co = ChromiumOptions()
            co.set_local_port(port)
            co.incognito()
            co.set_argument("--disable-popup-blocking")

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


def open_browsers(count: int = 1, browser_type: str = "chrome", url: str = "") -> list[dict]:
    """
    批量打开带 debug 端口的浏览器窗口。
    逐个启动并等待，避免同时启动导致资源争抢和连接超时。
    """
    # 使用全局端口分配器（避免与注册任务的端口冲突）
    ports = [_alloc_port() for _ in range(count)]

    results = []
    for i, port in enumerate(ports):
        result = _open_single_browser(port, browser_type, url)
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
