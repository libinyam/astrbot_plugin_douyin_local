# astrbot_plugin_douyin_local

AstrBot 抖音公开链接解析插件。它会自动识别聊天消息里的抖音链接，尝试解析视频或图集并发送到当前会话。

这个版本不调用第三方解析站，不会把链接转发给 `toody.netlify.app` 一类的外部解析 API。它会直接访问抖音公开分享页和公开 Web 接口，因此稳定性取决于抖音网页结构和风控策略。

## 功能

- 自动识别 `v.douyin.com`、`www.douyin.com`、`m.douyin.com`、`iesdouyin.com` 链接
- 自动展开短链并提取作品 ID
- 支持视频作品和图集作品
- 支持群白名单、会话 ID 白名单、私聊开关
- 可配置失败时是否回复、图集最多发送数量、请求超时
- 可选填写自己的抖音网页 Cookie，提高公开页面解析成功率

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
- `douyin_cookie`: 默认留空。只有在公开页面解析经常失败时，再填你自己的浏览器 Cookie。

## 注意

本插件只面向公开分享链接，不处理私密内容、登录后专属内容或需要绕过访问控制的内容。抖音网页接口可能变化，如果突然失效，通常需要更新 `douyin_parser.py` 里的解析策略。

请在遵守平台规则、版权要求和所在地法律法规的前提下使用本插件。插件仅提供公开分享链接的解析辅助，不鼓励未授权下载、传播或商用他人内容。

## 开源协议

MIT License
