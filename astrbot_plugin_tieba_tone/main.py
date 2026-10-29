"""AstrBot 插件：吧味学习机（astrbot_plugin_tieba_tone）。

功能概览：
1. 按设定频率（每天/每小时）自动刷管理员指定贴吧的帖子与楼层评论；
2. 把抓到的内容交给 LLM 提炼成「语言风格指南 + 代表性语录」；
3. 之后每次调用 LLM 对话时，自动把风格指南注入系统提示词，
   让机器人的回复带上该吧吧友的说话方式和风格；
4. 提供 WebUI 可视化视图：扫码登录百度账号、查看状态/语料/日志、
   直接修改全部插件配置、下拉选择学习用模型。

参考与致谢：
- AstrBot 插件开发规范 https://docs.astrbot.app/dev/star/plugin-new.html
- TiebaLite https://github.com/zzc10086/TiebaLite （登录流程参考）
- MediaCrawler / ParseHub / RSSHub（贴吧接口与签名参考）
"""

import asyncio
import base64
import time
import uuid
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

from .store import Store
from .tieba import StyleLearner, TiebaClient, TiebaCrawler, QrLoginError

PLUGIN_NAME = "astrbot_plugin_tieba_tone"
LEGACY_PLUGIN_NAME = "astrbot_plugin_tieba_style"  # 旧版目录名，用于数据自动迁移
SCAN_INTERVAL_SEC = 30  # 调度循环检查间隔
QR_SESSION_TTL = 300  # 二维码会话有效期（秒）

# Web API 返回工具与 request 代理：
# AstrBot 4.29.x 起，register_web_api 注册的 handler 是【无参调用】的，
# 请求对象通过模块级代理 astrbot.api.web.request 获取（框架在每次请求时绑定）。
try:
    from astrbot.api.web import json_response, error_response  # noqa: F401
    from astrbot.api.web import request as plugin_request

    _HAS_WEB_HELPER = True
except Exception:  # pragma: no cover - 旧版本兼容
    _HAS_WEB_HELPER = False

    def json_response(data, status_code: int = 200, headers=None):
        from starlette.responses import JSONResponse

        return JSONResponse(data, status_code=status_code, headers=headers)

    def error_response(message: str, status_code: int = 400, data=None, headers=None):
        from starlette.responses import JSONResponse

        payload = {"error": message}
        if data is not None:
            payload["data"] = data
        return JSONResponse(payload, status_code=status_code, headers=headers)

    class _PluginRequestFallback:
        """旧版 AstrBot 没有 request 代理时的占位对象。"""

        def __getattr__(self, key):
            raise AttributeError(
                f"当前 AstrBot 版本不支持 astrbot.api.web.request，无法访问 request.{key}"
            )

    plugin_request = _PluginRequestFallback()


def _get_data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        root = Path(get_astrbot_data_path())
    except Exception:
        root = Path("data")
    return root / "plugin_data" / PLUGIN_NAME


def _migrate_legacy_data(target: Path) -> None:
    """从旧插件名（astrbot_plugin_tieba_style）自动迁移数据目录。"""
    try:
        import shutil

        legacy = target.parent / LEGACY_PLUGIN_NAME
        if legacy.exists() and not target.exists():
            shutil.copytree(legacy, target)
    except Exception:
        pass


@register(
    name=PLUGIN_NAME,
    author="Chescy20",
    desc="刷百度贴吧指定吧的帖子并学习吧友语言风格，应用到后续对话（吧味学习机）",
    version="1.0.0",
    repo="https://github.com/Chescy20/astrbot_plugin_tieba_tone",
)
class TiebaStylePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        data_dir = _get_data_dir()
        _migrate_legacy_data(data_dir)
        self.store = Store(data_dir)
        self.client = TiebaClient(account=self.store.account, ua=self.config.get("user_agent") or "")
        self.learner = StyleLearner()
        self._lock = asyncio.Lock()
        self._scheduler_task = None
        self._qr_sessions: dict = {}
        self._register_web_apis()
        self._log("info", "插件已加载，等待指令 / WebUI 操作")

    # ================================================================ 工具
    def _cfg(self, key: str, default=None):
        try:
            v = self.config.get(key, default)
        except Exception:
            v = default
        return v if v is not None else default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        v = self._cfg(key, default)
        return bool(v)

    def _log(self, level: str, message: str) -> None:
        try:
            getattr(logger, level)(f"[吧味学习机] {message}")
        except Exception:
            logger.info(f"[吧味学习机] {message}")
        try:
            self.store.push_log(level, message)
        except Exception:
            pass

    async def _check_admin(self, event: AstrMessageEvent) -> bool:
        """尽力判断消息发送者是否为管理员（兼容不同平台适配器）。"""
        try:
            val = getattr(event, "is_admin", None)
            if val is True:
                return True
            if callable(val):
                try:
                    ret = val()
                    if asyncio.iscoroutine(ret):
                        ret = await ret
                    if ret:
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        role = str(getattr(event, "role", "") or "").lower()
        if role in ("admin", "owner", "administrator", "superuser"):
            return True
        return False

    def _all_providers(self) -> list:
        """获取全部已配置的「对话（Chat Completion）」Provider 实例。"""
        provs: list = []
        # AstrBot 4.29.x: context.get_all_providers() 只返回对话型
        for meth in ("get_all_providers", "get_all_chat_providers"):
            fn = getattr(self.context, meth, None)
            if callable(fn):
                try:
                    ret = fn()
                    if ret:
                        provs = list(ret)
                        break
                except Exception:
                    pass
        if not provs:
            pm = getattr(self.context, "provider_manager", None)
            if pm is not None:
                try:
                    provs = list(pm.get_insts() or [])
                except Exception:
                    pass
        # 老版本没有分类时，用 provider_type 元信息过滤出对话型
        chat: list = []
        for p in provs:
            try:
                meta = p.meta() if callable(getattr(p, "meta", None)) else None
                if meta is not None:
                    pt = str(getattr(meta, "provider_type", "") or "")
                    if pt and "chat" not in pt.lower():
                        continue
            except Exception:
                pass
            chat.append(p)
        return chat or provs

    def _get_provider(self):
        """获取用于「风格学习」的 LLM Provider。

        - 配置了 provider_id 时，优先按 ID 精确获取（4.29.x 的
          context.get_provider_by_id），找不到再按 id/名称/模型名兜底匹配；
        - 留空时使用当前默认对话模型。
        """
        pid = str(self._cfg("provider_id", "") or "").strip()
        prov = None
        if pid:
            fn = getattr(self.context, "get_provider_by_id", None) or getattr(
                self.context, "get_provider", None
            )
            if callable(fn):
                try:
                    prov = fn(pid)
                except Exception as e:
                    self._log("warn", f"获取指定 Provider「{pid}」失败: {e}")
            if prov is None:
                # 兜底：按 id / 配置名 / 模型名 模糊匹配
                needle = pid.lower()
                for p in self._all_providers():
                    cfg = getattr(p, "provider_config", None) or {}
                    names = {
                        str(v).strip().lower()
                        for v in (
                            getattr(p, "id", None),
                            cfg.get("id"),
                            cfg.get("name"),
                            getattr(p, "model_name", None),
                        )
                        if v
                    }
                    if needle in names:
                        prov = p
                        break
                if prov is None:
                    self._log("warn", f"找不到 ID 为「{pid}」的 Provider，回退到默认对话模型")
        if prov is None:
            # 留空（或上面的回退）：使用当前默认对话模型
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                try:
                    prov = getter()
                except Exception:
                    prov = None
            if prov is None:
                for p in self._all_providers():
                    prov = p
                    break
        return prov

    # ================================================================ 生命周期
    @filter.on_astrbot_loaded()
    async def on_loaded(self, event=None):
        """AstrBot 全部加载完成后，启动定时调度协程。"""
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            self._log("info", "定时学习调度已启动")

    async def terminate(self):
        """插件被卸载/停用时调用。"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=3)
            except Exception:
                pass
        await self.client.close()
        self._log("info", "插件已卸载")

    async def _scheduler_loop(self):
        """调度循环：按配置的频率触发「刷帖+学习」。"""
        await asyncio.sleep(15)  # 等待其他组件就绪
        while True:
            try:
                await self._tick_schedule()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log("error", f"调度循环异常: {e}")
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def _tick_schedule(self):
        now = datetime.now()
        state = self.store.state
        if not self._cfg_bool("enabled", False):
            state["next_run_hint"] = "未启用定时学习"
            return
        if state.get("running"):
            return
        mode = str(self._cfg("schedule_mode", "daily") or "daily").lower()
        last = float(state.get("last_run") or 0)
        due = False
        if mode == "hourly":
            due = (now.timestamp() - last) >= 3600
            state["next_run_hint"] = "每小时执行一次"
        else:
            hour = int(self._cfg("daily_hour", 3) or 0) % 24
            today_target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            due = now >= today_target and last < today_target.timestamp()
            state["next_run_hint"] = f"每天 {hour}:00 执行"
        if due:
            try:
                await self.run_crawl_and_learn(trigger="schedule")
            except Exception as e:
                self._log("error", f"定时学习失败: {e}")

    # ================================================================ 核心流程
    async def run_crawl_and_learn(self, trigger: str = "manual") -> dict:
        """执行一轮「刷帖 → 学习」。同一时间只允许一个任务。"""
        if self.store.state.get("running"):
            raise RuntimeError("已有学习任务正在运行，请稍后再试")
        async with self._lock:
            self.store.state["running"] = True
            self.store.state["last_error"] = ""
            await self.store.save_state()
            try:
                forums = [str(f).strip() for f in (self._cfg("forums", []) or []) if str(f).strip()]
                if not forums:
                    raise ValueError("尚未配置目标贴吧，请到插件配置或 WebUI 中设置")

                # 同步最新登录态
                self.client.account = self.store.account

                self._log("info", f"开始刷帖（触发: {trigger}），目标：{forums}")
                crawler = TiebaCrawler(self.client, self.store, dict(self.config))
                stats = await crawler.crawl(progress_cb=lambda m: self._log("info", m))

                samples = self.store.corpus.get("pending_samples", [])
                if not samples:
                    self._log("warn", "本轮没有抓取到可用样本，跳过风格学习")
                    if stats["errors"]:
                        self.store.state["last_error"] = "; ".join(stats["errors"][:3])
                    return stats

                self._log("info", f"抓取到 {len(samples)} 条样本，开始调用 LLM 提炼语言风格...")
                provider = self._get_provider()
                result = await self.learner.learn(provider, samples, forums)

                corpus = self.store.corpus
                corpus["profile"] = result["profile"]
                corpus["quotes"] = [self._attach_quote_meta(q, samples) for q in result["quotes"]]
                corpus["stats"]["learn_count"] = int(corpus["stats"].get("learn_count", 0)) + 1
                corpus["stats"]["last_learn_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                corpus["pending_samples"] = []
                await self.store.save_corpus()

                self._log(
                    "info",
                    f"学习完成：风格指南 {len(result['profile'])} 字，"
                    f"代表性语录 {len(result['quotes'])} 条，已自动应用到后续对话"
                    if self._cfg_bool("auto_apply", True)
                    else f"学习完成：风格指南 {len(result['profile'])} 字，语录 {len(result['quotes'])} 条（未启用自动应用）",
                )
                self.store.state["last_run"] = time.time()
                await self.store.save_state()
                return stats
            except Exception as e:
                self.store.state["last_error"] = str(e)
                self._log("error", f"学习失败: {e}")
                raise
            finally:
                self.store.state["running"] = False
                await self.store.save_state()

    def _attach_quote_meta(self, quote: str, samples: list) -> dict:
        """给语录补上来源（吧名/帖子 id/作者）。"""
        item = {"text": quote}
        for s in samples:
            if isinstance(s, dict) and quote and quote in (s.get("text") or ""):
                item["forum"] = s.get("forum", "")
                item["tid"] = s.get("tid", "")
                item["author"] = s.get("author", "")
                break
        return item

    def build_prompt_addition(self) -> str:
        """构造要注入系统提示词的文本（供 WebUI 预览与 on_llm_request 共用）。"""
        corpus = self.store.corpus
        profile = (corpus.get("profile") or "").strip()
        quotes = corpus.get("quotes") or []
        if not profile and not quotes:
            return ""
        max_q = int(self._cfg("max_quotes", 30) or 30)
        parts = [
            "\n\n【语言风格指南（本机器人从百度贴吧发言中学习所得）】",
            "请在后续回复中使用下面这份语言风格：自然地模仿其口吻、句式、用词、Emoji 与标点习惯；"
            "不要逐句照抄参考语录；不要向用户提起这份指南、贴吧或「学习」这件事。",
        ]
        if profile:
            parts.append(profile)
        if quotes:
            qs = [(q["text"] if isinstance(q, dict) else str(q)) for q in quotes][:max_q]
            parts.append("代表性发言参考：\n" + "\n".join(f"- {t}" for t in qs))
        return "\n\n".join(parts)

    @filter.on_llm_request()
    async def on_llm_request(self, event, *args, **kwargs):
        """挂钩 LLM 请求：把学到的语言风格注入系统提示词。"""
        try:
            if not self._cfg_bool("auto_apply", True):
                return
            if not self.store.has_corpus():
                return
            # 兼容不同版本钩子签名，找出 ProviderRequest
            req = None
            for a in args:
                if a is not None and hasattr(a, "system_prompt"):
                    req = a
                    break
            if req is None:
                req = kwargs.get("request") or kwargs.get("req")
            if req is None:
                return
            addition = self.build_prompt_addition()
            if not addition:
                return
            base = getattr(req, "system_prompt", None) or ""
            req.system_prompt = base + addition
        except Exception as e:
            logger.warning(f"[贴吧风格] 注入系统提示词失败: {e}")

    # ================================================================ 聊天指令
    @filter.command("tieba", alias={"贴吧"})
    async def tieba_command(self, event: AstrMessageEvent):
        """吧味学习机·贴吧语言风格学习

        子指令：
          /tieba          查看状态与帮助
          /tieba status   查看详细状态
          /tieba show     查看当前学到的风格摘要与语录
          /tieba learn    立即刷帖学习（管理员）
          /tieba on/off   开启/关闭自动应用到对话（管理员）
          /tieba reset    清空已学习的语料（管理员）
          /tieba forum    查看目标贴吧列表（管理员）
          /tieba forum add <吧名> / del <吧名>（管理员）
        """
        raw = (event.message_str or "").strip()
        for prefix in ("/", "!", "！"):
            if raw.startswith(prefix):
                raw = raw[1:]
        tokens = raw.split()
        # 去掉指令名本身（可能是 tieba 或中文别名）
        if tokens and tokens[0].lower() in ("tieba", "贴吧"):
            tokens = tokens[1:]
        sub = tokens[0].lower() if tokens else "help"
        sub_args = tokens[1:]

        if sub in ("help", "帮助", "h", ""):
            yield event.plain_result(self._help_text())
        elif sub in ("status", "状态"):
            yield event.plain_result(self._status_text())
        elif sub in ("show", "查看", "风格"):
            yield event.plain_result(self._show_text())
        elif sub == "learn":
            if not await self._check_admin(event):
                yield event.plain_result("该操作仅限管理员。")
                return
            yield event.plain_result("开始刷帖学习，请稍候...")
            try:
                stats = await self.run_crawl_and_learn(trigger="command")
                msg = (
                    f"学习完成：抓取 {stats['threads']} 帖 / "
                    f"{stats['comments']} 楼，样本 {stats['samples']} 条。"
                )
                if stats["errors"]:
                    msg += f"\n有 {len(stats['errors'])} 个错误，详见 WebUI 日志。"
            except Exception as e:
                msg = f"学习失败: {e}"
            yield event.plain_result(msg)
        elif sub in ("on", "off", "启用", "关闭"):
            if not await self._check_admin(event):
                yield event.plain_result("该操作仅限管理员。")
                return
            enable = sub in ("on", "启用")
            self.config["auto_apply"] = enable
            try:
                self.config.save_config()
            except Exception:
                pass
            yield event.plain_result(
                f"已{'开启' if enable else '关闭'}语言风格自动应用到后续对话。"
            )
        elif sub == "reset":
            if not await self._check_admin(event):
                yield event.plain_result("该操作仅限管理员。")
                return
            self.store.reset_corpus()
            await self.store.save_corpus()
            yield event.plain_result("已清空语言风格语料。")
        elif sub == "forum":
            if not await self._check_admin(event):
                yield event.plain_result("该操作仅限管理员。")
                return
            action = sub_args[0].lower() if sub_args else "list"
            forums = [str(f).strip() for f in (self._cfg("forums", []) or []) if str(f).strip()]
            if action in ("list", "列表"):
                yield event.plain_result(
                    "目标贴吧：" + ("、".join(forums) if forums else "（空，请到插件配置中添加）")
                )
            elif action in ("add", "添加") and sub_args[1:]:
                name = sub_args[1].strip()
                if name and name not in forums:
                    forums.append(name)
                    self.config["forums"] = forums
                    try:
                        self.config.save_config()
                    except Exception:
                        pass
                yield event.plain_result(f"已添加「{name}」，当前目标：{'、'.join(forums)}")
            elif action in ("del", "remove", "删除") and sub_args[1:]:
                name = sub_args[1].strip()
                forums = [f for f in forums if f != name]
                self.config["forums"] = forums
                try:
                    self.config.save_config()
                except Exception:
                    pass
                yield event.plain_result(f"已移除「{name}」，当前目标：{'、'.join(forums)}")
            else:
                yield event.plain_result("用法：/tieba forum add <吧名> 或 /tieba forum del <吧名>")
        else:
            yield event.plain_result(self._help_text())

    def _help_text(self) -> str:
        return (
            "【吧味学习机】\n"
            "/tieba status - 查看状态\n"
            "/tieba show - 查看当前学到的风格\n"
            "/tieba learn - 立即刷帖学习（管理员）\n"
            "/tieba on|off - 开启/关闭应用到对话（管理员）\n"
            "/tieba reset - 清空语料（管理员）\n"
            "/tieba forum add|del <吧名> - 管理目标贴吧（管理员）\n"
            "详细配置与登录请前往 AstrBot WebUI 的插件可视化视图。"
        )

    def _status_text(self) -> str:
        corpus = self.store.corpus
        stats = corpus.get("stats", {})
        acc = self.store.account
        running = bool(self.store.state.get("running"))
        last_run = self.store.state.get("last_run")
        last_run_str = (
            datetime.fromtimestamp(float(last_run)).strftime("%Y-%m-%d %H:%M:%S")
            if last_run
            else "从未执行"
        )
        forums = "、".join(str(f) for f in (self._cfg("forums", []) or [])) or "（未配置）"
        return (
            "【吧味学习机·状态】\n"
            f"目标贴吧：{forums}\n"
            f"定时学习：{'启用' if self._cfg_bool('enabled', False) else '未启用'}"
            f"（{self._cfg('schedule_mode', 'daily')}）\n"
            f"自动应用：{'开' if self._cfg_bool('auto_apply', True) else '关'}\n"
            f"百度账号：{acc.get('username') or acc.get('display_name') or '未登录'}"
            f"{'(' + self.store.mask_bduss() + ')' if acc.get('bduss') else ''}\n"
            f"当前状态：{'正在学习...' if running else '空闲'}\n"
            f"上次执行：{last_run_str}\n"
            f"累计学习：帖子 {stats.get('total_threads', 0)} / 楼层 {stats.get('total_comments', 0)}"
            f" / 风格更新 {stats.get('learn_count', 0)} 次\n"
            f"上次错误：{self.store.state.get('last_error') or '无'}"
        )

    def _show_text(self) -> str:
        corpus = self.store.corpus
        profile = (corpus.get("profile") or "").strip()
        quotes = corpus.get("quotes") or []
        if not profile and not quotes:
            return "还没有学到任何语言风格，先执行 /tieba 学习一下吧。"
        text = "【当前语言风格指南】\n"
        if profile:
            text += profile[:1500] + ("\n…" if len(profile) > 1500 else "")
        if quotes:
            text += "\n\n代表性语录：\n"
            for q in quotes[:10]:
                t = q["text"] if isinstance(q, dict) else str(q)
                text += f"- {t}\n"
        return text

    # ================================================================ Web API
    def _register_web_apis(self):
        """注册 Web API。

        注意：handler 均为无参函数；请求对象通过模块级代理
        ``astrbot.api.web.request`` 在被调用时获取。
        """
        routes = [
            ("status", self.api_status, ["GET"], "获取吧味学习机插件状态"),
            ("config", self.api_config, ["GET", "POST"], "读取/更新全部运行配置"),
            ("models", self.api_models, ["GET"], "获取可用的对话模型（Provider）列表"),
            ("login/qr/start", self.api_qr_start, ["POST"], "获取百度登录二维码"),
            ("login/qr/poll", self.api_qr_poll, ["GET", "POST"], "轮询扫码登录状态"),
            ("login/bduss", self.api_bduss_login, ["POST"], "手动 BDUSS 登录"),
            ("login/logout", self.api_logout, ["POST"], "退出登录"),
            ("crawl/run", self.api_crawl_run, ["POST"], "立即触发刷帖学习"),
            ("corpus", self.api_corpus, ["GET"], "获取风格画像与语录"),
            ("corpus/reset", self.api_corpus_reset, ["POST"], "清空语料"),
            ("logs", self.api_logs, ["GET"], "获取运行日志"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)
            except Exception as e:
                logger.warning(f"[吧味学习机] 注册 Web API {path} 失败: {e}")

    # ---- 状态
    async def api_status(self):
        corpus = self.store.corpus
        stats = dict(corpus.get("stats", {}))
        acc = self.store.account
        last_run = self.store.state.get("last_run")
        return json_response(
            {
                "account": {
                    "logged_in": bool(acc.get("bduss")),
                    "username": acc.get("username") or acc.get("display_name") or "",
                    "bduss": self.store.mask_bduss(),
                    "source": acc.get("source", ""),
                    "logged_at": (
                        datetime.fromtimestamp(float(acc.get("logged_at") or 0)).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        if acc.get("logged_at")
                        else ""
                    ),
                },
                "config": {
                    "enabled": self._cfg_bool("enabled", False),
                    "forums": [str(f) for f in (self._cfg("forums", []) or [])],
                    "schedule_mode": self._cfg("schedule_mode", "daily"),
                    "daily_hour": self._cfg("daily_hour", 3),
                    "posts_per_crawl": self._cfg("posts_per_crawl", 10),
                    "comments_per_post": self._cfg("comments_per_post", 15),
                    "auto_apply": self._cfg_bool("auto_apply", True),
                    "provider_id": self._cfg("provider_id", ""),
                },
                "stats": stats,
                "has_corpus": self.store.has_corpus(),
                "quote_count": len(corpus.get("quotes", [])),
                "running": bool(self.store.state.get("running")),
                "last_run": last_run,
                "last_run_str": (
                    datetime.fromtimestamp(float(last_run)).strftime("%Y-%m-%d %H:%M:%S")
                    if last_run
                    else ""
                ),
                "next_run_hint": self.store.state.get("next_run_hint", ""),
                "last_error": self.store.state.get("last_error", ""),
            }
        )

    # ---- 配置
    def _config_snapshot(self) -> dict:
        """当前生效配置的完整快照（供 WebUI 展示）。"""
        return {
            "enabled": self._cfg_bool("enabled", False),
            "forums": [str(f) for f in (self._cfg("forums", []) or [])],
            "schedule_mode": str(self._cfg("schedule_mode", "daily") or "daily"),
            "daily_hour": int(self._cfg("daily_hour", 3) or 0),
            "posts_per_crawl": int(self._cfg("posts_per_crawl", 10) or 0),
            "comments_per_post": int(self._cfg("comments_per_post", 15) or 0),
            "auto_apply": self._cfg_bool("auto_apply", True),
            "max_quotes": int(self._cfg("max_quotes", 30) or 0),
            "min_comment_length": int(self._cfg("min_comment_length", 4) or 0),
            "max_comment_length": int(self._cfg("max_comment_length", 300) or 0),
            "request_interval": float(self._cfg("request_interval", 2.0) or 0),
            "provider_id": str(self._cfg("provider_id", "") or ""),
            "user_agent": str(self._cfg("user_agent", "") or ""),
        }

    def _apply_config(self, data: dict) -> tuple:
        """校验并写入配置，返回 (已更新字段字典, 错误信息列表)。"""
        updated: dict = {}
        errors: list = []

        def set_bool(key: str):
            if key in data:
                self.config[key] = bool(data[key])
                updated[key] = self.config[key]

        def set_int(key: str, lo: int, hi: int):
            if key in data:
                try:
                    v = int(data[key])
                except (TypeError, ValueError):
                    errors.append(f"{key} 必须是整数")
                    return
                if v < lo or v > hi:
                    errors.append(f"{key} 需在 {lo}~{hi} 之间")
                    return
                self.config[key] = v
                updated[key] = v

        def set_float(key: str, lo: float, hi: float):
            if key in data:
                try:
                    v = float(data[key])
                except (TypeError, ValueError):
                    errors.append(f"{key} 必须是数字")
                    return
                if v < lo or v > hi:
                    errors.append(f"{key} 需在 {lo}~{hi} 之间")
                    return
                self.config[key] = v
                updated[key] = v

        set_bool("enabled")
        set_bool("auto_apply")
        if "forums" in data:
            if isinstance(data["forums"], list):
                forums = [str(f).strip() for f in data["forums"] if str(f).strip()]
                self.config["forums"] = forums
                updated["forums"] = forums
            else:
                errors.append("forums 必须是字符串数组")
        if "schedule_mode" in data:
            v = str(data["schedule_mode"] or "").strip().lower()
            if v in ("daily", "hourly"):
                self.config["schedule_mode"] = v
                updated["schedule_mode"] = v
            else:
                errors.append("schedule_mode 只能是 daily 或 hourly")
        set_int("daily_hour", 0, 23)
        set_int("posts_per_crawl", 1, 100)
        set_int("comments_per_post", 0, 200)
        set_int("max_quotes", 1, 200)
        set_int("min_comment_length", 0, 100)
        set_int("max_comment_length", 10, 5000)
        set_float("request_interval", 0.5, 60)
        if "provider_id" in data:
            self.config["provider_id"] = str(data["provider_id"] or "").strip()
            updated["provider_id"] = self.config["provider_id"]
        if "user_agent" in data:
            self.config["user_agent"] = str(data["user_agent"] or "").strip()[:500]
            updated["user_agent"] = self.config["user_agent"]
        return updated, errors

    async def api_config(self):
        """GET 读取完整运行配置；POST 更新配置（全部字段均可修改）。"""
        method = str(getattr(plugin_request, "method", "GET") or "GET").upper()
        if method == "POST":
            data = await plugin_request.json(default={})
            if not isinstance(data, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)
            updated, errors = self._apply_config(data)
            if errors:
                return error_response("配置校验失败：" + "；".join(errors), status_code=400)
            try:
                self.config.save_config()
            except Exception as e:
                self._log("warn", f"保存配置失败: {e}")
            self._log("info", f"WebUI 更新配置: {list(updated.keys())}")
            return json_response({"updated": updated, "config": self._config_snapshot()})
        return json_response(self._config_snapshot())

    async def api_models(self):
        """列出 AstrBot 中已配置的对话模型（Provider），供 WebUI 下拉选择。"""
        infos = []
        for p in self._all_providers():
            cfg = getattr(p, "provider_config", None) or {}
            try:
                model = p.get_model() or ""
            except Exception:
                model = ""
            if not model:
                model = str(cfg.get("model") or "")
            ptype = str(cfg.get("type") or "")
            try:
                meta = p.meta()
                pid = str(getattr(meta, "id", "") or cfg.get("id") or "")
                model = str(getattr(meta, "model", "") or model)
                ptype = str(getattr(meta, "type", "") or ptype)
            except Exception:
                pid = str(cfg.get("id") or getattr(p, "id", "") or "")
            label = pid or "（未命名）"
            detail = " · ".join(x for x in (ptype, model) if x)
            if detail:
                label += f"（{detail}）"
            infos.append({"id": pid, "type": ptype, "model": model, "label": label})
        current = str(self._cfg("provider_id", "") or "")
        return json_response(
            {
                "providers": infos,
                "current": current,
                "default_in_use": not current.strip(),
            }
        )

    # ---- 登录
    async def api_qr_start(self):
        """申请扫码登录二维码，直接返回 base64 图片，前端可直接展示。"""
        try:
            qr = await self.client.qrcode_start()
        except Exception as e:
            self._log("error", f"获取登录二维码失败: {e}")
            return error_response(f"获取二维码失败: {e}", status_code=500)
        sid = uuid.uuid4().hex
        self._qr_sessions[sid] = {
            "sign": qr["sign"],
            "gid": qr["gid"],
            "callback": qr["callback"],
            "imgurl": qr["imgurl"],
            "created_at": time.time(),
            "done": False,
        }
        # 清理过期会话
        now = time.time()
        for k in [k for k, v in self._qr_sessions.items() if now - v["created_at"] > QR_SESSION_TTL]:
            self._qr_sessions.pop(k, None)
        # 尝试把二维码下载下来转成 data uri（避免浏览器跨域/CSP 问题）
        qrcode_data = ""
        try:
            img_bytes = await self.client.download_bytes(qr["imgurl"])
            qrcode_data = "data:image/png;base64," + base64.b64encode(img_bytes).decode("ascii")
        except Exception as e:
            self._log("warn", f"下载二维码图片失败，使用原始 URL: {e}")
        return json_response(
            {
                "sid": sid,
                "qrcode": qrcode_data,
                "qrcode_url": qr["imgurl"],
                "expires_in": QR_SESSION_TTL,
            }
        )

    async def api_qr_poll(self):
        try:
            sid = plugin_request.query.get("sid") or ""
        except Exception:
            sid = ""
        sess = self._qr_sessions.get(sid)
        if not sess:
            return error_response("二维码会话不存在或已过期，请重新获取", status_code=404)
        if time.time() - sess["created_at"] > QR_SESSION_TTL:
            self._qr_sessions.pop(sid, None)
            return error_response("二维码已过期，请重新获取", status_code=410)
        if sess.get("done"):
            return json_response(
                {"state": "success", "account": self._account_brief(), "message": "登录成功"}
            )
        try:
            result = await self.client.qrcode_poll(sess["sign"], sess["gid"], sess["callback"])
        except Exception as e:
            return json_response({"state": "error", "message": f"轮询失败: {e}"})
        state = result.get("state")
        if state == "success":
            account = result.get("account") or {}
            self.store.account = account
            self.client.account = account
            await self.store.save_account()
            sess["done"] = True
            self._log(
                "info",
                f"百度账号登录成功：{account.get('username') or account.get('display_name') or '未知'}",
            )
            return json_response(
                {"state": "success", "account": self._account_brief(), "message": "登录成功"}
            )
        message = result.get("message") or {
            "waiting": "等待扫码...",
            "scanned": "已扫码，请在手机上确认登录",
        }.get(state, "")
        return json_response({"state": state, "message": message})

    async def api_bduss_login(self):
        """手动填入 BDUSS 登录（适合不方便扫码的场景）。"""
        data = await plugin_request.json(default={})
        bduss = str((data or {}).get("bduss") or "").strip()
        if not bduss:
            return error_response("请提供 bduss 字段", status_code=400)
        try:
            account = await self.client.login_with_bduss(bduss)
        except QrLoginError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"登录失败: {e}", status_code=500)
        self.store.account = account
        self.client.account = account
        await self.store.save_account()
        self._log("info", f"BDUSS 手动登录成功：{account.get('username') or '未知名'}")
        return json_response({"account": self._account_brief()})

    async def api_logout(self):
        self.store.account = {}
        self.client.account = {}
        await self.store.save_account()
        self._log("info", "已退出百度账号登录")
        return json_response({"ok": True})

    def _account_brief(self) -> dict:
        acc = self.store.account
        return {
            "logged_in": bool(acc.get("bduss")),
            "username": acc.get("username") or acc.get("display_name") or "",
            "bduss": self.store.mask_bduss(),
            "source": acc.get("source", ""),
        }

    # ---- 抓取与学习
    async def api_crawl_run(self):
        if self.store.state.get("running"):
            return error_response("已有学习任务正在运行", status_code=409)
        asyncio.create_task(self._run_crawl_and_learn_safe("webui"))
        return json_response({"started": True, "message": "已开始刷帖学习，可在日志中查看进度"})

    async def _run_crawl_and_learn_safe(self, trigger: str):
        try:
            await self.run_crawl_and_learn(trigger=trigger)
        except Exception as e:
            self._log("error", f"WebUI 触发的学习失败: {e}")

    async def api_corpus(self):
        corpus = self.store.corpus
        quotes = corpus.get("quotes", [])[:200]
        return json_response(
            {
                "profile": corpus.get("profile", ""),
                "quotes": quotes,
                "stats": corpus.get("stats", {}),
                "pending_samples": len(corpus.get("pending_samples", [])),
                "preview": self.build_prompt_addition(),
            }
        )

    async def api_corpus_reset(self):
        self.store.reset_corpus()
        await self.store.save_corpus()
        self._log("warn", "WebUI 执行了清空语料操作")
        return json_response({"ok": True})

    async def api_logs(self):
        try:
            limit = int(plugin_request.query.get("limit") or 50)
        except Exception:
            limit = 50
        return json_response({"logs": self.store.recent_logs(limit)})
