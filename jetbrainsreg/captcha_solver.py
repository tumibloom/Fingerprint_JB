"""
AI captcha solver - CDP screenshot + coordinate click
（移植自 baiqi-GhostReg，适配 FingerprintReg 配置体系）
用 CDP 截整页截图 → 发给 AI 视觉模型 → 返回要点击的坐标 → tab.actions 点击
"""
import base64
import logging
import re
import time as _time

import httpx
from . import config

logger = logging.getLogger("jetbrainsreg.captcha")


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, proxy=None,
        transport=httpx.HTTPTransport(local_address=config.HTTP_LOCAL_ADDRESS))


PROMPT = "Image coords: top-left(0,0) bottom-right(1000,1000). This screenshot shows a reCAPTCHA challenge. Click all grid cells matching the prompt text. Output ONLY coordinates as: [(x1,y1),(x2,y2),(x3,y3)]"


def solve_click(image_bytes: bytes) -> list[tuple[int, int]]:
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": config.AI_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": PROMPT},
        ]}],
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 800,
    }

    for attempt in range(3):
        try:
            c = _client()
            try:
                r = c.post(f"{config.AI_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {config.AI_API_KEY}",
                             "Content-Type": "application/json"},
                    json=payload)
            finally:
                c.close()

            if r.status_code != 200:
                logger.error(f"[AI] HTTP {r.status_code}")
                return []

            text = r.json()["choices"][0]["message"]["content"].strip()
            logger.info(f"[AI] resp({len(text)}ch): {text[:300]}")

            pairs = re.findall(r'\((\d{2,3})\s*,\s*(\d{2,3})\)', text)
            coords = [(int(x), int(y)) for x, y in pairs if 10 < int(x) < 990 and 10 < int(y) < 990]
            if coords:
                logger.info(f"[AI] {len(coords)} coords: {coords}")
                return coords

            logger.warning(f"[AI] no valid coords parsed")
            return []

        except Exception as e:
            logger.warning(f"[AI] attempt {attempt+1}: {e}")
            if attempt < 2:
                _time.sleep(1)
    return []
