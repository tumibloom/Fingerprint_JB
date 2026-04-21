"""
YYDS Mail (vip.215.im) 临时邮箱服务
功能：申请临时邮箱 + 轮询获取 JetBrains 验证码
替代已被风控的 nimail.cn
"""
import random
import re
import string
import time
import logging

import httpx

from . import config

logger = logging.getLogger("jetbrainsreg.email")


def _new_client() -> httpx.Client:
    """创建新的 httpx Client（线程安全，避免并发请求冲突）
    不再强制 local_address / proxy=None，让请求走系统默认网络（跟随梯子代理）。
    旧版 nimail.cn 是国内站需要绕过梯子直连，YYDS Mail 在 Cloudflare 上需要走梯子。
    """
    return httpx.Client(timeout=30)


def _random_local_part(length: int = 10) -> str:
    """生成随机邮箱用户名"""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _get_api_key() -> str:
    """获取 API Key，如果为空则尝试重新加载"""
    if not config.YYDS_API_KEY:
        config.load_api_key()
    if not config.YYDS_API_KEY:
        raise RuntimeError(
            "YYDS Mail API Key 未配置！请在控制面板中设置 API Key。\n"
            "获取方式：访问 https://vip.215.im/api-keys 注册并创建 API Key"
        )
    return config.YYDS_API_KEY


_domain_cache: list[str] = []
_domain_cache_time: float = 0
_DOMAIN_CACHE_TTL = 600  # 域名列表缓存 10 分钟


def _pick_random_domain() -> str:
    """从 YYDS Mail 获取域名列表，随机选一个（带缓存，10 分钟刷新）"""
    global _domain_cache, _domain_cache_time

    if _domain_cache and (time.time() - _domain_cache_time) < _DOMAIN_CACHE_TTL:
        return random.choice(_domain_cache)

    api_key = _get_api_key()
    client = _new_client()
    try:
        resp = client.get(
            f"{config.YYDS_API_BASE}/domains",
            headers={"X-API-Key": api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        domains = data.get("data", [])
        if not domains:
            raise RuntimeError("YYDS Mail 没有可用域名")

        # 过滤健康的域名（MX 有效）
        healthy = [d for d in domains if d.get("isMxValid")]
        if healthy:
            domains = healthy

        _domain_cache = [d["domain"] for d in domains]
        _domain_cache_time = time.time()
        logger.info(f"域名列表已缓存: {len(_domain_cache)} 个健康域名")

        return random.choice(_domain_cache)
    finally:
        client.close()


def apply_email(username: str | None = None, max_retries: int = 3) -> str:
    """
    通过 YYDS Mail API 申请一个临时邮箱（带重试，防网络波动）。
    返回完整邮箱地址。同时将 account_id 和 token 存入模块级变量供后续使用。
    """
    for attempt in range(1, max_retries + 1):
        try:
            api_key = _get_api_key()
            local_part = username or _random_local_part()
            domain = _pick_random_domain()

            client = _new_client()
            try:
                resp = client.post(
                    f"{config.YYDS_API_BASE}/accounts",
                    headers={
                        "X-API-Key": api_key,
                        "Content-Type": "application/json",
                    },
                    json={"localPart": local_part, "domain": domain},
                )
                resp.raise_for_status()
                result = resp.json()
            finally:
                client.close()

            if not result.get("success"):
                raise RuntimeError(f"API 返回失败: {result}")

            account = result["data"]
            email = account["address"]
            account_id = account["id"]
            token = account["token"]

            # 存入模块级缓存供 get_mails / delete 使用
            _account_cache[email] = {
                "id": account_id,
                "token": token,
                "domain": domain,
            }

            logger.info(f"申请邮箱成功: {email} (域名: {domain})")
            return email

        except Exception as e:
            logger.warning(f"申请邮箱第 {attempt} 次失败: {e}")
            if attempt < max_retries:
                time.sleep(3)
            else:
                raise RuntimeError(f"申请邮箱 {max_retries} 次均失败: {e}")


# 模块级缓存：email → {id, token, domain}
_account_cache: dict[str, dict] = {}


def get_mails(email: str) -> list[dict]:
    """
    获取指定邮箱的所有邮件（含完整正文）。
    YYDS Mail 的 GET /v1/messages 列表接口不含正文，
    需要对每封邮件调 GET /v1/messages/{id} 获取 html/text。
    """
    cache = _account_cache.get(email)
    if not cache:
        raise RuntimeError(f"邮箱 {email} 未通过 apply_email 创建，无法获取邮件")

    token = cache["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. 获取邮件列表
    client = _new_client()
    try:
        resp = client.get(
            f"{config.YYDS_API_BASE}/messages",
            params={"address": email},
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json()
    finally:
        client.close()

    messages = result.get("data", {}).get("messages", [])

    # 2. 对每封邮件获取完整内容（html/text）
    for msg in messages:
        msg_id = msg.get("id")
        if msg_id and "content" not in msg:
            try:
                detail = _fetch_message_detail(msg_id, email, token)
                # html 可能是数组，拼成字符串
                html = detail.get("html", "")
                if isinstance(html, list):
                    html = "".join(html)
                text = detail.get("text", "")
                msg["html"] = html
                msg["text"] = text
                msg["content"] = html or text
            except Exception as e:
                logger.warning(f"获取邮件详情失败 {msg_id}: {e}")
                msg["content"] = ""

    return messages


def _fetch_message_detail(msg_id: str, email: str, token: str) -> dict:
    """获取单封邮件完整详情（含 html/text 正文）"""
    client = _new_client()
    try:
        resp = client.get(
            f"{config.YYDS_API_BASE}/messages/{msg_id}",
            params={"address": email},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("data", {})
    finally:
        client.close()


def delete_email(email: str) -> bool:
    """删除邮箱释放名额（用完即删）"""
    cache = _account_cache.get(email)
    if not cache:
        return False

    try:
        client = _new_client()
        try:
            resp = client.delete(
                f"{config.YYDS_API_BASE}/accounts/{cache['id']}",
                headers={"Authorization": f"Bearer {cache['token']}"},
            )
            if resp.status_code in (200, 204):
                logger.info(f"删除邮箱成功: {email}")
                _account_cache.pop(email, None)
                return True
            else:
                logger.warning(f"删除邮箱返回 {resp.status_code}: {email}")
                return False
        finally:
            client.close()
    except Exception as e:
        logger.warning(f"删除邮箱失败: {email} - {e}")
        return False


def extract_verification_code(mail_content: str) -> str | None:
    """
    从邮件内容中提取 JetBrains 验证码（6位数字）。
    JetBrains 邮件格式: <span style="font-size: 24px; font-weight: bold; ...">361169</span>
    """
    # 高置信度模式（JetBrains 专用格式）
    high_confidence_patterns = [
        r'font-weight:\s*bold[^>]*>\s*(\d{6})\s*<',
        r'font-size:\s*2[0-9]px[^>]*>\s*(\d{6})\s*<',
        r'(?:verification\s*code|confirm.*?code|your\s*code)\s*(?:is|:)?\s*[:\s]*(\d{6})',
    ]

    for pattern in high_confidence_patterns:
        match = re.search(pattern, mail_content, re.IGNORECASE)
        if match:
            code = match.group(1)
            logger.info(f"[ExtractCode] 高置信度匹配: {code} (pattern: {pattern[:40]})")
            return code

    # 中置信度：HTML 标签内独立的 6 位数字
    mid_patterns = [
        r'>[\s\n]*(\d{6})[\s\n]*</(?:span|div|p|td|strong|b)',
    ]
    for pattern in mid_patterns:
        match = re.search(pattern, mail_content, re.IGNORECASE)
        if match:
            candidate = match.group(1)
            if candidate not in ('000000', '123456'):
                logger.info(f"[ExtractCode] 中置信度匹配: {candidate}")
                return candidate

    # 低置信度兜底：从纯文本中找独立的 6 位数字
    clean = re.sub(r'<style[^>]*>.*?</style>', '', mail_content, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = re.sub(r'#[0-9a-fA-F]{3,8}\b', '', clean)
    clean = re.sub(r'\b\d{7,}\b', '', clean)
    clean = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '', clean)
    clean = re.sub(r'(?:19|20)\d{2}[-/]\d{2}[-/]\d{2}', '', clean)
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

    print("=== YYDS Mail 邮箱服务测试 ===\n")

    # 1. 申请邮箱
    email = apply_email()
    print(f"[1] 申请邮箱: {email}")

    # 2. 查询邮件（此时应该为空）
    mails = get_mails(email)
    print(f"[2] 当前邮件数: {len(mails)}")

    # 3. 删除邮箱
    ok = delete_email(email)
    print(f"[3] 删除邮箱: {'成功' if ok else '失败'}")

    print(f"\n[4] 测试完成。")
