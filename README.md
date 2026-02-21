# quote_bot

一个最小 Telegram 机器人：
- 私聊收到文字后，返回一张白底黑字 PNG 图片。
- 支持 emoji（Google style）。
- 支持 inline mode：在任意聊天里 `@你的机器人 用户输入`，返回图片建议（带防抖延迟，不会每个键都触发生成）。

## 安装

```bash
pdm install
```

## 运行

先在环境变量里放入 bot token：

```bash
export TELEGRAM_BOT_TOKEN="<your-token>"
```

启动机器人（`httpx` 长轮询）：

```bash
pdm run quote-bot
```

也可以直接用 Python 启动并调参数：

```bash
python -m quote_bot.bot --timeout 30 --retry-delay 3
```

## 启用 Inline Mode

在 BotFather 对该机器人执行：

```text
/setinline
```

inline 查询返回图片建议时，机器人会先等待一小段防抖时间，再生成图片并上传拿到 `file_id`，最后以 cached photo 返回结果。
如果未配置 `INLINE_CACHE_CHAT_ID`，会临时上传到发起 inline 的用户会话。
emoji 渲染使用 Google style 源，首次遇到新 emoji 时需要网络拉取图标。

## 可选环境变量

- `TELEGRAM_BOT_TOKEN`: Telegram Bot Token（必填）
- `POLL_TIMEOUT`: 长轮询超时秒数（默认 `30`）
- `RETRY_DELAY_SECONDS`: 请求失败后的重试间隔秒数（默认 `3`）
- `QUOTE_BOT_FONT_PATH`: 自定义字体路径（可选，便于中文字体显示）
- `INLINE_CACHE_TIME`: inline 结果缓存时间秒数（默认 `60`）
- `INLINE_CACHE_CHAT_ID`: inline 临时上传使用的 chat id（可选，建议填一个私有频道/群）
- `INLINE_DEBOUNCE_SECONDS`: inline 处理防抖延迟秒数（默认 `0.8`）
- `WORKER_CONCURRENCY`: 并发处理任务数（默认 `4`）
- `LOG_LEVEL`: 日志级别（默认 `INFO`）
