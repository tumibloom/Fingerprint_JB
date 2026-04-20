"""JetBrainsReg 配置文件"""

# ── JetBrains 注册页 ──
SIGNUP_URL = "https://account.jetbrains.com/signup"

# ── nimail.cn 临时邮箱 ──
NIMAIL_APPLY_URL = "https://www.nimail.cn/api/applymail"
NIMAIL_GET_URL = "https://www.nimail.cn/api/getmails"
NIMAIL_DOMAIN = "nimail.cn"

# ── 注册参数 ──
DEFAULT_PASSWORD = "hajimi123"
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

# 指纹伪装的平台/品牌/时区候选池
FINGERPRINT_PLATFORMS = [
    ("windows", "10.0.0"), ("windows", "11.0.0"),
    ("macos", "14.5.0"), ("macos", "15.2.0"), ("linux", None),
]
FINGERPRINT_BRANDS = [("Chrome", None), ("Edge", None), ("Opera", None), ("Vivaldi", None)]
FINGERPRINT_TIMEZONES = [
    "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore",
    "America/Los_Angeles", "America/New_York", "Europe/London", "Europe/Berlin",
]
