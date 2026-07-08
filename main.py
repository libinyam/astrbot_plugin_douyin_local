from __future__ import annotations

import os
import tempfile
from pathlib import Path

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

        if item.is_video:
            # 抖音视频链接需要带 Referer 才能下载，先下载到临时文件再发送
            video_file = await _download_video(
                item.video_url,
                item.resolved_url,
                _as_float(self.config.get("timeout_seconds", 20), 20),
            )
            if video_file:
                yield event.chain_result([Comp.File(video_file, "douyin_video.mp4")])
                # 延迟清理临时文件
                try:
                    os.unlink(video_file)
                except OSError:
                    pass
            else:
                # 下载失败，回退到 URL 方式
                yield event.chain_result([Comp.Video.fromURL(item.video_url)])
            return

        if item.is_images:
            image_urls = item.image_urls or []
            max_images = max(1, _as_int(self.config.get("max_images", 12), 12))
            forward_threshold = max(1, _as_int(self.config.get("forward_threshold", 3), 3))

            images_to_send = image_urls[:max_images]

            if len(images_to_send) > forward_threshold:
                # 超过阈值，用合并转发（聊天记录）形式发送
                try:
                    nodes = [
                        Comp.Node(
                            content=[Comp.Image.fromURL(url)],
                            uin="0",
                            name="抖音图集",
                        )
                        for url in images_to_send
                    ]
                    yield event.chain_result([Comp.Nodes(nodes=nodes)])
                except Exception as exc:  # noqa: BLE001
                    # 合并转发失败（可能不支持），回退到逐张发送
                    logger.warning(f"合并转发发送失败，回退到逐张发送: {exc}")
                    for url in images_to_send:
                        yield event.chain_result([Comp.Image.fromURL(url)])
            else:
                # 未超过阈值，逐张发送
                for url in images_to_send:
                    yield event.chain_result([Comp.Image.fromURL(url)])

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


async def _download_video(video_url: str, referer: str, timeout: float) -> str | None:
    """下载抖音视频到临时文件，返回文件路径。

    抖音 CDN 要求带 Referer 头，否则返回 403。
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
            resp = await client.get(video_url)
            resp.raise_for_status()

            # 写入临时文件
            tmp_dir = Path(tempfile.gettempdir()) / "astrbot_douyin"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = tmp_dir / f"{_safe_filename(video_url)}.mp4"
            tmp_file.write_bytes(resp.content)
            return str(tmp_file)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"视频下载失败，回退到 URL 方式: {exc}")
        return None


def _safe_filename(url: str) -> str:
    """从 URL 生成一个安全的文件名。"""
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()[:16]
