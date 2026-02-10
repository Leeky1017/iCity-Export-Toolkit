# iCity Export Toolkit

一个面向非技术用户的 iCity 日记导出工具。

## 功能

- 自动安装依赖（首次运行自动创建本地虚拟环境）
- 交互式输入账号和密码（密码隐藏输入）
- 导出 `JSON` 与 `TXT`
- 可按 `年/月/日` 自动拆分为 Markdown 文件

## 快速开始

### macOS

```bash
python3 icity_export.py
```

详细步骤见：`README_macOS.md`

### Windows

```powershell
py icity_export.py
```

详细步骤见：`README_Windows.md`

## 输出结果

运行完成后通常会得到：

- `xxx.json`：结构化数据
- `xxx.txt`：纯文本导出
- `xxx_md/`：按天拆分的 Markdown 目录

## 目录说明

- `icity_export.py`：主脚本
- `README_macOS.md`：macOS 操作手册
- `README_Windows.md`：Windows 操作手册

## 说明

本仓库仅包含工具脚本与说明文档，不包含任何个人日记数据。
