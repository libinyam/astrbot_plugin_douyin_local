from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import unquote, urljoin

import httpx


DOUYIN_URL_RE = re.compile(
    r"https?://(?:(?:v|www|m)\.douyin\.com|(?:www\.)?iesdouyin\.com)/[^\s<>'\"]+",
    re.IGNORECASE,
)

AWEME_ID_PATTERNS = [
    re.compile(r"/(?:video|note)/(\d{10,25})", re.IGNORECASE),
    re.compile(r"/share/(?:video|note)/(\d{10,25})", re.IGNORECASE),
    re.compile(r"(?:modal_id|aweme_id|item_id|item_ids)=['\"]?(\d{10,25})", re.IGNORECASE),
    re.compile(r"['\"](?:aweme_id|awemeId|item_id|itemId)['\"]\s*:\s*['\"]?(\d{10,25})", re.IGNORECASE),
]

TRAILING_URL_CHARS = " \t\r\n\"'<>)]}，。！？、；;,.!?"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Referer": "https://www.douyin.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


class DouyinParseError(RuntimeError):
    """Raised when a public Douyin link cannot be parsed."""


@dataclass(slots=True)
class ParsedDouyinItem:
    item_id: str
    content_type: str
    title: str
    author: str
    source_url: str
    resolved_url: str
    video_url: str = ""
    image_urls: list[str] | None = None
    cover_url: str = ""
    api_source: str = ""

    @property
    def is_video(self) -> bool:
        return self.content_type == "video" and bool(self.video_url)

    @property
    def is_images(self) -> bool:
        return self.content_type == "images" and bool(self.image_urls)


def extract_douyin_url(text: str) -> str | None:
    """Extract the first Douyin share/page URL from arbitrary message text."""
    if not text:
        return None
    match = DOUYIN_URL_RE.search(text)
    if not match:
        return None
    return _clean_text_url(match.group(0))


def extract_aweme_id(text: str) -> str | None:
    """Extract a Douyin item id from a URL, HTML snippet, or JSON string."""
    if not text:
        return None
    decoded = html.unescape(unquote(text))
    for pattern in AWEME_ID_PATTERNS:
        match = pattern.search(decoded)
        if match:
            return match.group(1)
    return None


class DouyinParser:
    """Parse public Douyin video/image posts without a third-party parser API.

    Douyin's public web payload changes over time, so this parser intentionally
    uses multiple strategies and fails loudly when none of them work.
    """

    def __init__(self, timeout: float = 20, cookie: str = "") -> None:
        self.timeout = timeout
        self.cookie = cookie.strip()

    async def parse(self, url_or_text: str) -> ParsedDouyinItem:
        source_url = extract_douyin_url(url_or_text) or _clean_text_url(url_or_text)
        if not source_url:
            raise DouyinParseError("没有找到抖音链接")

        headers = dict(DEFAULT_HEADERS)
        if self.cookie:
            headers["Cookie"] = self.cookie

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout),
        ) as client:
            first_resp = await self._get(client, source_url)
            resolved_url = str(first_resp.url)
            page_text = first_resp.text or ""
            item_id = extract_aweme_id(resolved_url) or extract_aweme_id(page_text)

            page_item = self._extract_item_from_page(page_text, item_id)
            if page_item:
                return self._normalize_item(
                    page_item,
                    source_url=source_url,
                    resolved_url=resolved_url,
                    item_id=item_id,
                    api_source="page-state",
                )

            if not item_id:
                raise DouyinParseError("已打开链接，但没有找到作品 ID")

            errors: list[str] = []
            page_candidates = list(self._page_candidates(item_id))
            for source, page_url, page_headers in page_candidates:
                try:
                    resp = await self._get(client, page_url, headers=page_headers)
                    item = self._extract_item_from_page(resp.text or "", item_id)
                    if item:
                        return self._normalize_item(
                            item,
                            source_url=source_url,
                            resolved_url=str(resp.url),
                            item_id=item_id,
                            api_source=source,
                        )
                    errors.append(f"{source}: 没有媒体数据")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{source}: {exc}")

            for source, endpoint, params in self._api_candidates(item_id):
                try:
                    resp = await self._get(
                        client,
                        endpoint,
                        params=params,
                        headers={"Referer": resolved_url},
                    )
                    data = _json_from_response(resp)
                    item = self._extract_item_from_json(data, item_id)
                    if item:
                        return self._normalize_item(
                            item,
                            source_url=source_url,
                            resolved_url=resolved_url,
                            item_id=item_id,
                            api_source=source,
                        )
                    errors.append(f"{source}: 没有媒体数据")
                except Exception as exc:  # noqa: BLE001 - keep parser failure readable to users.
                    errors.append(f"{source}: {exc}")

            discover_item = await self._try_discover_page(client, item_id, resolved_url, errors)
            if discover_item:
                return self._normalize_item(
                    discover_item,
                    source_url=source_url,
                    resolved_url=resolved_url,
                    item_id=item_id,
                    api_source="discover-page",
                )

            detail = "；".join(errors[-3:])
            raise DouyinParseError(f"未能从抖音公开页面或接口提取媒体信息。{detail}")

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp

    def _api_candidates(self, item_id: str) -> Iterable[tuple[str, str, dict[str, str]]]:
        common = {
            "item_ids": item_id,
        }
        yield (
            "iesdouyin-iteminfo",
            "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/",
            common,
        )
        yield (
            "douyin-iteminfo",
            "https://www.douyin.com/web/api/v2/aweme/iteminfo/",
            common,
        )
        yield (
            "douyin-aweme-detail",
            "https://www.douyin.com/aweme/v1/web/aweme/detail/",
            {
                "aweme_id": item_id,
                "aid": "6383",
                "device_platform": "webapp",
                "channel": "channel_pc_web",
                "pc_client_type": "1",
                "version_code": "170400",
            },
        )
        yield (
            "douyin-light-aweme-detail",
            "https://www.douyin.com/aweme/v1/web/aweme/detail/",
            {
                "aweme_id": item_id,
                "aid": "6383",
                "device_platform": "webapp",
            },
        )

    def _page_candidates(self, item_id: str) -> Iterable[tuple[str, str, dict[str, str]]]:
        mobile_headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.douyin.com/",
        }
        for kind in ("video", "note"):
            yield (
                f"iesdouyin-share-{kind}",
                f"https://www.iesdouyin.com/share/{kind}/{item_id}/",
                mobile_headers,
            )
            yield (
                f"m-douyin-share-{kind}",
                f"https://m.douyin.com/share/{kind}/{item_id}",
                mobile_headers,
            )

    async def _try_discover_page(
        self,
        client: httpx.AsyncClient,
        item_id: str,
        referer: str,
        errors: list[str],
    ) -> dict[str, Any] | None:
        try:
            resp = await self._get(
                client,
                "https://www.douyin.com/discover",
                params={"modal_id": item_id},
                headers={"Referer": referer},
            )
            return self._extract_item_from_page(resp.text or "", item_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"discover-page: {exc}")
            return None

    def _extract_item_from_page(self, page_text: str, item_id: str | None) -> dict[str, Any] | None:
        for blob in _extract_json_blobs(page_text):
            item = self._extract_item_from_json(blob, item_id)
            if item:
                return item
        return None

    def _extract_item_from_json(self, data: Any, item_id: str | None) -> dict[str, Any] | None:
        for candidate in _iter_dicts(data):
            if item_id and _dict_id(candidate) == item_id and _looks_like_media_item(candidate):
                return candidate

        for candidate in _iter_dicts(data):
            if _looks_like_media_item(candidate):
                return candidate

        return None

    def _normalize_item(
        self,
        item: dict[str, Any],
        source_url: str,
        resolved_url: str,
        item_id: str | None,
        api_source: str,
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


def _clean_text_url(value: str) -> str:
    return html.unescape(value or "").strip().rstrip(TRAILING_URL_CHARS)


def _extract_json_blobs(page_text: str) -> Iterable[Any]:
    if not page_text:
        return

    script_patterns = [
        re.compile(
            r"<script[^>]+id=[\"'](?:RENDER_DATA|SSR_RENDER_DATA|__UNIVERSAL_DATA_FOR_REHYDRATION__)[\"'][^>]*>(.*?)</script>",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"window\.(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*(\{.*?\})\s*;?\s*</script>",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"window\.(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*JSON\.parse\((['\"])(.*?)\1\)",
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    for pattern in script_patterns:
        for match in pattern.finditer(page_text):
            raw = match.group(2 if len(match.groups()) >= 2 and match.group(2) else 1).strip()
            for candidate in _json_decode_candidates(raw):
                yield candidate


def _json_decode_candidates(raw: str) -> Iterable[Any]:
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
        text = candidate.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            continue


def _decode_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


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
            "desc",
            "caption",
            "author",
            "authorInfo",
            "share_info",
            "shareInfo",
            "video",
            "videoInfo",
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


def _extract_video_url(item: dict[str, Any]) -> str:
    video = item.get("video") or item.get("videoInfo") or item.get("video_data")
    if not isinstance(video, dict):
        return ""

    containers: list[Any] = []
    preferred_keys = [
        "play_addr",
        "playAddr",
        "play_addr_h264",
        "playAddrH264",
        "download_addr",
        "downloadAddr",
        "play_api",
        "playApi",
        "h264_play_addr",
        "h264PlayAddr",
    ]
    for key in preferred_keys:
        containers.append(video.get(key))

    for bit_rate in video.get("bit_rate") or video.get("bitRate") or []:
        if isinstance(bit_rate, dict):
            containers.append(bit_rate.get("play_addr") or bit_rate.get("playAddr"))

    urls = [url for url in _urls_from_containers(containers) if _looks_like_video_url(url)]
    return _prefer_non_watermark_video(_choose_best_url(urls))


def _extract_image_urls(item: dict[str, Any]) -> list[str]:
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

    result: list[str] = []
    for image_item in images:
        containers = []
        if isinstance(image_item, dict):
            for key in (
                "display_image",
                "displayImage",
                "origin_image",
                "originImage",
                "image",
                "url_list",
                "urlList",
                "urls",
                "download_url_list",
            ):
                containers.append(image_item.get(key))
        else:
            containers.append(image_item)

        url = _choose_best_url(_urls_from_containers(containers))
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
    if text.startswith("/"):
        text = urljoin("https://www.douyin.com", text)
    return text


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


def _choose_best_url(urls: Iterable[str]) -> str:
    unique = []
    for url in urls:
        if url.startswith(("http://", "https://")) and url not in unique:
            unique.append(url)
    if not unique:
        return ""

    def score(url: str) -> tuple[int, int, int]:
        lower = url.lower()
        return (
            1 if lower.startswith("https://") else 0,
            1 if "watermark" not in lower and "playwm" not in lower else 0,
            len(url),
        )

    return sorted(unique, key=score, reverse=True)[0]


def _looks_like_video_url(url: str) -> bool:
    lower = url.lower()
    if any(marker in lower for marker in ("/aweme/v1/play", "/aweme/v1/playwm", "douyinvod.com")):
        return True
    if any(ext in lower for ext in (".mp4", ".mov", ".m3u8")):
        return True
    if any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic")):
        return False
    return "video_id=" in lower or "mime_type=video" in lower


def _prefer_non_watermark_video(url: str) -> str:
    if not url:
        return ""
    return url.replace("/playwm/", "/play/")
