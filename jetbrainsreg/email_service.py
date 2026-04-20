"""
nimail.cn 临时邮箱服务
功能：申请临时邮箱 + 轮询获取 JetBrains 验证码
"""
import random
import re
import string
import time
import logging

import httpx

from . import config

logger = logging.getLogger("jetbrainsreg.email")

# HTTP 请求配置（线程安全：每次调用创建新 Client）
_HEADERS = {
    "accept": "application/json",
    "accept-language": "zh-CN,zh",
    "origin": "https://www.nimail.cn",
}


def _new_client() -> httpx.Client:
    """创建新的 httpx Client（线程安全，避免并发请求冲突）"""
    transport = httpx.HTTPTransport(local_address=config.HTTP_LOCAL_ADDRESS)
    return httpx.Client(timeout=30, proxy=None, transport=transport, headers=_HEADERS)


def _random_username(length: int = 10) -> str:
    """生成随机邮箱用户名"""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def apply_email(username: str | None = None, max_retries: int = 3) -> str:
    """
    申请一个 nimail.cn 临时邮箱（带重试，防网络波动）。
    """
    for attempt in range(1, max_retries + 1):
        try:
            if username is None:
                uname = _random_username()
            else:
                uname = username

            email = f"{uname}@{config.NIMAIL_DOMAIN}"

            client = _new_client()
            resp = client.post(
                config.NIMAIL_APPLY_URL,
                data={"mail": email},
            )
            resp.raise_for_status()

            data = resp.json()
            if str(data.get("success", "")).lower() == "true":
                actual_email = data.get("user", email)
                logger.info(f"申请邮箱成功: {actual_email}")
                return actual_email

            raise RuntimeError(f"申请邮箱失败: {data}")

        except Exception as e:
            logger.warning(f"申请邮箱第 {attempt} 次失败: {e}")
            if attempt < max_retries:
                time.sleep(3)
            else:
                raise RuntimeError(f"申请邮箱 {max_retries} 次均失败: {e}")


def get_mails(email: str) -> list[dict]:
    """
    获取指定邮箱的所有邮件（含内容）。
    nimail.cn 的邮件列表 API 只返回元数据，内容需要用 raw-html API 单独获取。
    """
    timestamp = int(time.time() * 1000)
    client = _new_client()
    resp = client.post(
        config.NIMAIL_GET_URL,
        data={
            "mail": email,
            "time": "0",
            "_": str(timestamp),
        },
    )
    resp.raise_for_status()

    data = resp.json()
    # 返回格式: {"to": "...", "mail": [...], "success": "true"}
    if isinstance(data, list):
        mail_list = data
    elif isinstance(data, dict):
        mail_list = data.get("mail", data.get("mails", data.get("data", [])))
    else:
        mail_list = []

    # 补充邮件内容（通过 raw-html API）
    for mail in mail_list:
        if "content" not in mail and mail.get("id"):
            try:
                content = _fetch_mail_content(email, mail["id"])
                mail["content"] = content
            except Exception as e:
                logger.warning(f"获取邮件内容失败: {e}")

    return mail_list


def _fetch_mail_content(email: str, mail_id: str, max_retries: int = 3) -> str:
    """通过 raw-html API 获取邮件 HTML 内容（带重试，防 VPN 抖动）"""
    url = f"https://www.nimail.cn/api/raw-html/{email}/{mail_id}"
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            client = _new_client()
            resp = client.get(url)
            resp.raise_for_status()
            content = resp.text
            if content and len(content.strip()) > 10:
                return content
            logger.warning(f"获取邮件内容返回空内容 (attempt {attempt})")
        except Exception as e:
            last_err = e
            logger.warning(f"获取邮件内容失败 (attempt {attempt}): {e}")
            if attempt < max_retries:
                time.sleep(2)
    raise RuntimeError(f"获取邮件内容 {max_retries} 次均失败: {last_err}")


def extract_verification_code(mail_content: str) -> str | None:
    """
    从邮件内容中提取 JetBrains 验证码（6位数字）。
    JetBrains 邮件格式: <span style="font-size: 24px; font-weight: bold; ...">361169</span>

    注意：严格按优先级匹配，避免误匹配 CSS 颜色代码、时间戳等数字。
    """
    # 高置信度模式（JetBrains 专用格式）
    high_confidence_patterns = [
        # JetBrains 特征: 大号加粗 span 里的 6 位数字（最可靠）
        r'font-weight:\s*bold[^>]*>\s*(\d{6})\s*<',
        r'font-size:\s*2[0-9]px[^>]*>\s*(\d{6})\s*<',
        # "code" / "verification" 相关文字紧跟 6 位数字
        r'(?:verification\s*code|confirm.*?code|your\s*code)\s*(?:is|:)?\s*[:\s]*(\d{6})',
    ]

    for pattern in high_confidence_patterns:
        match = re.search(pattern, mail_content, re.IGNORECASE)
        if match:
            code = match.group(1)
            logger.info(f"[ExtractCode] 高置信度匹配: {code} (pattern: {pattern[:40]})")
            return code

    # 中置信度：HTML 标签内独立的 6 位数字（排除 style 属性、颜色代码等）
    # 只匹配 ><数字>< 模式，且前面不是 style= 或 color: 等属性
    mid_patterns = [
        # 排除 style 属性中的数字：只匹配标签内容区域的数字
        r'>[\s\n]*(\d{6})[\s\n]*</(?:span|div|p|td|strong|b)',
    ]
    for pattern in mid_patterns:
        match = re.search(pattern, mail_content, re.IGNORECASE)
        if match:
            candidate = match.group(1)
            # 排除常见的非验证码数字（如 000000, 123456 这种不太可能是真码的）
            if candidate not in ('000000', '123456'):
                logger.info(f"[ExtractCode] 中置信度匹配: {candidate}")
                return candidate

    # 低置信度兜底：从纯文本中找独立的 6 位数字
    clean = re.sub(r'<style[^>]*>.*?</style>', '', mail_content, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)           # 去 HTML 标签
    clean = re.sub(r'#[0-9a-fA-F]{3,8}\b', '', clean)  # 去颜色代码 (#fff, #000000, #ffffffcc)
    clean = re.sub(r'\b\d{7,}\b', '', clean)          # 去超长数字（时间戳、ID 等）
    clean = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '', clean)  # 去 IP 地址
    clean = re.sub(r'(?:19|20)\d{2}[-/]\d{2}[-/]\d{2}', '', clean)    # 去日期
    match = re.search(r'(?<!\d)(\d{6})(?!\d)', clean)
    if match:
        candidate = match.group(1)
        if candidate not in ('000000',):
            logger.info(f"[ExtractCode] 兜底匹配: {candidate}")
            return candidate

    return None


def poll_verification_code(
    email: str,
    timeout: float | None = None,
    interval: float | None = None,
) -> str:
    """
    轮询等待验证码邮件到达，提取并返回验证码。
    
    Args:
        email: 邮箱地址
        timeout: 超时秒数，默认用 config.EMAIL_POLL_TIMEOUT
        interval: 轮询间隔秒数，默认用 config.EMAIL_POLL_INTERVAL
        
    Returns:
        6位数字验证码
        
    Raises:
        TimeoutError: 超时未收到验证码
    """
    if timeout is None:
        timeout = config.EMAIL_POLL_TIMEOUT
    if interval is None:
        interval = config.EMAIL_POLL_INTERVAL
    
    deadline = time.time() + timeout
    attempt = 0
    
    logger.info(f"开始轮询验证码: {email} (超时={timeout}s, 间隔={interval}s)")
    
    while time.time() < deadline:
        attempt += 1
        try:
            mails = get_mails(email)
        except Exception as e:
            logger.warning(f"第{attempt}次轮询网络错误（自动重试）: {e}")
            time.sleep(interval)
            continue
        
        if mails:
            # 遍历所有邮件，找 JetBrains 的验证码
            for mail in mails:
                content = mail.get("content", "") or mail.get("html", "") or mail.get("text", "")
                subject = mail.get("subject", "") or ""
                
                # 优先检查 JetBrains 相关邮件
                full_text = subject + " " + content
                if "jetbrains" in full_text.lower() or "account" in full_text.lower():
                    code = extract_verification_code(full_text)
                    if code:
                        logger.info(f"获取验证码成功: {code} (第{attempt}次轮询)")
                        return code
            
            # 如果没找到 JetBrains 邮件，也在所有邮件中找验证码
            for mail in mails:
                content = mail.get("content", "") or mail.get("html", "") or mail.get("text", "")
                code = extract_verification_code(content)
                if code:
                    logger.info(f"获取验证码成功: {code} (第{attempt}次轮询, 非JB邮件)")
                    return code
        
        logger.debug(f"第{attempt}次轮询，未收到验证码，等待 {interval}s...")
        time.sleep(interval)
    
    raise TimeoutError(f"等待验证码超时 ({timeout}s): {email}")


# ── 模块测试入口 ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(message)s")
    
    print("=== nimail.cn 邮箱服务测试 ===\n")
    
    # 1. 申请邮箱
    email = apply_email()
    print(f"[1] 申请邮箱: {email}")
    
    # 2. 查询邮件（此时应该为空）
    mails = get_mails(email)
    print(f"[2] 当前邮件数: {len(mails)}")
    
    # 3. 提示手动测试
    print(f"\n[3] 测试完成。如需测试验证码轮询，请手动向 {email} 发送含 6 位数字的邮件，")
    print("    然后运行: python -m jetbrainsreg.email_service --poll <email>")
