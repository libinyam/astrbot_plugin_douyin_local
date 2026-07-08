from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import unquote, urljoin

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DOUYIN_URL_RE = re.compile(
    r"https?://(?:(?:v|www|m|jx)\.douyin\.com|(?:www\.)?iesdouyin\.com|aweme\.snssdk\.com)/[^\s<>'\"]+",
    re.IGNORECASE,
)

AWEME_ID_PATTERNS = [
    re.compile(r"/(?:video|note|slides)/(?:share/)?(\d{15,25})", re.IGNORECASE),
    re.compile(r"/share/(?:video|note|slides)/(\d{15,25})", re.IGNORECASE),
    re.compile(r"(?:modal_id|aweme_id|item_id|item_ids)=['\"]?(\d{15,25})", re.IGNORECASE),
    re.compile(r"['\"](?:aweme_id|awemeId|item_id|itemId)['\"]\s*:\s*['\"]?(\d{15,25})", re.IGNORECASE),
]

# 纯 ID 匹配（18~20 位数字）
RAW_ID_RE = re.compile(r"(?<!\d)(\d{18,20})(?!\d)")

TRAILING_URL_CHARS = " \t\r\n\"'<>)]}，。！？、；;,.!?"

# 移动端 UA（访问 iesdouyin 分享页必须用移动端 UA 才能拿到 _ROUTER_DATA）
IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.6 Mobile/15E148 Safari/604.1 Edg/132.0.0.0"
)

ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 15; SM-G998B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Mobile Safari/537.36 Edg/132.0.0.0"
)

SHARE_HEADERS = {
    "User-Agent": IOS_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.iesdouyin.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

SLIDES_HEADERS = {
    "User-Agent": ANDROID_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.iesdouyin.com/",
}

# 视频清晰度探测列表（从高到低）
PLAY_RATIOS = ("1080p", "720p", "540p", "360p")

# ttwid 注册接口
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_REGISTER_BODY = json.dumps({
    "region": "cn",
    "aid": 1768,
    "needFid": False,
    "service": "www.iesdouyin.com",
    "union": True,
    "fid": "",
})


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

class DouyinParseError(RuntimeError):
    """抖音解析失败时抛出。"""


@dataclass(slots=True)
class ParsedDouyinItem:
    item_id: str
    content_type: str          # "video" | "images"
    title: str
    author: str
    source_url: str
    resolved_url: str
    video_url: str = ""
    image_urls: list[str] = field(default_factory=list)
    cover_url: str = ""
    api_source: str = ""

    @property
    def is_video(self) -> bool:
        return self.content_type == "video" and bool(self.video_url)

    @property
    def is_images(self) -> bool:
        return self.content_type == "images" and bool(self.image_urls)


# ---------------------------------------------------------------------------
# 公开工具函数
# ---------------------------------------------------------------------------

def extract_douyin_url(text: str) -> str | None:
    """从任意消息文本中提取第一个抖音链接。"""
    if not text:
        return None
    match = DOUYIN_URL_RE.search(text)
    if not match:
        # 尝试纯数字 ID（分享文本中可能只有 aweme_id）
        id_match = RAW_ID_RE.search(text)
        if id_match:
            return f"https://www.iesdouyin.com/share/video/{id_match.group(1)}/"
        return None
    return _clean_text_url(match.group(0))


def extract_aweme_id(text: str) -> str | None:
    """从 URL、HTML 片段或 JSON 字符串中提取抖音作品 ID。"""
    if not text:
        return None
    decoded = html.unescape(unquote(text))
    for pattern in AWEME_ID_PATTERNS:
        match = pattern.search(decoded)
        if match:
            return match.group(1)
    # 纯 ID
    id_match = RAW_ID_RE.search(decoded)
    if id_match:
        return id_match.group(1)
    return None


# ---------------------------------------------------------------------------
# 解析器主类
# ---------------------------------------------------------------------------

class DouyinParser:
    """通过 iesdouyin 移动端分享页解析公开抖音视频/图集。

    不依赖 a_bogus / X-Bogus 签名，不调用第三方解析站。
    仅需一个可免费注册的匿名 ttwid cookie。
    """

    def __init__(self, timeout: float = 20, cookie: str = "") -> None:
        self.timeout = timeout
        # 用户传入的 cookie 可能包含 ttwid，优先使用
        self.cookie = cookie.strip()
        self._ttwid: str = ""

    async def parse(self, url_or_text: str) -> ParsedDouyinItem:
        source_url = extract_douyin_url(url_or_text) or _clean_text_url(url_or_text)
        if not source_url:
            raise DouyinParseError("没有找到抖音链接")

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout),
        ) as client:
            # 1) 如果用户提供了 cookie，直接用；否则自动注册匿名 ttwid
            await self._ensure_ttwid(client)

            # 2) 从短链/URL 中提取 item_id
            item_id = await self._resolve_item_id(client, source_url)
            if not item_id:
                raise DouyinParseError("无法从链接中提取作品 ID")

            # 3) 尝试解析视频分享页
            try:
                item = await self._parse_share_page(client, item_id, source_url)
                if item:
                    return item
            except DouyinParseError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DouyinParseError(f"解析分享页失败: {exc}") from exc

            # 4) 尝试图集接口
            try:
                item = await self._parse_slides(client, item_id, source_url)
                if item:
                    return item
            except Exception:  # noqa: BLE001
                pass

            raise DouyinParseError(
                f"未能从抖音公开页面提取媒体信息（item_id={item_id}）。"
                "可能原因：作品已被删除/设为私密，或抖音页面结构已变更。"
            )

    # -- ttwid 注册 --------------------------------------------------------

    async def _ensure_ttwid(self, client: httpx.AsyncClient) -> None:
        """从用户 cookie 中提取 ttwid，或注册匿名 ttwid。"""
        if self._ttwid:
            return

        # 尝试从用户传入的 cookie 中提取
        if self.cookie:
            for part in self.cookie.split(";"):
                part = part.strip()
                if part.startswith("ttwid="):
                    self._ttwid = part[len("ttwid="):]
                    return

        # 注册匿名 ttwid
        try:
            resp = await client.post(
                TTWID_REGISTER_URL,
                content=TTWID_REGISTER_BODY,
                headers={
                    "Content-Type": "application/json",
                    "Referer": "https://www.iesdouyin.com/",
                    "User-Agent": IOS_UA,
                },
                follow_redirects=False,
            )
            # 从 Set-Cookie 头提取 ttwid
            set_cookie = resp.headers.get("set-cookie", "")
            ttwid_match = re.search(r"ttwid=([^;]+)", set_cookie)
            if ttwid_match:
                self._ttwid = ttwid_match.group(1)
        except Exception:  # noqa: BLE001
            pass  # ttwid 注册失败不致命，继续尝试无 cookie 请求

    def _build_cookie_header(self, extra: str = "") -> str:
        parts = []
        if self._ttwid:
            parts.append(f"ttwid={self._ttwid}")
        if self.cookie:
            parts.append(self.cookie)
        if extra:
            parts.append(extra)
        return "; ".join(parts)

    # -- item_id 解析 -------------------------------------------------------

    async def _resolve_item_id(self, client: httpx.AsyncClient, url: str) -> str | None:
        """从短链或完整 URL 中解析出 item_id。

        对于 v.douyin.com 短链，先不跟随重定向拿 Location 头。
        """
        # 先直接从 URL 中提取
        item_id = extract_aweme_id(url)
        if item_id:
            return item_id

        # 短链重定向解析
        headers = dict(SHARE_HEADERS)
        cookie = self._build_cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        try:
            resp = await client.get(
                url,
                headers=headers,
                follow_redirects=False,
            )
            # 从 Location 头提取
            location = resp.headers.get("location", "")
            if location:
                item_id = extract_aweme_id(location)
                if item_id:
                    return item_id
        except Exception:  # noqa: BLE001
            pass

        # 跟随重定向后再试
        try:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            item_id = extract_aweme_id(str(resp.url))
            if item_id:
                return item_id
            item_id = extract_aweme_id(resp.text or "")
            if item_id:
                return item_id
        except Exception:  # noqa: BLE001
            pass

        return None

    # -- 分享页解析（核心） -------------------------------------------------

    async def _parse_share_page(
        self,
        client: httpx.AsyncClient,
        item_id: str,
        source_url: str,
    ) -> ParsedDouyinItem | None:
        """通过 iesdouyin 移动端分享页提取 window._ROUTER_DATA。"""
        share_url = f"https://www.iesdouyin.com/share/video/{item_id}/"

        headers = dict(SHARE_HEADERS)
        cookie = self._build_cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        resp = await client.get(share_url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        page_text = resp.text or ""

        router_data = _extract_router_data(page_text)
        if not router_data:
            return None

        # 尝试从 loaderData 中查找作品数据
        loader_data = router_data.get("loaderData") or {}
        item = _find_item_in_router_data(loader_data, item_id)
        if not item:
            return None

        return self._normalize_item(
            item,
            source_url=source_url,
            resolved_url=share_url,
            item_id=item_id,
            api_source="iesdouyin-share",
            client=client,
        )

    # -- 图集解析 -----------------------------------------------------------

    async def _parse_slides(
        self,
        client: httpx.AsyncClient,
        item_id: str,
        source_url: str,
    ) -> ParsedDouyinItem | None:
        """通过 iesdouyin v2 slidesinfo 接口解析图集。"""
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{item_id}]",
            "request_source": "200",
        }
        headers = dict(SLIDES_HEADERS)
        cookie = self._build_cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()

        data = _json_from_response(resp)
        if not isinstance(data, dict):
            return None

        details = data.get("aweme_details") or data.get("aweme_list") or []
        if not details:
            return None

        item = details[0] if isinstance(details, list) else details
        if not isinstance(item, dict):
            return None

        return self._normalize_item(
            item,
            source_url=source_url,
            resolved_url=url,
            item_id=item_id,
            api_source="iesdouyin-slides",
            client=client,
        )

    # -- 标准化 -------------------------------------------------------------

    def _normalize_item(
        self,
        item: dict[str, Any],
        source_url: str,
        resolved_url: str,
        item_id: str | None,
        api_source: str,
        client: httpx.AsyncClient | None = None,
    ) -> ParsedDouyinItem:
        normalized_id = item_id or _dict_id(item) or ""
        title = _first_text(
            item.get("desc"),
            item.get("caption"),
            item.get("preview_title"),
            item.get("item_title"),
            _get_path(item, "share_info.share_title"),
            _get_path(item, "shareInfo.shareTitle"),
            _get_path(item, "seo_info.ocr_content"),
        )
        author = _first_text(
            _get_path(item, "author.nickname"),
            _get_path(item, "author.unique_id"),
            _get_path(item, "author.sec_uid"),
            item.get("author_name"),
        )

        image_urls = _extract_image_urls(item)
        video_url = _extract_video_url(item)
        cover_url = _extract_cover_url(item)

        if image_urls:
            content_type = "images"
        elif video_url:
            content_type = "video"
        else:
            raise DouyinParseError("找到了作品数据，但没有找到可发送的视频或图片地址")

        return ParsedDouyinItem(
            item_id=normalized_id,
            content_type=content_type,
            title=title,
            author=author,
            source_url=source_url,
            resolved_url=resolved_url,
            video_url=video_url,
            image_urls=image_urls,
            cover_url=cover_url,
            api_source=api_source,
        )


# ---------------------------------------------------------------------------
# _ROUTER_DATA 提取
# ---------------------------------------------------------------------------

def _extract_router_data(page_text: str) -> dict[str, Any] | None:
    """从 iesdouyin 分享页 HTML 中提取 window._ROUTER_DATA 的 JSON。"""
    if not page_text:
        return None

    # 匹配 window._ROUTER_DATA = {...}</script>
    # 用 DOTALL 匹配跨行，匹配到 </script> 为止
    pattern = re.compile(
        r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
        re.DOTALL,
    )
    match = pattern.search(page_text)
    if not match:
        # 备用：尝试 __UNIVERSAL_DATA_FOR_REHYDRATION__（PC 端结构）
        return _extract_universal_data(page_text)

    raw = match.group(1).strip()
    return _try_parse_json(raw)


def _extract_universal_data(page_text: str) -> dict[str, Any] | None:
    """尝试从 PC 端 __UNIVERSAL_DATA_FOR_REHYDRATION__ 提取数据。"""
    pattern = re.compile(
        r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(page_text)
    if not match:
        return None
    raw = match.group(1).strip()
    return _try_parse_json(raw)


def _try_parse_json(raw: str) -> dict[str, Any] | None:
    """尝试多种方式解析 JSON 字符串。"""
    candidates = [
        raw,
        html.unescape(raw),
        unquote(raw),
        unquote(html.unescape(raw)),
        _decode_js_string(raw),
        _decode_js_string(html.unescape(raw)),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        text = candidate.strip().rstrip(";").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _find_item_in_router_data(loader_data: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    """在 loaderData 中查找作品数据。

    iesdouyin 分享页的 key 格式为 "video_(id)/page" 或 "note_(id)/page"。
    """
    # 精确匹配 key
    for key in (f"video_({item_id})/page", f"note_({item_id})/page"):
        page_data = loader_data.get(key)
        if isinstance(page_data, dict):
            item = _extract_item_from_page_data(page_data)
            if item:
                return item

    # 模糊匹配（key 可能不含括号）
    for key, value in loader_data.items():
        if not isinstance(value, dict):
            continue
        if item_id in key and ("video" in key or "note" in key):
            item = _extract_item_from_page_data(value)
            if item:
                return item

    # 遍历所有 loaderData 值
    for value in loader_data.values():
        if not isinstance(value, dict):
            continue
        item = _extract_item_from_page_data(value)
        if item:
            return item

    return None


def _extract_item_from_page_data(page_data: dict[str, Any]) -> dict[str, Any] | None:
    """从 loaderData 的单个 page 数据中提取 aweme item。"""
    # 路径 1: videoInfoRes.item_list[0]
    video_info = page_data.get("videoInfoRes") or page_data.get("video_info_res")
    if isinstance(video_info, dict):
        item_list = video_info.get("item_list") or video_info.get("itemList") or []
        if item_list and isinstance(item_list, list):
            return item_list[0]

    # 路径 2: itemInfo.itemStruct / itemInfo.item_struct
    item_info = page_data.get("itemInfo") or page_data.get("item_info")
    if isinstance(item_info, dict):
        item_struct = item_info.get("itemStruct") or item_info.get("item_struct")
        if isinstance(item_struct, dict):
            return item_struct

    # 路径 3: 直接是 item
    if _looks_like_media_item(page_data):
        return page_data

    return None


# ---------------------------------------------------------------------------
# URL 提取与标准化（含无水印逻辑）
# ---------------------------------------------------------------------------

def _extract_video_url(item: dict[str, Any]) -> str:
    """从 item JSON 中提取视频 URL。优先返回无水印链接。"""
    video = item.get("video") or item.get("videoInfo") or item.get("video_data")
    if not isinstance(video, dict):
        return ""

    containers: list[Any] = []
    preferred_keys = [
        "play_addr", "playAddr",
        "play_addr_h264", "playAddrH264",
        "download_addr", "downloadAddr",
        "play_api", "playApi",
        "h264_play_addr", "h264PlayAddr",
    ]
    for key in preferred_keys:
        containers.append(video.get(key))

    for bit_rate in video.get("bit_rate") or video.get("bitRate") or []:
        if isinstance(bit_rate, dict):
            containers.append(bit_rate.get("play_addr") or bit_rate.get("playAddr"))

    urls = _urls_from_containers(containers)

    # 如果拿到了 play_addr.uri 但不是完整 URL，构造 aweme.snssdk.com 播放链接
    # play_addr.uri 通常是纯 video_id，用它构造无水印播放链接
    uri = _get_path(video, "play_addr.uri") or _get_path(video, "playAddr.uri")
    if uri and not any(u.startswith(("http://", "https://")) for u in urls):
        if re.fullmatch(r"\w+", str(uri)):
            # 用 play 而不是 playwm，获取无水印视频
            urls.append(f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p")

    return _choose_best_url(urls)


def _extract_image_urls(item: dict[str, Any]) -> list[str]:
    """从 item JSON 中提取图集图片 URL 列表。优先返回无水印原图。"""
    images: list[Any] = []
    image_post_info = item.get("image_post_info") or item.get("imagePostInfo")
    if isinstance(image_post_info, dict):
        images.extend(image_post_info.get("images") or [])

    if isinstance(item.get("images"), list):
        images.extend(item["images"])
    if isinstance(item.get("image_infos"), list):
        images.extend(item["image_infos"])
    if isinstance(item.get("imageInfos"), list):
        images.extend(item["imageInfos"])

    # iesdouyin slidesinfo 返回的图片结构
    if isinstance(item.get("images"), list):
        for img in item["images"]:
            if isinstance(img, dict):
                url_list = img.get("url_list") or img.get("urlList")
                if isinstance(url_list, list) and url_list:
                    images.append({"url_list": url_list})

    result: list[str] = []
    for image_item in images:
        url = ""
        if isinstance(image_item, dict):
            # 按优先级依次尝试：origin_image（无水印原图）> display_image > 其他
            priority_keys = [
                "origin_image", "originImage",      # 优先：无水印原图
                "display_image", "displayImage",     # 其次：展示图（可能有水印）
                "image",                             # 通用
                "url_list", "urlList",              # 直接 URL 列表
                "urls", "download_url_list",
            ]
            for key in priority_keys:
                container = image_item.get(key)
                if container:
                    candidate = _choose_best_url(_urls_from_containers([container]))
                    if candidate:
                        url = candidate
                        break
        else:
            url = _choose_best_url(_urls_from_containers([image_item]))

        if url and url not in result:
            result.append(url)

    return result


def _extract_cover_url(item: dict[str, Any]) -> str:
    video = item.get("video") or item.get("videoInfo") or item.get("video_data")
    containers: list[Any] = []
    if isinstance(video, dict):
        for key in ("cover", "origin_cover", "originCover", "dynamic_cover", "dynamicCover"):
            containers.append(video.get(key))
    for key in ("cover", "coverUrl", "cover_url"):
        containers.append(item.get(key))
    return _choose_best_url(_urls_from_containers(containers))


def _urls_from_containers(containers: Iterable[Any]) -> list[str]:
    urls: list[str] = []
    for container in containers:
        if not container:
            continue
        if isinstance(container, str):
            urls.append(container)
        elif isinstance(container, list):
            urls.extend(str(url) for url in container if url)
        elif isinstance(container, dict):
            for key in ("url_list", "urlList", "urls", "download_url_list"):
                value = container.get(key)
                if isinstance(value, list):
                    urls.extend(str(url) for url in value if url)
            for key in ("url", "uri"):
                value = container.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    urls.append(value)
    return [_normalize_media_url(url) for url in urls if url]


def _normalize_media_url(url: str) -> str:
    text = html.unescape(url).replace("\\u0026", "&").strip()
    if text.startswith("//"):
        text = "https:" + text
    if text.startswith("/") and not text.startswith("//"):
        text = urljoin("https://www.douyin.com", text)
    return text


def _choose_best_url(urls: Iterable[str]) -> str:
    """选择最佳 URL，优先无水印链接。"""
    unique: list[str] = []
    for url in urls:
        if url.startswith(("http://", "https://")) and url not in unique:
            unique.append(url)
    if not unique:
        return ""

    def score(url: str) -> tuple[int, int, int]:
        lower = url.lower()
        return (
            1 if lower.startswith("https://") else 0,
            # 优先无水印：不含 watermark、playwm 的链接得分更高
            1 if "watermark" not in lower and "playwm" not in lower else 0,
            len(url),
        )

    return sorted(unique, key=score, reverse=True)[0]


# ---------------------------------------------------------------------------
# JSON 工具函数
# ---------------------------------------------------------------------------

def _json_from_response(resp: httpx.Response) -> Any:
    text = resp.text.strip()
    if not text:
        raise DouyinParseError(f"接口返回空内容，HTTP {resp.status_code}")

    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        content_type = resp.headers.get("content-type", "unknown")
        snippet = re.sub(r"\s+", " ", text[:120])
        if "<html" in text[:300].lower() or "<!doctype" in text[:300].lower():
            snippet = "HTML 页面，可能被风控或接口已变更"
        raise DouyinParseError(
            f"接口未返回 JSON，HTTP {resp.status_code}, Content-Type: {content_type}, {snippet}"
        ) from exc


def _decode_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _dict_id(item: dict[str, Any]) -> str:
    for key in ("aweme_id", "awemeId", "item_id", "itemId", "id", "id_str", "group_id", "groupId"):
        value = item.get(key)
        if value is not None and re.fullmatch(r"\d{10,25}", str(value)):
            return str(value)
    return ""


def _looks_like_media_item(item: dict[str, Any]) -> bool:
    has_title_or_author = any(
        key in item
        for key in (
            "desc", "caption", "author", "authorInfo",
            "share_info", "shareInfo", "video", "videoInfo",
        )
    )
    return has_title_or_author and (
        isinstance(item.get("video"), dict)
        or isinstance(item.get("videoInfo"), dict)
        or isinstance(item.get("image_post_info"), dict)
        or isinstance(item.get("imagePostInfo"), dict)
        or isinstance(item.get("images"), list)
    )


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _get_path(value: dict[str, Any], dotted_path: str) -> Any:
    current: Any = value
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _clean_text_url(value: str) -> str:
    return html.unescape(value or "").strip().rstrip(TRAILING_URL_CHARS)
