# astrbot_plugin_douyin_local

AstrBot 抖音公开链接解析插件。它会自动识别聊天消息里的抖音链接，尝试解析视频或图集并发送到当前会话。

这个版本不调用第三方解析站，不依赖 `a_bogus` / `X-Bogus` 签名。它通过 iesdouyin 移动端分享页 + `window._ROUTER_DATA` 提取作品数据，仅需一个可免费注册的匿名 `ttwid` cookie。

## 功能

- 自动识别 `v.douyin.com`、`jx.douyin.com`、`www.douyin.com`、`m.douyin.com`、`iesdouyin.com` 链接
- 自动展开短链并提取作品 ID
- 支持视频作品和图集作品
- 自动注册匿名 `ttwid`，无需手动配置 Cookie
- 视频下载带正确 `Referer` 头，避免 403
- 无水印视频解析（play 而非 playwm）
- 检测到抖音链接后会优先拦截，避免普通 LLM 回复抢先处理
- 支持群白名单、会话 ID 白名单、私聊开关
- 可配置失败时是否回复、图集最多发送数量、请求超时

## 安装

### 通过 AstrBot WebUI 安装

在 AstrBot WebUI 的插件管理中选择从 GitHub/仓库地址安装，填入：

```text
https://github.com/libinyam/astrbot_plugin_douyin_local
```

安装后在 WebUI 中启用或重载插件。

### 手动安装

进入 AstrBot 插件目录并克隆仓库：

```bash
cd data/plugins
git clone https://github.com/libinyam/astrbot_plugin_douyin_local.git
```

确保依赖已安装：

```bash
pip install -r data/plugins/astrbot_plugin_douyin_local/requirements.txt
```

然后在 AstrBot WebUI 中重载/启用插件。

## 使用

无需命令，直接在群聊或私聊中发送包含抖音链接的消息：

```text
https://v.douyin.com/xxxxxxx/
```

插件会自动回复作者、标题，并发送视频或图集图片。

## 配置建议

- `group_whitelist`: 建议先填测试群 ID，确认稳定后再扩大范围。
- `reply_on_failure`: 群里不想刷屏时可以关闭。
- `max_images`: 图集较大时建议限制在 9 到 12 张。
- `douyin_cookie`: 默认留空，插件会自动注册匿名 `ttwid`。只有在解析经常失败时，再填你自己的浏览器 Cookie（需包含 `ttwid`）。

## 解析原理

1. 从消息中提取抖音短链
2. 请求短链（不跟随重定向），从 `Location` 头提取作品 ID
3. 请求 `iesdouyin.com/share/video/{id}/` 分享页（移动端 UA + ttwid）
4. 从页面 HTML 中提取 `window._ROUTER_DATA` JSON
5. 从 `loaderData` -> `videoInfoRes.item_list[0]` 中提取视频/图集数据
6. 视频通过 `aweme.snssdk.com/aweme/v1/play/` 端点获取无水印直链
7. 图集通过 iesdouyin v2 slidesinfo 接口获取图片

此方案不需要 `a_bogus` 签名，稳定性取决于抖音移动端分享页结构。

## 注意

本插件只面向公开分享链接，不处理私密内容、登录后专属内容或需要绕过访问控制的内容。抖音网页接口可能变化，如果突然失效，通常需要更新 `douyin_parser.py` 里的解析策略。

请在遵守平台规则、版权要求和所在地法律法规的前提下使用本插件。插件仅提供公开分享链接的解析辅助，不鼓励未授权下载、传播或商用他人内容。

## 更新

手动安装的用户可以进入插件目录更新：

```bash
cd data/plugins/astrbot_plugin_douyin_local
git pull
```

更新后在 AstrBot WebUI 中重载插件或重启 AstrBot。

## 开源协议

MIT License
