# astrbot_plugin_tieba_tone

> 吧味学习机 —— 百度贴吧语言风格学习插件 for [AstrBot](https://github.com/AstrBotDevs/AstrBot)

刷管理员指定贴吧的帖子与楼层评论，让 LLM 学习吧友的说话方式和语言习惯，
并自动应用到后续的 AI 对话中 —— 让你的机器人说话「对味」。

## ✨ 功能特性

- 📥 **刷帖学习**：可指定任意贴吧（吧名列表），只抓取每个吧**「热门」分区**的帖子正文 + 楼层评论，自动跳过置顶帖（吧规/公告等）
- 🧠 **风格提炼**：把抓到的语料交给 LLM，生成「语言风格指南 + 代表性语录」
- 💬 **应用到对话**：通过 `on_llm_request` 钩子自动注入系统提示词，后续每次对话都使用学到的风格（**只读学习，不发帖、不回复、不点赞**）
- ⏰ **定时任务**：支持「每天 X 点」或「每小时」执行一次；每次刷多少帖、每帖学多少评论均可配置
- 🔐 **百度登录**：WebUI 中扫码登录（与 [TiebaLite](https://github.com/zzc10086/TiebaLite) 同思路：打开百度登录页 → 用户手动完成登录 → 自动获取 BDUSS 登录态），也支持手动填入 BDUSS
- 🖥️ **图形化 WebUI**：插件详情页内置可视化视图，登录、查看状态/语料/日志、一键触发学习，**全部配置均可直接在 WebUI 修改**，学习用的模型也可下拉选择

## 📦 安装

将本仓库克隆/下载到 AstrBot 的插件目录：

```bash
# 源码部署：克隆到 data/plugins/ 下
cd AstrBot/data/plugins
git clone https://github.com/Chescy20/astrbot_plugin_tieba_tone.git
```

或者在 AstrBot WebUI 的「插件」页面中通过 Git 仓库地址安装。安装后重启 AstrBot，插件会自动加载（依赖 `aiohttp`，AstrBot 已内置）。

> 如果之前安装过旧版 `astrbot_plugin_tieba_style`，删掉旧插件目录即可，登录态与语料会自动迁移到新目录下。

## 🚀 使用

### 1. 配置目标贴吧

在 AstrBot WebUI → 插件 → 「吧味学习机」→ 配置中填写，也可以打开**可视化视图 →「设置」**页直接修改：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 启用定时刷帖学习 | 是否按频率自动执行 | 关 |
| 目标贴吧 | 要学习的吧名列表 | `["孙笑川吧"]` |
| 执行频率 | `daily`（每天一次）/ `hourly`（每小时一次） | `daily` |
| 每日执行时间（小时） | daily 模式的执行时刻 | 3 |
| 每次刷帖数量（每个吧） | 每轮从每个吧抓取的帖子数 | 10 |
| 每个帖子的评论学习数 | 每帖抓取的楼层数 | 15 |
| 自动应用到后续对话 | 是否注入系统提示词 | 开 |
| 注入语录条数上限 | 注入的代表性语录最大条数 | 30 |
| 学习用的模型 Provider ID | 留空 = 使用当前默认模型 | 空 |
| 请求间隔（秒） | 防风控 | 2.0 |

### 2. 登录百度账号

进入 AstrBot WebUI → 插件 → 「吧味学习机」→ 可视化视图，打开「百度账号」标签页：

**方式一：扫码登录（推荐）**

1. 点击「获取登录二维码」；
2. 用手机百度 App 扫描页面上的二维码，并在手机上**手动确认登录**；
3. 插件自动获取登录信息（BDUSS），全程不保存账号密码。

登录流程与 TiebaLite 一致：TiebaLite 是点击登录后弹出百度账号登录网页，
用户手动登录后 App 从 WebView 中取出 Cookie；本插件在网页环境下用百度官方的
扫码登录接口实现了同样的「用户手动登录 → 自动获取登录态」流程。

**方式二：手动 BDUSS**

1. 浏览器打开 `https://tieba.baidu.com` 并登录百度账号；
2. 按 F12 → 应用程序/存储 → Cookies → 找到 `BDUSS` 字段，复制其值；
3. 粘贴到 WebUI 的「手动 BDUSS 登录」输入框中保存。

> 说明：部分公开贴吧也可以不登录直接学习；登录后可抓取到更多内容、更稳定。

### 3. 开始学习

- **自动**：配置中开启「启用定时刷帖学习」，到点自动执行；
- **手动**：WebUI「概览」页点击「立即刷帖学习」，或聊天中发送 `/tieba learn`（管理员）。

学习过程：抓取帖子与评论 → 清洗过滤（去链接/HTML/纯水帖）→ LLM 提炼风格指南与
代表性语录 → 保存到语料库 → 下一次对话自动生效。

## 🤖 聊天指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/tieba` 或 `/tieba help` | 所有人 | 帮助 |
| `/tieba status` | 所有人 | 查看状态 |
| `/tieba show` | 所有人 | 查看当前学到的风格摘要与语录 |
| `/tieba learn` | 管理员 | 立即刷帖学习 |
| `/tieba on` / `/tieba off` | 管理员 | 开启/关闭自动应用到对话 |
| `/tieba reset` | 管理员 | 清空已学习的语料 |
| `/tieba forum` | 管理员 | 查看目标贴吧列表 |
| `/tieba forum add 孙笑川吧` | 管理员 | 添加目标贴吧 |
| `/tieba forum del 孙笑川吧` | 管理员 | 移除目标贴吧 |

## 🧩 它是如何工作的

```
┌─────────────┐   帖子/评论    ┌──────────────┐   清洗后样本   ┌──────────┐
│ 贴吧接口     │ ────────────▶ │  Crawler     │ ────────────▶ │ Learner  │
│ frs/pb page │               └──────────────┘               └────┬─────┘
└─────────────┘                                                   │ LLM 提炼
                                                                  ▼
┌─────────────┐  系统提示词注入   ┌──────────────┐   风格画像      ┌──────────┐
│ on_llm_req  │ ◀────────────── │  main.py     │ ◀────────────── │ 语料库    │
└─────────────┘                 └──────────────┘                  └──────────┘
```

1. **抓取**：优先使用贴吧 PC 端 JSON 接口（`/c/f/frs/page_pc`、`/c/f/pb/page_pc`），失败自动回退移动端接口，带 BDUSS 登录态；
2. **学习**：样本去重清洗后交给 LLM，输出 Markdown 风格指南和 JSON 语录；
3. **应用**：每次 LLM 请求前，把风格指南追加到系统提示词末尾（稳定型人设注入，符合 AstrBot 开发规范建议）。

## 📂 目录结构

```
astrbot_plugin_tieba_tone/
├── main.py                    # 插件主类：指令、调度、LLM 注入、Web API
├── metadata.yaml              # 插件元数据
├── _conf_schema.json          # 配置 Schema（WebUI 配置页）
├── requirements.txt
├── store.py                   # 持久化存储（data/plugin_data 下）
├── tieba/
│   ├── client.py              # 登录（扫码/BDUSS）与贴吧 API
│   ├── crawler.py             # 抓取流水线与文本清洗
│   └── learner.py             # LLM 风格学习
├── views/dashboard/           # 插件可视化视图（AstrBot 4.29+ 标准目录）
│   ├── index.html / app.js / style.css
└── .gitignore
```

## ⚠️ 注意事项

- 本插件**只读取**帖子内容用于学习，**不会**发帖、回复、点赞或签到；
- 只抓取各吧「热门」分区（移动端接口 `tab_id=1`）的帖子，并跳过置顶帖，避免学到吧规公告等无风格价值的内容；
- 请遵守目标贴吧的吧规，控制抓取频率（默认每次请求间隔 2 秒），避免给服务器造成压力；
- BDUSS 是敏感登录凭证，仅保存在本地 `data/plugin_data/astrbot_plugin_tieba_tone/account.json`，请勿泄露给他人；
- 学习效果取决于目标贴吧的内容质量与所用模型能力，建议选择活跃、语言风格鲜明的贴吧。

## 🙏 致谢与灵感来源

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件开发规范与文档
- [TiebaLite](https://github.com/zzc10086/TiebaLite) —— 登录流程参考（WebView 网页登录 → 提取 BDUSS 自动登录）
- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)、ParseHub、[RSSHub](https://github.com/DIYgod/RSSHub) —— 贴吧接口与签名算法参考

## 📃 许可证

MIT
