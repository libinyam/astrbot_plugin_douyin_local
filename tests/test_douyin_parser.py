import json
import unittest
from urllib.parse import quote

import httpx

from douyin_parser import (
    DouyinParseError,
    DouyinParser,
    _extract_router_data,
    _find_item_in_router_data,
    _json_from_response,
    _normalize_media_url,
    extract_aweme_id,
    extract_douyin_url,
)


ITEM_ID = "7123456789012345678"


class DouyinParserTests(unittest.TestCase):
    def test_extract_url_from_share_text(self):
        text = "8.88 复制打开抖音 https://v.douyin.com/Abc-123/ ，看看这个视频"
        self.assertEqual(extract_douyin_url(text), "https://v.douyin.com/Abc-123/")

    def test_extract_url_from_jx_short_link(self):
        text = "看看这个 https://jx.douyin.com/Abc456/"
        self.assertEqual(extract_douyin_url(text), "https://jx.douyin.com/Abc456/")

    def test_extract_aweme_id_from_common_urls(self):
        self.assertEqual(
            extract_aweme_id(f"https://www.douyin.com/video/{ITEM_ID}"),
            ITEM_ID,
        )
        self.assertEqual(
            extract_aweme_id(f"https://www.douyin.com/discover?modal_id={ITEM_ID}"),
            ITEM_ID,
        )
        self.assertEqual(
            extract_aweme_id(f"https://www.iesdouyin.com/share/video/{ITEM_ID}/"),
            ITEM_ID,
        )

    def test_extract_video_from_router_data(self):
        """测试从移动分享页的 window._ROUTER_DATA 中提取作品数据"""
        parser = DouyinParser()
        payload = {
            "loaderData": {
                f"video_({ITEM_ID})/page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": ITEM_ID,
                                "desc": "测试标题",
                                "author": {"nickname": "测试作者"},
                                "video": {
                                    "play_addr": {
                                        "uri": "vid123abc",
                                        "url_list": [
                                            "https://example.com/video.mp4?x=1\\u0026y=2",
                                        ],
                                    },
                                    "cover": {"url_list": ["https://example.com/cover.jpg"]},
                                },
                            }
                        ]
                    }
                }
            }
        }
        page = (
            '<script>window._ROUTER_DATA = '
            f"{json.dumps(payload, ensure_ascii=False)}"
            ";</script>"
        )
        router_data = _extract_router_data(page)
        self.assertIsNotNone(router_data)
        item = _find_item_in_router_data(router_data.get("loaderData", {}), ITEM_ID)
        self.assertIsNotNone(item)

        result = parser._normalize_item(
            item, "https://v.douyin.com/a/", "", ITEM_ID, "test"
        )
        self.assertTrue(result.is_video)
        self.assertEqual(result.author, "测试作者")
        self.assertEqual(result.title, "测试标题")
        self.assertIn("/aweme/v1/play/", result.video_url)
        self.assertIn("video_id=vid123abc", result.video_url)

    def test_extract_images_from_item(self):
        parser = DouyinParser()
        item = {
            "aweme_id": ITEM_ID,
            "desc": "图集",
            "author": {"nickname": "作者"},
            "image_post_info": {
                "images": [
                    {"display_image": {"url_list": ["https://example.com/1.jpg"]}},
                    {"origin_image": {"url_list": ["https://example.com/2.jpg"]}},
                ]
            },
        }
        result = parser._normalize_item(item, "https://v.douyin.com/a/", "", ITEM_ID, "test")

        self.assertTrue(result.is_images)
        self.assertEqual(result.image_urls, ["https://example.com/1.jpg", "https://example.com/2.jpg"])

    def test_normalize_media_url(self):
        self.assertEqual(
            _normalize_media_url("//example.com/video.mp4"),
            "https://example.com/video.mp4",
        )
        # \u0026 是 JSON 中 & 的转义形式
        self.assertEqual(
            _normalize_media_url("https://example.com/v.mp4?x=1\\u0026y=2"),
            "https://example.com/v.mp4?x=1&y=2",
        )

    def test_non_json_api_response_has_readable_error(self):
        response = httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<!doctype html><html><title>blocked</title></html>",
            request=httpx.Request("GET", "https://www.douyin.com/api"),
        )

        with self.assertRaises(DouyinParseError) as ctx:
            _json_from_response(response)

        self.assertIn("接口未返回 JSON", str(ctx.exception))
        self.assertIn("HTML 页面", str(ctx.exception))

    def test_empty_api_response_has_readable_error(self):
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            text="",
            request=httpx.Request("GET", "https://www.douyin.com/api"),
        )

        with self.assertRaises(DouyinParseError) as ctx:
            _json_from_response(response)

        self.assertIn("接口返回空内容", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
