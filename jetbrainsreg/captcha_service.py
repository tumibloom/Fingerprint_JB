"""
打码平台接入层 — 支持 YesCaptcha / CapSolver（API 接口几乎相同）
核心流程：createTask → 轮询 getTaskResult → 返回 gRecaptchaResponse token
（移植自 baiqi-GhostReg，适配 FingerprintReg 配置体系）
"""
import logging
import ssl
import time

import httpx

from . import config

logger = logging.getLogger("jetbrainsreg.captcha_service")

# ── 超时 & 轮询配置 ──
CREATE_TIMEOUT = 30          # createTask 请求超时
POLL_INTERVAL = 3            # 轮询间隔（秒）
POLL_TIMEOUT = 120           # 轮询总超时（秒）


class CaptchaServiceError(Exception):
    """打码平台错误"""
    pass


# YesCaptcha 双节点：国际 + 国内（自动回退）
_YESCAPTCHA_ENDPOINTS = [
    "https://api.yescaptcha.com",
    "https://cn.yescaptcha.com",
]


def _get_api_bases() -> list[str]:
    """根据配置的平台返回 API base URL 列表（多节点自动回退）"""
    p = config.CAPTCHA_PLATFORM.lower()
    if p == "yescaptcha":
        custom = config.YESCAPTCHA_API_BASE.rstrip("/")
        # 把用户配置的放第一，再加其他节点做回退
        bases = [custom]
        for ep in _YESCAPTCHA_ENDPOINTS:
            if ep.rstrip("/") != custom:
                bases.append(ep)
        return bases
    elif p == "capsolver":
        return [config.CAPSOLVER_API_BASE.rstrip("/")]
    raise CaptchaServiceError(f"未知打码平台: {config.CAPTCHA_PLATFORM}")


def _http_post(url: str, json_data: dict, timeout: int = CREATE_TIMEOUT) -> dict:
    """带 SSL 容错的 HTTP POST（梯子 TUN 模式下 SSL 可能异常）"""
    # 先正常尝试
    try:
        resp = httpx.post(url, json=json_data, timeout=timeout)
        return resp.json()
    except (httpx.ConnectError, ssl.SSLError):
        pass

    # SSL 失败 → 尝试 verify=False
    try:
        resp = httpx.post(url, json=json_data, timeout=timeout, verify=False)
        return resp.json()
    except Exception as e:
        raise CaptchaServiceError(f"HTTP 请求失败: {url} → {e}")


def _post_with_fallback(path: str, json_data: dict, timeout: int = CREATE_TIMEOUT) -> dict:
    """多节点回退 POST"""
    bases = _get_api_bases()
    last_err = None
    for base in bases:
        url = f"{base}{path}"
        try:
            return _http_post(url, json_data, timeout)
        except CaptchaServiceError as e:
            last_err = e
            logger.warning(f"[CaptchaService] {base} 失败: {e}，尝试下一个节点...")
            continue
    raise last_err or CaptchaServiceError("所有节点均失败")


def is_enabled() -> bool:
    """打码平台是否已配置且可用"""
    return bool(config.CAPTCHA_PLATFORM and config.CAPTCHA_CLIENT_KEY)


def get_balance() -> float:
    """查询余额/积分"""
    if not is_enabled():
        raise CaptchaServiceError("打码平台未配置")

    data = _post_with_fallback("/getBalance", {"clientKey": config.CAPTCHA_CLIENT_KEY})
    if data.get("errorId", 0) != 0:
        raise CaptchaServiceError(
            f"查询余额失败: {data.get('errorCode')} - {data.get('errorDescription')}"
        )
    return data.get("balance", 0)


def solve_recaptcha_v2(website_url: str, website_key: str) -> str:
    """
    提交 reCAPTCHA v2 任务并等待结果。
    返回 gRecaptchaResponse token（字符串）。
    失败抛出 CaptchaServiceError。
    """
    if not is_enabled():
        raise CaptchaServiceError("打码平台未配置")

    client_key = config.CAPTCHA_CLIENT_KEY
    platform = config.CAPTCHA_PLATFORM.lower()

    # ── Step 1: createTask ──
    task_type = "NoCaptchaTaskProxyless"  # 通用类型，YesCaptcha 和 CapSolver 均支持
    if platform == "capsolver":
        task_type = "ReCaptchaV2TaskProxyLess"

    payload = {
        "clientKey": client_key,
        "task": {
            "type": task_type,
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
    }
    logger.info(f"[CaptchaService] createTask (type={task_type})")

    try:
        data = _post_with_fallback("/createTask", payload)
    except Exception as e:
        raise CaptchaServiceError(f"createTask 请求失败: {e}")

    if data.get("errorId", 0) != 0:
        raise CaptchaServiceError(
            f"createTask 错误: {data.get('errorCode')} - {data.get('errorDescription')}"
        )

    task_id = data.get("taskId")
    if not task_id:
        raise CaptchaServiceError(f"createTask 未返回 taskId: {data}")

    logger.info(f"[CaptchaService] taskId={task_id}，开始轮询...")

    # ── Step 2: 轮询 getTaskResult ──
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > POLL_TIMEOUT:
            raise CaptchaServiceError(f"轮询超时 ({POLL_TIMEOUT}s)")

        time.sleep(POLL_INTERVAL)

        try:
            result = _post_with_fallback(
                "/getTaskResult",
                {"clientKey": client_key, "taskId": task_id},
            )
        except Exception as e:
            logger.warning(f"[CaptchaService] 轮询请求失败: {e}")
            continue

        if result.get("errorId", 0) != 0:
            err_code = result.get("errorCode", "")
            # 某些错误是致命的
            if err_code in ("ERROR_CAPTCHA_UNSOLVABLE", "ERROR_TOKEN_EXPIRED",
                            "ERROR_KEY_DOES_NOT_EXIST", "ERROR_ZERO_BALANCE"):
                raise CaptchaServiceError(
                    f"致命错误: {err_code} - {result.get('errorDescription')}"
                )
            logger.warning(f"[CaptchaService] 轮询错误: {err_code}")
            continue

        status = result.get("status", "")
        if status == "ready":
            solution = result.get("solution", {})
            token = solution.get("gRecaptchaResponse", "")
            if token:
                logger.info(f"[CaptchaService] 获得 token（{len(token)} 字符，耗时 {elapsed:.1f}s）")
                return token
            raise CaptchaServiceError(f"solution 中无 gRecaptchaResponse: {solution}")

        if status == "processing":
            logger.debug(f"[CaptchaService] 识别中... ({elapsed:.0f}s)")
            continue

        # 未知状态
        logger.warning(f"[CaptchaService] 未知状态: {status}")
