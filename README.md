# iCity Export Toolkit

把自己在 iCity 的日记文字备份到电脑。默认读取已登录账户的普通日记和封存日记，输出 JSON、TXT、按日期拆分的 Markdown，并保留原始分页和核对报告。

## 开始使用

需要 **Python 3.10 或更新版本**。下载并解压整个仓库，保留两个 `.py` 文件在同一目录：

```bash
# macOS / Linux
python3 icity_export.py

# Windows PowerShell
py icity_export.py
```

第一次运行会自动准备依赖。按提示输入账号和密码；密码隐藏输入，不需要写进脚本或配置文件。默认原生模式能从登录响应识别自己的用户 ID，邮箱也可以用于登录。

[Windows 使用说明](README_Windows.md) · [macOS 使用说明](README_macOS.md)

## 这次修复了什么

旧版本通过不断增加 `?page=N`、直到遇到空页为止来判断结束。网站可能反复返回末页，而不是返回空页，旧版本因此无限读取同一批记录、把重复项累加到进度中。

现在默认使用手机端的认证历史接口，遵循服务器的 `has_more` 标记；网页备用模式则遵循“加载更多”箭头实际指向的地址。两种方式都会检查重复、按日记 ID 实时去重。原生协议定义有更新时会协商版本，但未来网站修改接口仍可能导致导出失败。

## 输出内容

默认在 `export/` 下生成：

- `icity_diary_export.json`：日记文字、时间、隐私/封存标记、原始日记字段和图片元数据。
- `icity_diary_export.txt`：按时间从旧到新排列的阅读版。
- `icity_diary_export_md/`：按年/月/日拆分的 Markdown；无法识别日期的记录保存在 `_undated.md`，不会丢弃。
- `icity_diary_export_raw_native/`：每一页成功读取的原始数据，便于恢复和核验。
- `icity_diary_export.report.json`：导出状态、唯一记录数、各集合的结束标记和计数差异。

**这是文字备份，不下载照片原文件、视频、评论或私信。** 原生接口返回的照片链接与元数据会保留在 JSON；链接不是离线照片备份。

## 怎样判断是否完整

程序分别核对普通、封存日记的实际记录数和服务器结束标记；可用时另读取网页个人主页总数。网页与原生计数不一致时，会明确提示，并用可得计数的较大值保守核对。两处计数的差异原因不一定可确定。

只有所选范围的记录数通过核对、服务器明确结束、全部所选阅读文件都写完，报告才会是 `complete`。接口更新、网络失败、重复页、人工页数上限、未知正文格式或中断都不能冒充全量完成。

退出码：`0` 已完成所选范围；`1` 失败；`2` 输入错误；`3` 仅部分导出；`130` 用户中断。报告的 `scope` 说明是否包括封存日记，`media_files_downloaded` 始终说明图片原文件是否已下载。

原始分页逐页保存，JSON/TXT 在第 1 页、每 10 页以及退出时更新。中断后请保留整个输出目录；已有同名备份时默认拒绝覆盖。此版本保留恢复所需的数据，但**不会自动跨运行断点续传**。

## 常用选项

```bash
# 自定义输出位置与文件名前缀；仍然会隐藏询问密码
python3 icity_export.py --output-dir ./my-backup --prefix diary

# 只导出普通日记，不含封存日记
python3 icity_export.py --no-archived

# 不生成按日 Markdown
python3 icity_export.py --no-split-md

# 网页备用模式：可能只开放部分历史，遇到限制会明确标为部分导出
python3 icity_export.py --source web

# 限制页数做测试；不能把人工截断的结果当成完整备份
python3 icity_export.py --max-pages 2
```

`--overwrite` 表示明确允许替换同名结果。原生分页旧目录会保留为 `.previous-时间戳`，不与新一次下载混合。

为兼容旧用法，仍保留 `--username`、`--password` 和旧位置参数，但**不推荐把密码写进命令**，否则可能进入命令历史或进程列表。

## 隐私和验证

- 密码默认只在本机隐藏输入，不保存到输出文件；登录凭据不写入原始分页。
- 原生请求仅发往 `https://icity.ly`，不跟随重定向；网页登录也不转发带密码的重定向请求。
- 输出包含私密日记，不要将输出目录、Cookie、Token、个人排查快照或登录配置上传到 GitHub。
- 仓库测试只使用合成数据，不会在 CI 登录真实账户。

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

原生协议实现参考了 MIT 许可的 [icity-exporter](https://github.com/chrisprown902-wq/icity-exporter)。版权及许可全文见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目不是 iCity 官方产品。
