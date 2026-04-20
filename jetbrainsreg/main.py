"""
JetBrainsReg 启动入口
用法:
    python -m jetbrainsreg            # 启动 Web 控制面板（默认端口 7860）
    python -m jetbrainsreg --port 8080
"""
import argparse
import logging
import sys
import threading
import time
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser(
        description="JetBrainsReg — JetBrains 账号半自动注册机",
    )
    parser.add_argument(
        "--port", type=int, default=7860,
        help="Web 控制面板端口 (默认: 7860)",
    )
    parser.add_argument(
        "--host", type=str, default="127.0.0.1",
        help="监听地址 (默认: 127.0.0.1)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="启动后不自动打开浏览器",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="显示详细日志",
    )

    args = parser.parse_args()

    # 切换工作目录到项目根目录（这样无论从哪里启动，output/ 等相对路径都正确）
    import os
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(project_dir)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    url = f"http://{args.host}:{args.port}"

    print()
    print("=" * 52)
    print("  JetBrainsReg — Account Semi-Auto Registration")
    print("=" * 52)
    print(f"  Dashboard: {url}")
    print(f"  Flow: Auto email -> You solve captcha -> Auto verify+register")
    print("=" * 52)
    print()

    # 等服务器就绪后再打开浏览器（轮询探测，避免 404）
    if not args.no_browser:
        def _open():
            import urllib.request
            for attempt in range(60):  # 最多等 30 秒
                time.sleep(0.5)
                try:
                    resp = urllib.request.urlopen(url, timeout=2)
                    if resp.status == 200:
                        webbrowser.open(url)
                        return
                except Exception:
                    pass
            # 超时兜底：仍然打开（可能只是响应慢）
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        "jetbrainsreg.server:app",
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
