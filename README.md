# ReadAloud · 朗读工作台

把任意文字读给你听 —— 语音合成用微软 Edge 神经语音，总结与润色用**你这台电脑上的 Ollama 模型**。
文字不出本机，AI 也在本机跑。

工作台页面可以放在 GitHub Pages 上，用任何设备打开都行；但它连的永远是**你本地那台机器**的引擎。

## 三种用法

| 场景 | 怎么用 |
| --- | --- |
| 网页 | 打开工作台 → 粘贴文稿 → 朗读。逐句高亮，点任意一句可跳读 |
| Obsidian | 插件 `readaloud-bridge`：选中文字 `Ctrl+Alt+P` 朗读，`Ctrl+Alt+S` 总结后朗读 |
| 任意软件 | 复制文字 → `Ctrl+Alt+R` 朗读剪贴板（`+S` 总结、`+W` 润色、`+X` 开工作台、`+Q` 停止） |

## 安装（一次性）

1. 下载本仓库（`Code → Download ZIP`）解压
2. 双击 **`install.bat`** —— 自动建环境、装依赖、注册 `readaloud://` 协议
3. 完事。之后**所有操作都在浏览器里**：在工作台页面点「一键启动本机引擎」即可

依赖只有 Python 3.10+ 和三个包（`edge-tts`、`flask`、`requests`）。
AI 功能额外需要 [Ollama](https://ollama.com)；没有也能正常朗读，只是「总结 / 润色」不可用。

## 在 GitHub Pages 上用

页面部署在 `docs/`，开启 Pages 后地址形如 `https://<用户名>.github.io/readaloud/`。

从云端页面连本机引擎时，浏览器会问一次是否允许「本地网络访问」（Chrome 142+ 的安全机制）——
**选允许即可**，之后不再打扰。顺手点一下「允许」的代价，换来的是文字和模型全程只在你本机。

## 常见问题

**页面显示「未连接本机引擎」**
先点页面上的「一键启动」。如果没反应，说明协议没注册 —— 双击 `install.bat` 或 `install-protocol.bat`。
实在不行就双击 `start-server.bat` 手动启动，再回页面点「重试」。

**浏览器提示被拒绝访问本地网络**
地址栏左侧图标 → 网站设置 → 把「本地网络访问」改成「允许」→ 刷新页面。

**总结/润色很慢或报错**
两个模型别同时驻留（16GB 显存会被挤爆）。在界面「本机引擎信息」里看看装了哪些模型，
选一个用就好。没有模型时执行 `ollama pull qwen3:4b`。

**edge-tts 报 403**
版本太老。`edge-tts` 需 **7.3.0+**（微软换了鉴权令牌方案），`requirements.txt` 已锁定。

## 端口与配置

默认端口 `8756`，改 `config.json` 里的 `port`；也可用环境变量 `READALOUD_PORT` 覆盖。
换了端口后，在页面地址后加 `?` 不行 —— 直接改浏览器 localStorage 的 `ra.port`，或改 `config.json` 后重启引擎。

## 目录结构

```
docs/index.html     工作台页面（GitHub Pages 发布的就是它，本地引擎也托管同一份）
server.py           引擎：语音合成 + 文本清洗分句 + AI 总结润色 + 播放广播
launcher.py         readaloud:// 协议的执行端（启动/停止/打开）
setup_protocol.py   注册/注销自定义协议
ra_common.py        剪贴板与热键的公共部分
clip_read.py        剪贴板朗读
hotkey.py           全局热键守护
install.bat         一键安装
```

## 卸载

- 关服务：`stop-server.bat`
- 注销协议：`uninstall-protocol.bat`
- 取消开机自启：`disable-autostart.bat`
- 删干净：直接删掉整个目录
