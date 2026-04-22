"""
FingerprintReg 启动入口
用法:
    python -m jetbrainsreg            # 启动 Web 控制面板（默认端口 7777）
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
        description="FingerprintReg — Fingerprint 账号注册机（半自动/全自动）",
    )
    parser.add_argument(
        "--port", type=int, default=7777,
        help="Web 控制面板端口 (默认: 7777)",
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
    print("=" * 60)
    print("  FingerprintReg — Fingerprint 账号注册机")
    print("  半自动 or 全自动，独立指纹，批量注册")
    print("=" * 60)
    print(f"  控制面板: {url}")
    print(f"  模式: 全自动验证码(打码平台+AI) / 半自动(手动过验证码)")
    print("-" * 60)
    print("  使用攻略:")
    print("    1. 首次使用请先在面板顶部填入 YYDS Mail API Key")
    print("    2. 设置密码 → 选浏览器 → 选窗口数 → 点「开始注册」")
    print("    3. 勾选「全自动验证码」可实现完全无人值守")
    print("    4. 注册完成后可使用「一键填卡」批量绑卡")
    print("=" * 60)
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

    try:
        uvicorn.run(
            "jetbrainsreg.server:app",
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        print("\n已手动停止服务器。")
    except OSError as e:
        if "address already in use" in str(e).lower() or "只允许使用一次" in str(e):
            print(f"\n错误: 端口 {args.port} 已被占用，请尝试 --port 指定其他端口")
        else:
            print(f"\n启动失败: {e}")
        sys.exit(1)
    except Exception as e:
        logging.getLogger("jetbrainsreg").error(f"服务器异常退出: {e}", exc_info=True)
        print(f"\n服务器异常退出: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
