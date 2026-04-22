"""FingerprintReg 配置文件"""
import json
from pathlib import Path

# ── JetBrains 注册页 ──
SIGNUP_URL = "https://account.jetbrains.com/signup"

# ── YYDS Mail 临时邮箱 (替代已被风控的 nimail.cn) ──
YYDS_API_BASE = "https://maliapi.215.im/v1"
YYDS_API_KEY = ""  # 运行时从 settings.json 加载，用户在 UI 中配置

# ── AI 验证码识别（通过反代调用 Gemini 3 Flash） ──
AI_API_BASE = ""  # 填入你的 AI 反代地址，如 https://api.example.com/v1
AI_API_KEY = ""   # 填入你的 AI API Key
AI_MODEL = "google-gemini-3-0-flash"
AI_CAPTCHA_ENABLED = True       # 是否启用 AI 自动验证码（False 则纯手动）

# ── 打码平台（YesCaptcha / CapSolver 等） ──
CAPTCHA_PLATFORM = ""           # "yescaptcha" / "capsolver" / ""(禁用)
CAPTCHA_CLIENT_KEY = ""         # 打码平台的 API Key (clientKey)
YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"  # 国际节点，国内可换 cn.yescaptcha.com
CAPSOLVER_API_BASE = "https://api.capsolver.com"

# ── 注册参数 ──
DEFAULT_PASSWORD = "YourPassword123"
DEFAULT_FIRST_NAME = "JetBrains"
DEFAULT_LAST_NAME = "User"

# ── 超时（秒） ──
EMAIL_POLL_INTERVAL = 3         # 邮件轮询间隔
EMAIL_POLL_TIMEOUT = 600        # 邮件等待超时（10 分钟，梯子慢时邮件可能延迟）
PAGE_TIMEOUT = 30               # 页面操作超时

# ── 用户可调延迟（秒） ──
# 这些值可通过 Web 面板实时修改，注册流程中读取最新值
DELAY_CLICK = 2.0          # 通用点击后等待（推荐 1.5~3）
DELAY_INPUT = 0.5           # 输入框填写后等待（推荐 0.3~1）
DELAY_PAGE_NAV = 3.0        # 页面跳转后等待（推荐 2~5）
DELAY_CAPTCHA_POLL = 2.0    # 人机验证轮询间隔（推荐 1.5~3）
DELAY_OTP_CHAR = 0.15       # 验证码逐字符输入间隔（推荐 0.08~0.3）
DELAY_STEP_TRANSITION = 2.0 # 步骤切换间等待（推荐 1~3）
DELAY_BROWSER_STAGGER = 2.0 # 批量启动浏览器间隔（推荐 1~4）

# ── HTTP 客户端（绕过 TUN 虚拟网卡） ──
HTTP_LOCAL_ADDRESS = "0.0.0.0"  # 强制本地直连，防止梯子 TUN 劫持

# ── 指纹浏览器（fingerprint-chromium） ──
# 默认路径：项目文件夹内的 Chromium/Application/chrome.exe
# 也支持绝对路径。留空或 None 则使用普通 Chrome/Edge（无指纹）
import os as _os
FINGERPRINT_BROWSER_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "Chromium", "Application", "chrome.exe"
)

# 指纹伪装的平台/品牌/时区/语言/内存候选池
FINGERPRINT_PLATFORMS = [
    ("windows", "10.0.0"), ("windows", "11.0.0"),
    ("macos", "14.5.0"), ("macos", "15.2.0"), ("linux", None),
]
FINGERPRINT_BRANDS = [("Chrome", None), ("Edge", None), ("Opera", None), ("Vivaldi", None)]
FINGERPRINT_TIMEZONES = [
    "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore",
    "America/Los_Angeles", "America/New_York", "Europe/London", "Europe/Berlin",
]
FINGERPRINT_LANGUAGES = [
    ("en-US", "en-US,en"),
    ("en-GB", "en-GB,en"),
    ("ja-JP", "ja-JP,ja,en"),
    ("de-DE", "de-DE,de,en"),
    ("fr-FR", "fr-FR,fr,en"),
    ("es-ES", "es-ES,es,en"),
    ("zh-CN", "zh-CN,zh,en"),
    ("ko-KR", "ko-KR,ko,en"),
    ("pt-BR", "pt-BR,pt,en"),
    ("it-IT", "it-IT,it,en"),
]
FINGERPRINT_MEMORY_SIZES = [2, 4, 8, 16, 32]  # deviceMemory (GB)
FINGERPRINT_CPU_CORES = [2, 4, 6, 8, 12, 16]

# ── 指纹功能开关（默认全部启用） ──
# 每个开关控制是否在启动时注入对应的指纹参数
# 用户可在 Web 面板「设置」中自由切换
FINGERPRINT_TOGGLES = {
    "fp_enabled":        True,   # 总开关：是否启用指纹伪装
    "fp_platform":       True,   # 伪装操作系统平台
    "fp_brand":          True,   # 伪装浏览器品牌 (User-Agent)
    "fp_timezone":       True,   # 伪装时区
    "fp_language":       True,   # 伪装语言 (--lang / --accept-lang)
    "fp_cpu":            True,   # 伪装 CPU 核心数
    "fp_memory":         True,   # 伪装内存大小 (navigator.deviceMemory)
    "fp_webrtc":         True,   # WebRTC 策略 (--disable-non-proxied-udp)
    "fp_canvas":         True,   # Canvas 指纹（不 disable = 启用伪装）
    "fp_audio":          True,   # Audio 指纹
    "fp_font":           True,   # 字体指纹
    "fp_clientrects":    True,   # ClientRects 指纹
    "fp_gpu":            True,   # GPU/WebGL 指纹
    "fp_webdriver":      True,   # 隐藏 navigator.webdriver
    "fp_automation":     True,   # 反自动化检测 (CDP/Shadow DOM)
}

# ── 持久化设置文件 ──
_PROJECT_DIR = Path(__file__).parent.parent
SETTINGS_FILE = _PROJECT_DIR / "output" / "settings.json"


def _load_settings_data() -> dict:
    """从 settings.json 加载全部数据"""
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def _save_settings_data(data: dict):
    """保存全部数据到 settings.json"""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_api_key() -> str:
    """从 settings.json 加载 API Key"""
    global YYDS_API_KEY
    data = _load_settings_data()
    YYDS_API_KEY = data.get("yyds_api_key", "")
    return YYDS_API_KEY


def save_api_key(key: str):
    """保存 API Key 到 settings.json"""
    global YYDS_API_KEY
    YYDS_API_KEY = key
    data = _load_settings_data()
    data["yyds_api_key"] = key
    _save_settings_data(data)


def load_fingerprint_toggles():
    """从 settings.json 加载指纹功能开关，合并到 FINGERPRINT_TOGGLES"""
    data = _load_settings_data()
    saved = data.get("fingerprint_toggles", {})
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in FINGERPRINT_TOGGLES:
                FINGERPRINT_TOGGLES[k] = bool(v)


def save_fingerprint_toggles(toggles: dict):
    """保存指纹功能开关到 settings.json 并更新运行时配置"""
    for k, v in toggles.items():
        if k in FINGERPRINT_TOGGLES:
            FINGERPRINT_TOGGLES[k] = bool(v)
    data = _load_settings_data()
    data["fingerprint_toggles"] = dict(FINGERPRINT_TOGGLES)
    _save_settings_data(data)


def get_fingerprint_toggles() -> dict:
    """获取当前指纹功能开关状态"""
    return dict(FINGERPRINT_TOGGLES)


def load_captcha_config():
    """从 settings.json 加载打码平台配置"""
    global CAPTCHA_PLATFORM, CAPTCHA_CLIENT_KEY
    data = _load_settings_data()
    CAPTCHA_PLATFORM = data.get("captcha_platform", "")
    CAPTCHA_CLIENT_KEY = data.get("captcha_client_key", "")


def save_captcha_config(platform: str, client_key: str):
    """保存打码平台配置到 settings.json"""
    global CAPTCHA_PLATFORM, CAPTCHA_CLIENT_KEY
    CAPTCHA_PLATFORM = platform
    CAPTCHA_CLIENT_KEY = client_key
    data = _load_settings_data()
    data["captcha_platform"] = platform
    data["captcha_client_key"] = client_key
    _save_settings_data(data)


# 启动时自动加载
load_api_key()
load_fingerprint_toggles()
load_captcha_config()
