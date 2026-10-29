"""贴吧内容抓取流水线：从指定吧抓取帖子与楼层评论，清洗成学习样本。"""

import asyncio
import hashlib
import re
from typing import Callable, Optional

from .client import TiebaClient

# 清洗用的正则
_HTML_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[\w\-./?=%&~+#:@!\*',;)\]}]+", re.I)
_AT_RE = re.compile(r"@[^\s@:：,，]{1,30}")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")
# 纯水帖文本（只由这些字符组成）
_WATER_WORDS = {
    "顶", "dd", "DD", "dd.", "Mark", "mark", "插眼", "留名", "抓", "前排", "nhof",
    "111", "222", "333", "??", "？", "。", "...", "。。。", "哈", "哈哈", "666", "6",
}


def clean_text(text: str, min_len: int, max_len: int) -> Optional[str]:
    """清洗评论文本。返回 None 表示应过滤掉。"""
    if not text:
        return None
    t = _HTML_RE.sub("", str(text))
    t = _URL_RE.sub("", t)
    t = _AT_RE.sub("", t)
    # 贴吧换行符占位（\ue001 之类）与多余空白
    t = t.replace("\ue001", "\n")
    t = _MULTI_SPACE_RE.sub(" ", t)
    t = _MULTI_NEWLINE_RE.sub("\n", t)
    t = t.strip()
    if not t or t in _WATER_WORDS:
        return None
    # 全是标点/数字
    if not re.search(r"[一-鿿A-Za-z]", t):
        return None
    if len(t) < min_len or len(t) > max_len:
        return None
    return t


def _sample_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class TiebaCrawler:
    """按配置抓取指定吧的帖子与评论，产出学习样本并写入 store 的 corpus。"""

    def __init__(self, client: TiebaClient, store, config: dict):
        self.client = client
        self.store = store
        self.config = config or {}

    def _cfg(self, key: str, default=None):
        v = self.config.get(key, default)
        return v if v is not None else default

    async def crawl(self, progress_cb: Optional[Callable[[str], None]] = None) -> dict:
        """执行一轮抓取。返回统计信息 dict。"""
        forums = [str(f).strip() for f in self._cfg("forums", []) if str(f).strip()]
        posts_per_forum = int(self._cfg("posts_per_crawl", 10) or 10)
        comments_per_post = int(self._cfg("comments_per_post", 15) or 15)
        interval = float(self._cfg("request_interval", 2.0) or 2.0)
        min_len = int(self._cfg("min_comment_length", 4) or 4)
        max_len = int(self._cfg("max_comment_length", 300) or 300)

        stats = {
            "forums": len(forums),
            "threads": 0,
            "comments": 0,
            "samples": 0,
            "skipped_dup_threads": 0,
            "errors": [],
        }
        if not forums:
            stats["errors"].append("尚未配置目标贴吧")
            return stats

        corpus = self.store.corpus
        scanned = set(corpus.get("scanned_tids", []))
        seen_hashes = set(s.get("hash") for s in corpus.get("pending_samples", []) if isinstance(s, dict))

        def report(msg: str):
            if progress_cb:
                try:
                    progress_cb(msg)
                except Exception:
                    pass

        for kw in forums:
            report(f"正在抓取「{kw}」的帖子列表...")
            try:
                threads = await self.client.get_threads(kw, limit=posts_per_forum * 2)
            except Exception as e:
                msg = f"「{kw}」帖子列表获取失败: {e}"
                stats["errors"].append(msg)
                report(msg)
                continue

            picked = 0
            for th in threads:
                if picked >= posts_per_forum:
                    break
                tid = th.get("tid")
                if not tid or tid in scanned:
                    stats["skipped_dup_threads"] += 1
                    continue
                scanned.add(tid)
                corpus.setdefault("scanned_tids", []).append(tid)
                picked += 1
                stats["threads"] += 1

                # 主楼正文（摘要）也作为样本
                if th.get("abstract"):
                    text = clean_text(th["abstract"], min_len, max_len)
                    if text:
                        self._add_sample(corpus, seen_hashes, text, kw, tid, th.get("author"), "thread")
                        stats["samples"] += 1

                await asyncio.sleep(max(interval, 0.5))
                try:
                    posts = await self.client.get_posts(tid, limit=comments_per_post)
                except Exception as e:
                    msg = f"帖子 {tid} 楼层获取失败: {e}"
                    stats["errors"].append(msg)
                    report(msg)
                    continue

                stats["comments"] += len(posts)
                for p in posts:
                    text = clean_text(p.get("content") or "", min_len, max_len)
                    if text:
                        added = self._add_sample(
                            corpus, seen_hashes, text, kw, tid, p.get("author"), "comment"
                        )
                        if added:
                            stats["samples"] += 1
                report(f"「{kw}」帖子 {tid} 完成，累计样本 {stats['samples']} 条")
                await asyncio.sleep(max(interval, 0.5))

        corpus.setdefault("stats", {})
        corpus["stats"]["total_threads"] = int(corpus["stats"].get("total_threads", 0)) + stats["threads"]
        corpus["stats"]["total_comments"] = int(corpus["stats"].get("total_comments", 0)) + stats["comments"]
        from datetime import datetime

        corpus["stats"]["last_crawl_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 仅保留最近的样本用于下一轮学习，避免样本池无限增长
        corpus["pending_samples"] = corpus["pending_samples"][-3000:]
        await self.store.save_corpus()
        report(f"抓取完成：{stats['threads']} 帖 / {stats['comments']} 楼 / {stats['samples']} 样本")
        return stats

    def _add_sample(self, corpus, seen_hashes, text, forum, tid, author, kind) -> bool:
        h = _sample_hash(text)
        if h in seen_hashes:
            return False
        seen_hashes.add(h)
        corpus.setdefault("pending_samples", []).append(
            {
                "hash": h,
                "text": text,
                "forum": forum,
                "tid": str(tid),
                "author": author or "",
                "kind": kind,
            }
        )
        return True
