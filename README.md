# 多站点自动签到 / 登录保活

支持 **New API** 风格中转站，可一次跑多个站点，并把结果推送到**企业微信应用**（非群机器人）。

| 站点 | 控制台 | 模式 |
|------|--------|------|
| [api.nloln.cn](https://api.nloln.cn/console/personal) | 个人中心 | `checkin` 自动签到（`POST /api/user/checkin`） |
| [anyrouter.top](https://anyrouter.top/console) | 控制台 | `checkin` 自动签到（`POST /api/user/sign_in`） |

## 功能

- 多站点配置（`sites` 数组）
- `checkin`：登录后按站点 `checkin_path` 签到
  - 经典 New-API（如 nloln）：`POST /api/user/checkin`（JSON `{}`）
  - anyrouter：`POST /api/user/sign_in`（空 body），再 `GET /api/user/self` 刷新额度
- `login_only`：登录后请求 `/api/user/self` 验证会话（仅保活，不签到）
- Cookie / 用户名密码 / access_token
- **企业微信应用消息推送**（每次运行汇总，含各站当前额度 `$`）
- 无 `requests` 时回退 `urllib`

## 快速开始

```bash
cp config.example.json config.json
# 编辑 sites 账号 + notify.wecom 凭证
# anyrouter 请确认：mode=checkin、checkin_path=/api/user/sign_in
python checkin.py
```

> 若你已有旧版 `config.json`，且 anyrouter 仍是 `mode: "login_only"` 或未设置 `checkin_path`，请改成与 `config.example.json` 中 anyrouter 段一致，否则只会保活、拿不到签到额度。

## 企业微信应用推送（非机器人）

使用「自建应用」发消息到成员，**不是**群机器人 Webhook。

### 1. 创建应用并拿凭证

1. 登录 [企业微信管理后台](https://work.weixin.qq.com/)
2. **应用管理** → **自建** → **创建应用**
3. 记录 **AgentId**、**Secret**
4. **我的企业** → 企业信息 → **企业 ID（corpid）**
5. 应用 **可见范围** 需包含接收人
6. （可选）开启微信插件后可在微信中收通知

### 2. 写入 config.json

```json
"notify": {
  "wecom": {
    "enabled": true,
    "corpid": "wwxxxxxxxx",
    "corpsecret": "应用Secret",
    "agentid": 1000002,
    "touser": "@all",
    "when": "always",
    "msgtype": "markdown"
  }
}
```

| 字段 | 说明 |
|------|------|
| `corpid` | 企业 ID |
| `corpsecret` | 应用 Secret |
| `agentid` | 应用 AgentId（数字） |
| `touser` | `@all` 或成员 userid，多人用 `\|` |
| `toparty` / `totag` | 可选 |
| `when` | `always` / `on_failure` / `never` |
| `msgtype` | `markdown` 或 `text` |

环境变量：`WECOM_CORPID` `WECOM_CORPSECRET` `WECOM_AGENTID` `WECOM_TOUSER`

```bash
python checkin.py --no-notify
```

### 推送示例

```
### 签到全部成功 (2/2)
> 时间：2026-07-11 09:00:01

✅ **nloln**
结果：签到成功
额度：$1.2345

✅ **anyrouter**
结果：签到成功
额度：$0.5000
```

## 站点配置

每个站点至少：`base_url` + `mode`，鉴权三选一：`username`+`password` / Cookie / `access_token`。

| 字段 | 说明 |
|------|------|
| `mode` | `checkin` 签到；`login_only` 仅保活 |
| `checkin_path` | 签到接口。nloln：`/api/user/checkin`；anyrouter：`/api/user/sign_in` |
| `self_path` | 默认 `/api/user/self`，签到后用于刷新额度 |
| `user_header` | 默认 `New-Api-User`（anyrouter 需要 `user_id`） |
| `quota_unit` | 默认 `500000`（显示为 `$x.xxxx`） |

anyrouter 示例片段：

```json
{
  "name": "anyrouter",
  "base_url": "https://anyrouter.top",
  "mode": "checkin",
  "checkin_path": "/api/user/sign_in",
  "self_path": "/api/user/self",
  "user_header": "New-Api-User",
  "user_id": "你的用户ID",
  "session_cookie": "session值（或完整 Cookie）",
  "quota_unit": 500000
}
```

## 命令

```bash
python checkin.py
python checkin.py -v
python checkin.py --only anyrouter
python checkin.py --no-notify
```

## 1Panel 计划任务脚本

```bash
#!/bin/bash
cd /opt/auto-checkin || exit 1
/usr/bin/python3 checkin.py >> /opt/auto-checkin/checkin.log 2>&1
```

## 安全

- 不要提交 `config.json`
- `chmod 600 config.json`
