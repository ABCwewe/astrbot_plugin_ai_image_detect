# astrbot_plugin_ai_image_detect

AstrBot 插件,用 Sightengine `genai` 模型判断群里发的图是不是 AI 生成的。

## 安装

WebUI → 插件管理 → 从 GitHub 安装 → 贴:

```
https://github.com/publieople/astrbot_plugin_ai_image_detect
```

## 配置

装完打开插件配置页填两项:

| 字段 | 说明 |
|---|---|
| `api_user` | Sightengine 的 API user |
| `api_secret` | Sightengine 的 API secret |

注册 → https://dashboard.sightengine.com/ 。

可选配置(都有默认值):

| 字段 | 默认 | 说明 |
|---|---|---|
| `ai_threshold` | `0.5` | `ai_generated` ≥ 此值判为 AI |
| `cooldown_seconds` | `30` | 同一会话两次调用的最小间隔(秒) |

## 用法

**命令**(群里发图后,或引用一条含图的消息):

```
/鉴图
```

bot 回:`AI 生成(AI 概率 99.0%,阈值 0.5,消耗 5 ops)`

**LLM 工具**:bot 在觉得该验图时也会自动调,无需手动触发。

**冷却**:同会话 30 秒内重复触发会被拒:
```
冷却中,还剩 27 秒
```

## 实现

- 端点:`https://api.sightengine.com/1.0/check.json?models=genai&url=<图片 URL>`
- 单次消耗:**5 ops**
- 返回字段: `ai_generated` (0~1)
- 冷却: 内存 dict,以 `unified_msg_origin` 为 key
- 不缓存,同图重复请求不省 ops(配额够用,不优化)

## 没做的(故意)

- per-generator 模型溯源(返回具体是 SD / MidJourney / Flux)— Sightengine 基础 API 不给,高级要联系销售
- 本地文件上传(只接 URL,QQ 群图本身就是 CDN URL,免做)
- 持久化缓存(同图几乎不会重复,内存占的不值得)

## 依赖

- `httpx` (AstrBot v4 自带)
- AstrBot 已在 `astrbot/api` 提供 `@command` / `@llm_tool` 装饰器,无需额外装包
