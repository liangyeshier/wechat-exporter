# 微信聊天记录导出工具（macOS）

> 在自己的 Mac 上，把**你本人**微信的聊天记录一键导出成网页 / PDF / 可打印图片 / 表格。
> 全程**本地、离线、只读** —— 不联网、不上传、不改动微信任何文件。

<p>
<img alt="platform" src="https://img.shields.io/badge/platform-macOS-black">
<img alt="python" src="https://img.shields.io/badge/python-3.11%2B-blue">
<img alt="license" src="https://img.shields.io/badge/license-MIT-green">
<img alt="WeChat" src="https://img.shields.io/badge/WeChat-v3.x%20%26%20v4.0%2B-07C160">
</p>

---

## ✨ 为什么用它

- **真双击即用，不碰终端。** 下载后双击 `启动.command`：自动装依赖 → 给微信签名一次 → 提取密钥 → 打开图形界面。以后再双击直接开。
- **真实还原，不是占位符。** 真实图片、真实头像、真实语音（可转文字）、视频、转账金额与状态、`[捂脸]` 这类表情码渲染成真 emoji。
- **自包含单文件 HTML。** 导出的网页把图片 / 头像 / 语音 / 视频全部内嵌进**一个文件**，发给别人也能直接打开。
- **能打印。** PDF 与 A4 图片排版，`紧凑`密度每页塞更多内容、打印页数最少。
- **可视化挑选。** 界面里选联系人、按时间范围、`✕` 删除不想要的消息，再导出。
- **多格式一次搞定。** HTML · PDF · A4 图片(PNG) · CSV(Excel) · TXT。
- **新旧微信都支持。** 自动识别 v3.x 与 v4.0+（含 zstd 压缩、每库独立密钥），无需手动指定版本。
- **隐私优先。** 语音转文字、图片解码全部在**本机离线**完成；微信数据只读，绝不回写。

---

## 🚀 快速开始

> 仅支持 **macOS**，且只能导出**你自己**登录在这台 Mac 上的微信。

1. **下载本项目**（绿色 `Code` → `Download ZIP`，解压；或 `git clone`）。
2. **双击 `启动.command`。** 首次会自动：
   - 安装依赖（首次需联网，几分钟）；
   - 提示给微信做一次性签名（要输入开机密码）→ 退出并重开微信 → 提取密钥；
   - 自动弹出浏览器界面。
   > 首次提取密钥前，请先**打开并登录微信**，随便点开一个**有图片**的聊天往上滚动，让图片加载出来。
3. **在界面里导出：** 选联系人 → 「加载消息」→（可 `✕` 删除）→ 勾选格式 → 「导出」。文件默认存到**桌面**。

> 双击没反应？右键 `启动.command` →「打开」→ 在弹窗里再点「打开」（macOS 首次会拦截未签名脚本）。

### 换一台 Mac 用
把整个文件夹拷过去（`.venv` 可以不拷），双击 `启动.command` 即可——它会在新机器上自动重建环境、重新签名、提取该机器的密钥。

---

## 📦 导出格式

| 格式 | 用途 |
|---|---|
| **HTML**（推荐） | 还原微信气泡，**自包含单文件**，转发即可打开 |
| **PDF / A4 图片** | 用于**打印**，A4 排版，紧凑密度省纸 |
| **CSV** | 表格，Excel 可直接打开（`utf-8-sig` 带 BOM，中文不乱码） |
| **TXT** | 纯文本 |

支持的消息类型：文本、图片、语音（可转写为文字）、视频、转账（显示**金额 + 状态**）、红包、表情、引用、位置、链接卡片等。

---

## 🔒 隐私与安全

- **仅限本人账号、本机本地数据。** 只导出你本人登录在这台 Mac 上的微信，不连接任何远程账号、不读取他人设备。
- **全程只读。** 把数据库**复制**到 `~/.wechat_exporter/decrypted/` 后再解密处理，**绝不回写或修改**微信文件。
- **完全离线。** 语音转写（faster-whisper）、图片解码全在本机 CPU 运行，**不上传任何音频 / 文本 / 图片**。
- 运行产生的中间数据在 `~/.wechat_exporter/`（`keys.json` 密钥、`decrypted/` 明文副本、`voice_cache/`），含敏感信息，请妥善保管或用后清理。**这些都在仓库之外，永远不会被提交。**

---

## ⚙️ 工作原理（简述）

```
locator        发现账号、检测 v3/v4 版本 → Layout（路径 / PRAGMA / schema）
   ↓
key_extractor  从运行中的微信进程内存扫描出 SQLCipher 密钥（纯 Python，mach_vm）
   ↓
decryptor      复制 DB → 按 v3/v4 对应 PRAGMA 用 SQLCipher 解密为明文副本
   ↓
reader/parser  读取联系人与会话表，解析各类消息、定位媒体文件
   ↓
media          头像 / 语音(SILK→WAV) / 图片(.dat→JPEG, 含 wxgf/HEVC) / 视频
   ↓
exporters      渲染为 HTML / PDF / A4 图片 / CSV / TXT
```

- 密钥只存在于**运行中的微信进程内存**里，磁盘上没有；提取需微信在线 + 对 `WeChat.app` 一次性签名 + 管理员权限。这步由 `启动.command` 自动完成。
- 媒体 `.dat` 解码为尽力而为（best-effort），极少数附件可能定位不到。
- 微信常改目录结构与加密参数，新版可能需要适配。

---

## ⚠️ 合法与免责声明（请务必阅读）

> 本项目仅供**个人备份自己的微信聊天记录**之用，与**腾讯 / 微信无关**，未获其授权或背书。
>
> - 请**仅在你拥有合法权利的数据**上使用；导出内容常含第三方隐私，**请勿未经同意传播或滥用**。
> - 提取密钥、解密本地数据库等操作**可能违反微信 / 腾讯的用户协议**，是否使用及由此产生的一切风险（包括账号风险）**由你自行评估并承担**。
> - 软件按「现状（AS-IS）」提供，**不作任何明示或暗示担保**，使用造成的后果由你自负。

---

## 🛠️ 命令行（进阶，可不看）

图形界面背后是一套完整的 CLI，可脚本化：

```bash
.venv/bin/python main.py --help                       # 全部参数
sudo .venv/bin/python main.py                          # 交互式
.venv/bin/python main.py --contact 张三 --format all --voice --self-contained --yes
```

常用参数：`--account <id片段>`、`--key <hex>`（手动传密钥，跳过提取）、`--voice`、`--format html|csv|txt|pdf|a4|all`、`--start/--end <YYYY-MM-DD>`、`--a4-density compact|normal`。

---

## 📁 目录结构

```
wechat-exporter/
├── 启动.command          # ★ 双击它：一键装依赖 + 签名 + 提取密钥 + 打开界面
├── 使用说明.md           # 给使用者看的简短中文说明
├── app.py                # 图形界面入口（本地网页版 Flask）
├── main.py               # 命令行入口
├── extract_keys.py       # 独立密钥提取脚本（仅标准库，供 sudo 调用）
├── core/                 # 核心：定位 / 提密钥 / 解密 / 读取 / 解析 / 媒体
├── export/               # 各格式导出器（html / csv / txt / image+pdf）
├── templates/            # HTML 导出模板（chat.html）
└── tests/                # 端到端测试（合成加密库验证）
```

---

## 🐍 环境要求

- macOS（Apple Silicon 或 Intel）
- Python 3.11 / 3.12（建议 `brew install python@3.12`；`启动.command` 会自动查找可用的 Python）

`启动.command` 会自动创建虚拟环境并安装 `requirements.txt` 里的依赖，**通常你不需要手动操作**。

---

## 📄 License

[MIT](./LICENSE) © 2026
