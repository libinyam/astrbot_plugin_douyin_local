import json
import unittest
from urllib.parse import quote

from douyin_parser import DouyinParser, extract_aweme_id, extract_douyin_url


ITEM_ID = "7123456789012345678"


class DouyinParserTests(unittest.TestCase):
    def test_extract_url_from_share_text(self):
        text = "8.88 复制打开抖音 https://v.douyin.com/Abc-123/ ，看看这个视频"
        self.assertEqual(extract_douyin_url(text), "https://v.douyin.com/Abc-123/")

    def test_extract_aweme_id_from_common_urls(self):
        self.assertEqual(
            extract_aweme_id(f"https://www.douyin.com/video/{ITEM_ID}"),
            ITEM_ID,
        )
        self.assertEqual(
            extract_aweme_id(f"https://www.douyin.com/discover?modal_id={ITEM_ID}"),
            ITEM_ID,
        )

    def test_extract_video_from_render_data(self):
        parser = DouyinParser()
        payload = {
            "route": {
                "item": {
                    "aweme_id": ITEM_ID,
                    "desc": "测试标题",
                    "author": {"nickname": "测试作者"},
                    "video": {
                        "play_addr": {
                            "url_list": [
                                "https://example.com/video.mp4?x=1\\u0026y=2",
                            ]
                        },
                        "cover": {"url_list": ["https://example.com/cover.jpg"]},
                    },
                }
            }
        }
        page = (
            '<script id="RENDER_DATA" type="application/json">'
            f"{quote(json.dumps(payload, ensure_ascii=False))}"
            "</script>"
        )
        item = parser._extract_item_from_page(page, ITEM_ID)
        result = parser._normalize_item(item, "https://v.douyin.com/a/", "", ITEM_ID, "test")

        self.assertTrue(result.is_video)
        self.assertEqual(result.author, "测试作者")
        self.assertEqual(result.title, "测试标题")
        self.assertEqual(result.video_url, "https://example.com/video.mp4?x=1&y=2")

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


if __name__ == "__main__":
    unittest.main()
