# JetBrainsReg — JetBrains 账号半自动注册机

一键批量注册 JetBrains 账号。自动填邮箱、自动接验证码、自动填密码，你只需要手动过一下图片验证码。

## 功能

- 自动申请临时邮箱
- 自动打开浏览器填写注册信息
- 自动点击 "I'm not a robot"
- **你手动完成图片验证码**（唯一需要人工的步骤，无时间限制）
- 自动接收邮箱验证码并填入
- 自动填写密码和姓名，完成注册
- 支持同时开多个窗口并行注册（最多10个）
- 一键填卡：批量给已注册账号绑定银行卡
- 智能识别：粘贴卡片信息自动解析卡号/CVV/有效期
- Web 控制面板实时显示进度，4 种主题可选
- 延迟参数可调，适应不同网络环境
- 注册成功的账号永久保存，关掉再开还在

## 下载

点击上方绿色的 **Code** 按钮 → **Download ZIP** → 解压到任意位置即可。

## 使用前准备

| 需要准备 | 说明 | 下载地址 |
|---|---|---|
| **Python 3.10+** | 运行环境，安装时必须勾选 "Add Python to PATH" | https://www.python.org/downloads/ |
| **Chrome 或 Edge** | 浏览器，Edge 是 Windows 自带的不用额外装 | Chrome: https://www.google.com/chrome/ |
| **梯子/VPN** | 中国大陆用户需要，用来访问 Google 验证码和 JetBrains | — |

> Python 依赖（DrissionPage、FastAPI 等）不需要单独下载，一条命令自动安装。

## 快速开始

**1.** 解压下载的 ZIP 文件

**2.** 打开解压后的文件夹，在地址栏输入 `cmd` 回车，打开命令行

**3.** 安装依赖（只需要第一次）：
```
pip install -r requirements.txt
```

**4.** 启动程序：
```
python -m jetbrainsreg
```

**5.** 浏览器自动打开控制面板 → 设置密码和窗口数 → 点「开始注册」→ 手动过验证码 → 完成！

详细步骤请看压缩包里的 **使用教程.txt**。

## 注意事项

- 密码**只能用英文字母和数字**，不要加 @#$ 等特殊符号，否则注册失败
- 注册成功后浏览器窗口会保留不关闭，方便你查看
- CMD 窗口是后台服务，注册过程中不要关
- 注册结果保存在 `output/` 文件夹，`accounts.csv` 可以用 Excel 打开
- 右上角齿轮按钮可以调整主题和延迟参数

## 项目结构

```
JetBrainsReg/
├── 使用教程.txt             ← 详细使用教程
├── 启动.bat                 ← 双击启动
├── requirements.txt         ← Python 依赖
├── jetbrainsreg/            ← 主程序
│   ├── main.py              ← 启动入口
│   ├── server.py            ← Web 控制面板后端
│   ├── register.py          ← 注册流程（8步自动化）
│   ├── email_service.py     ← 临时邮箱 API
│   ├── config.py            ← 配置（含可调延迟参数）
│   └── static/index.html    ← 控制面板网页
└── output/                  ← 注册结果（自动生成）
```

## 技术栈

Python 3.10+ / DrissionPage / FastAPI / WebSocket / nimail.cn

## 免责声明

本项目仅供学习和研究自动化技术使用。使用者应遵守 JetBrains 的服务条款和相关法律法规。作者不对因使用本工具造成的任何后果承担责任。
