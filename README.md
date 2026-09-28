# Telegram Video Search

Windows 本地视频检索工具。登录自己的 Telegram 账号，为有权访问的频道或超级群建立本地索引，按视频文件名与说明文字检索，以大图卡片浏览结果。

## 功能

- 扫码登录、验证码登录、两步验证；显示服务端指定的验证码接收方式和重发等待时间。
- 文件名、说明或两者一起搜索；两个独立关键词输入框，支持全部匹配 / 任一匹配。关键词无需相邻，位置和顺序不限。
- 大预览图卡片，每页 4 或 6 个视频；保留图片比例，支持完整说明和放大预览。
- 缩略图按需加载；新搜索前取消旧下载并清理上一轮图片缓存，同一轮翻页复用缓存。不下载完整视频。
- 历史索引、增量更新、停止后续扫、完整重扫；各账号索引分开保存。
- 可手动置顶或取消置顶任意频道，默认没有预设频道。
- 双击结果直接打开 Telegram Desktop 原消息，也可使用浏览器打开或复制链接。
- 支持 SOCKS5 / HTTP 代理；Windows DPAPI 加密保存 API 设置。

## 下载与使用

在本仓库的 **Releases** 下载 `TelegramVideoSearch-Windows.zip`，完整解压后运行 `TelegramVideoSearch.exe`。保留旁边的 `_internal` 文件夹。需要 64 位 Windows 10（1809 或更新）或 Windows 11，不需要另装 Python。

1. 在 [Telegram 官方网站](https://my.telegram.org/apps) 申请自己的 API ID / API Hash，在工具中填写。仓库和程序不包含预置凭据。
2. 推荐扫码登录：手机 Telegram → 设置 → 设备 → 连接桌面设备。也支持手机号验证码登录；有两步验证时需输入密码。
3. 选择频道，点击 **建立 / 继续索引**。首次索引耗时取决于可读取的历史消息数量。
4. 输入一个或两个关键词，点击 **搜索**。默认要求所有关键词出现；第二个输入框可以留空。两个框都留空可浏览全部已索引视频。
5. 选中视频可查看完整说明；双击卡片在 Telegram Desktop 中打开。可在左侧设置实际使用的 `Telegram.exe` 路径。

选择频道后点击 **置顶当前频道** 或 **取消频道置顶**。选择 **所有已索引频道** 可跨频道检索。搜索只覆盖已建立的本地索引，不识别视频画面或语音。

## 本地数据与隐私

程序在自身目录的 `data/` 中保存运行数据，也可通过 `--data-dir` 指定其他目录：

- `settings.dpapi`：使用当前 Windows 用户的 DPAPI 加密的 API、手机号和代理设置。
- `telegram.session`：登录会话；关闭窗口后可复用，点击“退出 Telegram 登录”会撤销该会话。
- `index_<账号ID>.sqlite3`：各账号独立的频道元数据和视频索引。
- `thumbnails/`：按账号隔离的预览图缓存。
- `preferences.json`、`desktop.json`：自行选择的频道置顶和桌面程序路径。

验证码、两步验证密码和临时二维码令牌不保存。登录会话和索引本身不加密，整个 `data/` 目录都应视作私有数据。不要上传 API Hash、会话文件、索引、缓存或个人配置；仓库的 `.gitignore` 已排除这些运行数据。分享程序时使用 Release 压缩包，不要重新压缩正在使用的整个程序目录。

## 索引与连接

- **建立 / 继续索引**：读取上次进度之后的消息；中断后可续扫。
- **完整重扫**：更新已编辑的文字、清除已删除或不可见的消息。成功后才替换旧索引，中断会保留原索引。
- Telegram 限流时按提示等待；验证码接收方式由 Telegram 服务端指定，工具不能保证短信或其他渠道送达。
- 代理由用户自行填写，不会自动读取 Telegram Desktop 的代理配置。
- 打开私有频道消息时，Telegram Desktop 中登录的账号也必须具有访问权限。
- 没有静态缩略图的消息会显示占位提示，不会为生成封面下载完整视频。

## 源码运行

使用 Windows、Python 3.12，在仓库目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src\app.py
```

运行自动化测试（全部使用临时目录和模拟数据，不需要真实账号）：

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

构建独立程序：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean TelegramVideoSearch.spec
.\.venv\Scripts\python.exe scripts\package_release.py
```

发布包生成于 `release/TelegramVideoSearch-Windows.zip`。推送 `v*` 标签会通过 GitHub Actions 运行测试、构建 Windows 程序并上传到 Release；也可手动运行工作流生成构建产物。

## 许可证

应用源码使用 [MIT License](LICENSE)。依赖保留各自许可证，详见 [第三方说明](THIRD_PARTY_NOTICES.md) 和 `licenses/`。
