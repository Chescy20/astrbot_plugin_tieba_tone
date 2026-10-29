"""百度贴吧客户端：登录与内容抓取。

登录流程参考 TiebaLite (https://github.com/zzc10086/TiebaLite) 的思路：
点登录 → 打开百度账号登录网页 → 用户手动完成登录 → 自动获取登录信息
(BDUSS/STOKEN) → 之后所有请求自动携带账号新的登录态。

在 Web 环境下无法嵌入 App 的 WebView，因此用百度官方「扫码登录」接口
实现了等价流程：在插件 WebUI 中展示二维码 -> 用户用百度 App 扫码并手动确认
-> 服务端拿到 BDUSS/STOKEN -> 自动登录贴吧。同时也支持手动填入 BDUSS。

内容抓取优先使用贴吧移动端 JSON 接口（/c/f/frs/page，支持以 tab_id 指定
「热门」分区；移动端签名密钥 tiebaclient!!!），PC 端接口（page_pc）仅作兜底。
"""

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from typing import Any, Callable, Optional

import aiohttp

# 贴吧 PC 端接口签名密钥（逆向 PC 网页端 JS 所得）
PC_SALT = "36770b1f34c9bbf2e7d1a99d2b82fa9e"
# 贴吧移动客户端接口签名密钥
CLIENT_SALT = "tiebaclient!!!"

HOST = "https://tieba.baidu.com"
PASSPORT = "https://passport.baidu.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _jsonify(text: str) -> dict:
    """把响应解析成 dict：先尝试纯 JSON，再尝试 JSONP ``callback({...})``。"""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\((.*)\)", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 有时 JSONP 回包被多包了一层引号
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def _parse_qrbduss_login(text: str) -> dict:
    """解析 ``/v3/login/main/qrbdusslogin`` 的返回。

    百度这个接口返回的是「混合引号伪 JSON」：部分键值用双引号、部分用单引号
    （例如 ``'data': {...}``），且 ``stokenList`` 里夹杂 ``&quot;`` 实体，
    直接 ``json.loads`` 会失败。这里先做归一化再解析；仍然失败时按字段名
    正则兜底提取登录所需的关键字段。
    """
    s = (text or "").strip()
    if not s:
        return {}
    # 可能的 JSONP 包裹：callback({...})
    if not s.startswith("{"):
        m = re.search(r"\((.*)\)", s, re.S)
        if m:
            s = m.group(1).strip().rstrip(";").strip()
    body = s.replace("\\&", "&")
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # 单引号转双引号后重试
    try:
        converted = re.sub(r"'([^']*)'", r'"\1"', body)
        parsed = json.loads(converted)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # 正则兜底： quote 无关地提取需要的字段
    def find(key: str) -> str:
        m = re.search(r"['\"]" + re.escape(key) + r"['\"]\s*:\s*['\"]?([^'\"]*)['\"]?", body)
        return m.group(1) if m else ""

    code = find("code")
    if not code:
        return {}
    return {
        "code": code,
        "data": {
            "session": {
                "bduss": find("bduss"),
                "stoken": find("stoken"),
                "ptoken": find("ptoken"),
            },
            "user": {
                "username": find("username"),
                "userId": find("userId"),
                "displayName": find("displayName"),
            },
        },
    }


def _sign(params: dict, salt: str, sep: str = "", sort: bool = True) -> str:
    """贴吧接口签名。参考实现：参数键值对按（字典序）拼接后追加盐值取 md5。"""
    items = list(params.items())
    if sort:
        items = sorted(items, key=lambda kv: kv[0])
    raw = sep.join(f"{k}={v}" for k, v in items) + salt
    return md5(raw)


def content_to_text(content: Any) -> str:
    """把贴吧的富文本 content 字段转成纯文本。

    PC/移动接口中 content 可能是纯字符串，也可能是 ``[{type, text}, ...]``
    列表：type 0/1 为文字、2 为图片、3 为视频、18 为链接等。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    parts = []
    for item in content or []:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        t = str(item.get("type", "0"))
        txt = item.get("text") or item.get("c") or item.get("word") or ""
        if t in ("0", "1", "18") and isinstance(txt, str) and txt:
            parts.append(txt)
    return "".join(parts)


def pick_author(item: dict) -> str:
    """从帖子/楼层对象里尽量取到作者名。"""
    for key in ("user_name", "name", "author_name", "user_show_name", "nick_name", "show_name"):
        v = item.get(key)
        if v:
            return str(v)
    user = item.get("user") or item.get("author") or {}
    if isinstance(user, dict):
        for key in ("user_name", "name", "show_name", "nick_name", "display_name"):
            v = user.get(key)
            if v:
                return str(v)
    return ""


class QrLoginError(Exception):
    pass


class TiebaClient:
    def __init__(self, account: Optional[dict] = None, ua: str = ""):
        self.account = account or {}
        self.ua = ua or DEFAULT_UA
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------ session
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self, with_cookie: bool = True) -> dict:
        h = {"User-Agent": self.ua}
        if with_cookie and self.account.get("bduss"):
            cookie = f"BDUSS={self.account['bduss']}"
            if self.account.get("stoken"):
                cookie += f"; STOKEN={self.account['stoken']}"
            h["Cookie"] = cookie
        return h

    async def _get(self, url: str, params: dict = None, with_cookie: bool = True) -> str:
        session = await self._get_session()
        async with session.get(
            url, params=params, headers=self._headers(with_cookie)
        ) as resp:
            return await resp.text(encoding="utf-8", errors="ignore")

    async def _post(self, url: str, data: dict = None, with_cookie: bool = True) -> str:
        session = await self._get_session()
        async with session.post(
            url, data=data, headers=self._headers(with_cookie)
        ) as resp:
            return await resp.text(encoding="utf-8", errors="ignore")

    async def download_bytes(self, url: str) -> bytes:
        session = await self._get_session()
        async with session.get(url, headers={"User-Agent": self.ua}) as resp:
            return await resp.read()

    # ------------------------------------------------------------ 登录
    async def qrcode_start(self) -> dict:
        """申请登录二维码。返回 {sign, imgurl, gid, callback}。"""
        gid = uuid.uuid4().hex.upper()
        callback = f"tangram_guid{int(time.time() * 1000)}"
        params = {
            "lp": "pc",
            "qrloginfrom": "pc",
            "gid": gid,
            "callback": callback,
            "apiver": "v3",
            "tt": str(int(time.time() * 1000)),
            "tpl": "mn",
            "_": str(int(time.time() * 1000)),
        }
        text = await self._get(f"{PASSPORT}/v2/api/getqrcode", params)
        data = _jsonify(text)
        sign = data.get("sign")
        imgurl = data.get("imgurl") or ""
        if not sign or not imgurl:
            raise QrLoginError(f"获取二维码失败: {text[:200]}")
        if imgurl.startswith("//"):
            imgurl = "https:" + imgurl
        elif imgurl.startswith("http://"):
            imgurl = "https://" + imgurl[len("http://"):]
        elif not imgurl.startswith("https://"):
            # 百度有时返回无协议头的裸地址，如 passport.baidu.com/v2/api/qrcode?...
            imgurl = "https://" + imgurl
        return {"sign": sign, "imgurl": imgurl, "gid": gid, "callback": callback}

    async def qrcode_poll(self, sign: str, gid: str, callback: str) -> dict:
        """轮询扫码状态。

        返回:
            {"state": "waiting"}           等待扫码
            {"state": "scanned"}           已扫码，等待手机端确认
            {"state": "success", "account": {...}}  登录成功
            {"state": "error", "message": ...}
        """
        params = {
            "channel_id": sign,
            "tpl": "mn",
            "gid": gid,
            "callback": callback,
            "apiver": "v3",
            "tt": str(int(time.time() * 1000)),
            "_": str(int(time.time() * 1000)),
        }
        try:
            text = await self._get(f"{PASSPORT}/channel/unicast", params)
        except Exception:
            # unicast 是长轮询接口（服务端会 hold 住连接），客户端超时属正常现象，
            # 统一视为「等待扫码中」，由前端继续轮询。
            return {"state": "waiting"}

        data = _jsonify(text)
        err_info = data.get("errInfo")
        errno = data.get("errno")
        if errno is None and isinstance(err_info, dict):
            errno = err_info.get("no")
        try:
            errno = int(errno)
        except Exception:
            errno = 1
        if errno != 0:
            return {"state": "waiting"}

        channel_v = data.get("channel_v")
        if not channel_v:
            return {"state": "waiting"}
        try:
            payload = json.loads(channel_v) if isinstance(channel_v, str) else channel_v
        except Exception:
            return {"state": "scanned"}

        if isinstance(payload, dict) and "v" not in payload:
            # 带 status 字段表示尚未确认：1=已扫码待确认，2=回到等待
            status = payload.get("status")
            return {"state": "scanned" if str(status) == "1" else "waiting"}

        bduss = payload.get("v") if isinstance(payload, dict) else None
        if not bduss:
            return {"state": "waiting"}
        account = await self._qrbduss_login(str(bduss))
        if account:
            return {"state": "success", "account": account}
        return {"state": "error", "message": "扫码登录失败：无法用 BDUSS 换取登录态"}

    async def _qrbduss_login(self, bduss: str) -> Optional[dict]:
        """用扫码得到的临时 BDUSS 向百度换取完整登录态与账号信息。

        接口返回「混合引号伪 JSON」（部分键值用单引号），用
        ``_parse_qrbduss_login`` 归一化解析；成功后若账号名缺失，
        再用拿到的 BDUSS 调 ``/mo/q/sync`` 补全账号信息。
        """
        try:
            text = await self._get(
                f"{PASSPORT}/v3/login/main/qrbdusslogin",
                {"bduss": bduss},
                with_cookie=False,
            )
        except Exception:
            return None
        data = _parse_qrbduss_login(text)
        code = str(data.get("code") or "")
        d = data.get("data") or {}
        session = d.get("session") or {}
        user = d.get("user") or {}
        err_no = str((data.get("errInfo") or {}).get("no") or "")
        if code != "110000" and not (err_no == "0" and session.get("bduss")):
            return None
        new_bduss = session.get("bduss") or bduss
        account = {
            "bduss": new_bduss,
            "stoken": session.get("stoken") or "",
            "ptoken": session.get("ptoken") or "",
            "uid": str(user.get("userId") or ""),
            "username": user.get("username") or "",
            "display_name": user.get("displayName") or user.get("display_name") or "",
            "source": "qrcode",
            "logged_at": time.time(),
        }
        if not account["username"] and not account["uid"]:
            # 账号信息缺失时用换到的 BDUSS 再拉一次账号资料
            try:
                old_account = self.account
                self.account = account
                info = await self.fetch_account_info()
                self.account = old_account
                if info:
                    for k in ("uid", "username", "display_name"):
                        if info.get(k):
                            account[k] = info[k]
            except Exception:
                pass
        return account

    async def login_with_bduss(self, bduss: str) -> dict:
        """手动 BDUSS 登录。会尝试拉取账号名，失败也允许保存。"""
        bduss = (bduss or "").strip()
        if not bduss or " " in bduss:
            raise QrLoginError("BDUSS 格式不正确")
        account = {
            "bduss": bduss,
            "stoken": "",
            "uid": "",
            "username": "",
            "display_name": "",
            "source": "manual",
            "logged_at": time.time(),
        }
        old_account = self.account
        self.account = account
        try:
            info = await self.fetch_account_info()
            account.update(info)
        except Exception:
            pass
        finally:
            self.account = old_account
        return account

    async def fetch_account_info(self) -> dict:
        """尝试通过 /mo/q/sync 获取当前登录账号信息（用于校验 BDUSS 是否有效）。"""
        try:
            text = await self._get(f"{HOST}/mo/q/sync", {"client_type": "2", "back": "0"})
            data = _jsonify(text)
            if str(data.get("no")) == "0" and isinstance(data.get("data"), dict):
                user = data["data"].get("user") or {}
                if user:
                    return {
                        "uid": str(user.get("id") or user.get("uid") or ""),
                        "username": user.get("user_name") or user.get("name") or "",
                        "display_name": user.get("user_name") or user.get("name") or "",
                    }
        except Exception:
            pass
        return {}

    # ------------------------------------------------------------ 通用请求
    async def get_tbs(self) -> str:
        """获取 anti 防水墙 tbs。优先用 dc/common/tbs（匿名可用）。"""
        try:
            text = await self._get("http://tieba.baidu.com/dc/common/tbs", with_cookie=True)
            data = _jsonify(text)
            if data.get("tbs"):
                return str(data["tbs"])
        except Exception:
            pass
        # 回退：PC 端 psync 接口
        params = {"subapp_type": "pc", "_client_type": "20"}
        params["sign"] = _sign(params, PC_SALT)
        text = await self._get(f"{HOST}/c/s/pc/psync", params)
        data = _jsonify(text)
        return str(((data.get("data") or {}).get("anti") or {}).get("tbs") or "")

    async def _pc_api(self, path: str, data: dict, need_tbs: bool = True) -> dict:
        """调用 PC 端 JSON 接口，自动尝试多种签名变体。"""
        import copy

        base = {
            "subapp_type": "pc",
            "_client_type": "20",
        }
        if need_tbs:
            tbs = await self.get_tbs()
            if tbs:
                base["tbs"] = tbs

        variants = [
            ("sorted-nosep", True, ""),
            ("sorted-amp", True, "&"),
            ("raw-amp", False, "&"),
        ]
        last_err = ""
        for name, sort, sep in variants:
            payload = copy.deepcopy(base)
            payload.update(data)
            payload = {k: ("" if v is None else str(v)) for k, v in payload.items()}
            payload["sign"] = _sign(payload, PC_SALT, sep=sep, sort=sort)
            try:
                text = await self._post(f"{HOST}{path}", payload)
                resp = _jsonify(text)
                if int(str(resp.get("error_code", "0")) or 0) == 0:
                    return resp
                last_err = str(resp.get("error_msg") or resp.get("error_code") or "未知错误")
            except Exception as e:
                last_err = str(e)
        raise QrLoginError(f"PC 接口 {path} 请求失败: {last_err}")

    async def _mobile_api(self, path: str, params: dict) -> dict:
        """调用移动客户端接口（tiebaclient!!! 签名）。"""
        payload = {
            "_client_id": f"wappc_{int(time.time() * 1000)}_{random.randint(0, 999)}",
            "_client_type": "2",
            "_client_version": "12.20.1.0",
            "_phone_imei": "000000000000000",
            "from": "tieba",
        }
        if self.account.get("bduss"):
            payload["BDUSS"] = self.account["bduss"]
        payload.update(params)
        payload = {k: str(v) for k, v in payload.items()}
        payload["sign"] = _sign(payload, CLIENT_SALT)
        text = await self._post(f"{HOST}{path}", payload)
        resp = _jsonify(text)
        if int(str(resp.get("error_code", "0")) or 0) != 0:
            raise QrLoginError(
                f"移动接口 {path} 请求失败: {resp.get('error_msg') or resp.get('error_code')}"
            )
        return resp

    # ------------------------------------------------------------ 内容抓取
    # 贴吧吧页的「热门」分区 tab_id（跨吧固定为 1；503 是「最新」，
    # 其余为各吧自定义分区）。实测三个不同吧（孙笑川吧/bilibili吧/抗压背锅吧）
    # 的 frs_main_tab_list 中热门均为 tab_id=1。
    HOT_TAB_ID = "1"

    async def get_threads(self, kw: str, limit: int = 10) -> list:
        """获取吧「热门」分区的帖子列表（不含置顶帖）。

        返回 [{tid, title, abstract, author, reply_num}]。
        优先使用移动端接口（支持 tab_id 指定分区），PC 端仅作兜底。
        """
        kw = (kw or "").strip()
        if not kw:
            return []
        last_err = ""
        try:
            resp = await self._mobile_api(
                "/c/f/frs/page",
                {"kw": kw, "pn": "1", "rn": str(max(limit * 3, 30)), "tab_id": self.HOT_TAB_ID},
            )
            threads = self._parse_threads(resp)
            if threads:
                return threads[:limit]
        except Exception as e:
            last_err = str(e)
        # 回退 PC 端（无法指定分区，仅作兜底）
        try:
            resp = await self._pc_api(
                "/c/f/frs/page_pc",
                {
                    "kw": kw,
                    "pn": "1",
                    "sort_type": "-1",
                    "is_newfrs": "1",
                    "is_newfeed": "1",
                    "rn": str(max(limit * 3, 30)),
                    "rn_need": "10",
                },
            )
            return self._parse_threads(resp)[:limit]
        except Exception as e:
            raise QrLoginError(f"获取「{kw}」热门分区帖子列表失败（移动/PC 端均失败）: {e}; {last_err}")

    def _parse_threads(self, resp: dict) -> list:
        data = resp.get("data") or resp or {}
        thread_list = data.get("thread_list") or []
        result = []
        for item in thread_list:
            if not isinstance(item, dict):
                continue
            # 跳过置顶帖（吧规/公告/活动置顶），它们不代表吧友日常语言风格
            if item.get("is_top") or item.get("is_membertop") or item.get("top_thread_set_time"):
                continue
            tid = item.get("id") or item.get("tid") or item.get("thread_id")
            if not tid:
                continue
            abstract = item.get("abstract")
            if abstract is None:
                abstract = item.get("abstracts") or ""
            result.append(
                {
                    "tid": str(tid),
                    "title": str(item.get("title") or ""),
                    "abstract": content_to_text(abstract),
                    "author": pick_author(item),
                    "reply_num": item.get("reply_num") or 0,
                }
            )
        return result

    async def get_posts(self, tid: str, limit: int = 15) -> list:
        """获取某帖子的楼层评论。返回 [{pid, floor, content, author, post_time}]，
        并把主楼正文作为第一条（content 字段）。"""
        tid = str(tid or "").strip()
        if not tid:
            return []
        try:
            resp = await self._pc_api(
                "/c/f/pb/page_pc",
                {
                    "pn": "1",
                    "lz": "0",
                    "r": "2",
                    "mark_type": "0",
                    "back": "0",
                    "fr": "personal_page",
                    "kz": tid,
                    "session_request_times": "1",
                },
                need_tbs=True,
            )
            posts = self._parse_posts(resp)
            if posts:
                return posts[: limit]
        except Exception:
            pass
        try:
            resp = await self._mobile_api(
                "/c/f/pb/page", {"kz": tid, "pn": "1", "rn": str(max(limit, 30))}
            )
            return self._parse_posts(resp)[:limit]
        except Exception as e:
            raise QrLoginError(f"获取帖子 {tid} 楼层失败: {e}")

    def _parse_posts(self, resp: dict) -> list:
        data = resp.get("data") or resp or {}
        posts = []
        # 主楼正文
        thread = data.get("thread") or resp.get("thread") or {}
        origin = thread.get("origin_thread_info") or {}
        if origin:
            posts.append(
                {
                    "pid": "0",
                    "floor": "0",
                    "content": content_to_text(origin.get("content")),
                    "title": str(origin.get("title") or ""),
                    "author": pick_author(origin),
                    "post_time": str(origin.get("time") or ""),
                }
            )
        # 楼层列表
        post_list = data.get("post_list") or resp.get("post_list") or []
        for item in post_list:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if content is None:
                content = item.get("text") or ""
            text = content_to_text(content)
            posts.append(
                {
                    "pid": str(item.get("id") or item.get("pid") or ""),
                    "floor": str(item.get("floor") or ""),
                    "content": text,
                    "title": str(item.get("title") or ""),
                    "author": pick_author(item),
                    "post_time": str(item.get("time") or ""),
                }
            )
        return [p for p in posts if p["content"]]
