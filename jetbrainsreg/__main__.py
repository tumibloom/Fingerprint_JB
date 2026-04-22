"""允许 python -m jetbrainsreg 启动（包名保持 jetbrainsreg 兼容性）"""
if __name__ == "__main__":
    from .main import main
    main()
