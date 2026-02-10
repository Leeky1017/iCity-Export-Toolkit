# iCity 导出脚本使用手册（Windows）

这份手册给非技术用户，按步骤做即可。

## 1. 打开 PowerShell

方式 A（推荐）：
1. 按 `Win` 键。
2. 输入 `PowerShell`。
3. 点击 `Windows PowerShell` 打开。

方式 B：
1. 在任意文件夹空白处按住 `Shift` + 鼠标右键。
2. 选择“在此处打开 PowerShell 窗口”（或“在终端中打开”）。

## 2. 确认电脑有 Python

在 PowerShell 输入：

```powershell
py --version
```

如果看到类似 `Python 3.10.x`、`3.11.x`、`3.12.x` 就可以继续。

如果提示找不到 `py`：
1. 打开 https://www.python.org/downloads/windows/
2. 下载并安装 Python 3
3. 安装时勾选 `Add Python to PATH`
4. 安装后重新打开 PowerShell 再执行 `py --version`

## 3. 进入脚本所在文件夹

假设脚本在桌面：

```powershell
cd $HOME\Desktop
```

如果脚本在其他目录，就改成你的实际路径。

## 4. 运行脚本（推荐方式）

```powershell
py icity_export.py
```

说明：
- 脚本会一步步提问：账号、密码、目标用户、导出目录、文件前缀、是否生成按日 Markdown。
- 输入密码时不会显示字符，这是正常的。
- 如果某一步直接回车，会使用该步给出的默认值。

## 5. 首次运行会自动安装依赖

你不需要手动安装 `requests` 或 `beautifulsoup4`。

首次运行脚本会自动：
1. 在脚本目录创建 `.icity_export_venv`
2. 自动下载依赖
3. 自动重启脚本继续执行

首次通常要 1-3 分钟，取决于网络。

## 6. 导出结果在哪里

运行完成后会显示三类结果：
- `xxx.json`（结构化数据）
- `xxx.txt`（可直接阅读的文本）
- `xxx_md/`（按 `年/月/日` 拆分的 Markdown 文件夹）

默认会在你指定的 `--output-dir` 里。

## 7. 常见问题

### Q1: 提示网络错误
检查网络后重试。

### Q2: 提示登录失败
确认账号密码是否正确；若账号触发额外验证（如 2FA），需要先在网页端完成验证。

### Q3: 脚本窗口一闪而过
不要双击运行 `.py` 文件，要在 PowerShell 里按手册命令执行。
