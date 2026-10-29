"""本地持久化存储封装。

按照 AstrBot 插件规范，持久化数据存放在
``data/plugin_data/astrbot_plugin_tieba_tone/`` 目录下，
避免插件更新/重装时数据被覆盖。

文件说明：
- ``account.json``  百度账号登录态（BDUSS / STOKEN 等）
- ``corpus.json``   学习成果：风格画像、代表性语录、统计、待学习样本
- ``state.json``    运行状态：上次执行时间、运行日志等
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# 日志环形缓冲区最大条数
MAX_LOGS = 300
# 待学习样本上限（防止无限膨胀）
MAX_PENDING_SAMPLES = 3000
# 已抓取帖子 id 记录上限
MAX_SCANNED_TIDS = 5000


def _default_corpus() -> dict:
    return {
        # LLM 生成的语言风格指南（会注入到对话系统提示词中）
        "profile": "",
        # 代表性语录 [{"text": ..., "forum": ..., "tid": ..., "author": ...}]
        "quotes": [],
        "stats": {
            "total_threads": 0,
            "total_comments": 0,
            "learn_count": 0,
            "last_crawl_at": "",
            "last_learn_at": "",
        },
        # 最近一次抓取收集到的原始样本（供下次学习使用）
        "pending_samples": [],
        # 已经抓取过的帖子 id，避免重复学习
        "scanned_tids": [],
    }


def _default_state() -> dict:
    return {
        "last_run": 0.0,
        "running": False,
        "last_error": "",
        "logs": [],
    }


class Store:
    """插件持久化存储。所有方法均为协程安全（写操作加了异步锁）。"""

    def __init__(self, base_dir: Path):
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.account: dict = self._load("account.json", {})
        self.corpus: dict = self._load("corpus.json", _default_corpus())
        self.state: dict = self._load("state.json", _default_state())

    # ------------------------------------------------------------------ 基础
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        if not path.exists():
            return default() if callable(default) else default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default() if callable(default) else default

    def _save_sync(self, name: str, data: Any) -> None:
        """原子写：先写临时文件再替换，避免写坏现有数据。"""
        path = self._path(name)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(self.dir), prefix=f".{name}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            pass

    async def _save(self, name: str, data: Any) -> None:
        async with self._lock:
            self._save_sync(name, data)

    # ---------------------------------------------------------------- 账号
    async def save_account(self) -> None:
        await self._save("account.json", self.account)

    @property
    def is_logged_in(self) -> bool:
        return bool(self.account.get("bduss"))

    def mask_bduss(self) -> str:
        bduss = str(self.account.get("bduss") or "")
        if not bduss:
            return ""
        if len(bduss) <= 8:
            return bduss[:2] + "***"
        return f"{bduss[:4]}...{bduss[-4:]}"

    # ---------------------------------------------------------------- 语料
    async def save_corpus(self) -> None:
        # 限制体积
        self.corpus["pending_samples"] = self.corpus.get("pending_samples", [])[-MAX_PENDING_SAMPLES:]
        self.corpus["scanned_tids"] = self.corpus.get("scanned_tids", [])[-MAX_SCANNED_TIDS:]
        await self._save("corpus.json", self.corpus)

    def reset_corpus(self) -> None:
        """清空语料（保留统计历史中的计数字段为 0）。"""
        self.corpus = _default_corpus()

    def has_corpus(self) -> bool:
        return bool(self.corpus.get("profile")) or bool(self.corpus.get("quotes"))

    # ---------------------------------------------------------------- 状态
    async def save_state(self) -> None:
        self.state["logs"] = self.state.get("logs", [])[-MAX_LOGS:]
        await self._save("state.json", self.state)

    def push_log(self, level: str, message: str) -> None:
        """写入运行日志（同时供 WebUI 展示）。"""
        import time

        self.state.setdefault("logs", []).append(
            {
                "ts": time.time(),
                "level": level,
                "msg": str(message),
            }
        )
        self.state["logs"] = self.state["logs"][-MAX_LOGS:]

    def recent_logs(self, limit: int = 50) -> list:
        return list(reversed(self.state.get("logs", [])))[:limit]
