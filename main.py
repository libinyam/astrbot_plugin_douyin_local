from __future__ import annotations

import base64

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

try:
    from .douyin_parser import DouyinParseError, DouyinParser, extract_douyin_url
except ImportError:  # pragma: no cover - AstrBot loaders differ between versions.
    from douyin_parser import DouyinParseError, DouyinParser, extract_douyin_url

import httpx


@register(
    "astrbot_plugin_douyin_local",
    "libinyam",
    "自动解析公开抖音视频/图集链接，不依赖第三方解析站",
    "v0.2.0",
)
class LocalDouyinPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    def _check_permission(self, event: AstrMessageEvent):
        if self.config.get("enable_id_whitelist", False):
            allowed_ids = self.config.get("id_whitelist", [])
            origin = getattr(event, "unified_msg_origin", "")
            if allowed_ids and origin not in allowed_ids:
                reply = self.config.get("whitelist_reply", "")
                if reply:
                    return event.plain_result(reply)
                return "BLOCKED"

        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", None)
        if group_id:
            group_whitelist = self.config.get("group_whitelist", [])
            allowed_groups = [str(item) for item in group_whitelist]
            if allowed_groups and str(group_id) not in allowed_groups:
                return "BLOCKED"
        elif not self.config.get("enable_in_private", True):
            return "BLOCKED"

        return None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1)
    async def on_message(self, event: AstrMessageEvent):
        url = extract_douyin_url(event.message_str)
        if not url:
            return

        denied = self._check_permission(event)
        if denied is not None:
            if denied != "BLOCKED":
                yield denied
            return

        logger.info(f"检测到抖音链接: {url}")
        parser = DouyinParser(
            timeout=_as_float(self.config.get("timeout_seconds", 20), 20),
            cookie=str(self.config.get("douyin_cookie", "") or ""),
        )

        try:
            item = await parser.parse(url)
        except DouyinParseError as exc:
            logger.warning(f"抖音解析失败: {exc}")
            if self.config.get("reply_on_failure", True):
                yield event.plain_result(f"抖音解析失败: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - keep bot alive on parser/network changes.
            logger.error(f"抖音解析异常: {exc}")
            if self.config.get("reply_on_failure", True):
                yield event.plain_result(f"抖音解析异常: {exc}")
            return

        info = _format_item_info(item)
        if info:
            yield event.plain_result(info)

        # 抖音 CDN 要求带 Referer 才能下载，统一用 _download_media_bytes 下载为 bytes
        download_referer = item.resolved_url or "https://www.iesdouyin.com/"
        timeout = _as_float(self.config.get("timeout_seconds", 20), 20)

        if item.is_video:
            # 下载视频为 bytes，用 fromBase64 发送（避免跨容器文件路径问题）
            video_bytes = await _download_media_bytes(item.video_url, download_referer, timeout)
            if video_bytes:
                b64 = base64.b64encode(video_bytes).decode()
                yield event.chain_result([Comp.Video.fromBase64(b64)])
            else:
                # 下载失败，回退到 URL 方式
                yield event.chain_result([Comp.Video.fromURL(item.video_url)])
            return

        if item.is_images:
            image_urls = item.image_urls or []
            max_images = max(1, _as_int(self.config.get("max_images", 12), 12))
            forward_threshold = max(1, _as_int(self.config.get("forward_threshold", 3), 3))

            images_to_send = image_urls[:max_images]

            # 下载所有图片为 bytes（带 Referer）
            downloaded = []  # [(is_bytes, bytes_or_url), ...]
            for img_url in images_to_send:
                img_bytes = await _download_media_bytes(img_url, download_referer, timeout)
                if img_bytes:
                    downloaded.append((True, img_bytes))
                else:
                    downloaded.append((False, img_url))

            if len(downloaded) > forward_threshold:
                # 超过阈值，用合并转发（聊天记录）形式发送
                try:
                    nodes = []
                    for is_bytes, data in downloaded:
                        if is_bytes:
                            img_comp = Comp.Image.fromBytes(data)
                        else:
                            img_comp = Comp.Image.fromURL(data)
                        nodes.append(Comp.Node(
                            content=[img_comp],
                            uin="0",
                            name="抖音图集",
                        ))
                    yield event.chain_result([Comp.Nodes(nodes=nodes)])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"合并转发发送失败，回退到逐张发送: {exc}")
                    for is_bytes, data in downloaded:
                        if is_bytes:
                            yield event.chain_result([Comp.Image.fromBytes(data)])
                        else:
                            yield event.chain_result([Comp.Image.fromURL(data)])
            else:
                # 未超过阈值，逐张发送
                for is_bytes, data in downloaded:
                    if is_bytes:
                        yield event.chain_result([Comp.Image.fromBytes(data)])
                    else:
                        yield event.chain_result([Comp.Image.fromURL(data)])

            if len(image_urls) > max_images:
                yield event.plain_result(
                    f"图集共有 {len(image_urls)} 张，已按配置发送前 {max_images} 张。"
                )
            return

        yield event.plain_result("抖音解析成功，但没有可发送的视频或图片。")

    async def terminate(self):
        pass


def _format_item_info(item) -> str:
    type_name = "视频" if item.is_video else "图集" if item.is_images else "作品"
    parts = [f"抖音{type_name}解析成功"]
    if item.author:
        parts.append(f"作者: {item.author}")
    if item.title:
        parts.append(item.title)
    return "\n".join(parts)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def _download_media_bytes(url: str, referer: str, timeout: float) -> bytes | None:
    """下载抖音媒体文件为 bytes。

    抖音 CDN 要求带 Referer 头，否则返回 403。
    用 bytes 方式传输避免跨容器文件路径问题。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/16.6 Mobile/15E148 Safari/604.1"
        ),
        "Referer": referer or "https://www.iesdouyin.com/",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers=headers,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"媒体下载失败，回退到 URL 方式: {exc}")
        return None
