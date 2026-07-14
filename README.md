# 多站点自动签到 / 登录保活

支持 New API 风格站点，签到/登录后通过**企业微信群机器人 Webhook**推送结果。

自用站点：https://anyrouter.top、https://api.nloln.cn

## 企业微信群机器人（推荐）

1. 企业微信建群 → 群设置 → **群机器人** → 添加 → 复制 **Webhook 地址**
2. 写入 `config.json`：

```json
"notify": {
  "wecom_bot": {
    "enabled": true,
    "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx",
    "when": "always",
    "msgtype": "markdown"
  }
}
```

也可用环境变量：`WECOM_BOT_WEBHOOK`

| 字段 | 说明 |
|------|------|
| `webhook` | 群机器人 Webhook 完整 URL |
| `when` | `always` / `on_failure` / `never` |
| `msgtype` | `markdown` 或 `text` |

优点：无需配置企业可信 IP / 接收消息服务器 URL，也无需域名。

## 站点配置

每个站点至少：`base_url` + `mode`，鉴权三选一：

- `username` + `password`
- `session_cookie` + `user_id`
- `access_token`

## 命令

```bash
python3 checkin.py
python3 checkin.py -v
python3 checkin.py --no-notify
```

## 1Panel 计划任务

```bash
#!/bin/bash
cd /home/autocheckinAPI || exit 1
/usr/bin/python3 checkin.py >> /home/autocheckinAPI/checkin.log 2>&1
```
