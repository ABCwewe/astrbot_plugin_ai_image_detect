# astrbot_plugin_ai_image_detect

AstrBot 插件,用 Sightengine `genai` 模型判断群里发的图是不是 AI 生成的。

## 安装

WebUI → 插件管理 → 从 GitHub 安装 → 贴:

```
https://github.com/ABCwewe/astrbot_plugin_ai_image_detect
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
| `skip_animated` | `false` | 开启后屏蔽并跳过动图（GIF、动态 WebP/APNG）检测，避免消耗 API 配额 |
| `detect_animated_frames` | `false` | 开启后遇到动图自动截取首帧与中间帧两个静帧分别请求 API 并发送聚合置信度（消耗 2 次 API 配额） |

## 用法

**命令**(群里发图后,或引用一条含图的消息):

```
/鉴图
```

bot 回:`AI 生成(AI 概率 99.0%,阈值 0.5,消耗 5 ops)`

**动图设置与控制**:
- 可在 WebUI 插件配置页中开启/关闭 `skip_animated`（屏蔽动图）或 `detect_animated_frames`（抽帧检测）。
- 亦可通过群内命令实时切换或查看状态：
  ```
  /鉴图 动图 开    # 开启屏蔽动图（跳过检测）
  /鉴图 动图 关    # 关闭屏蔽动图
  /鉴图 抽帧 开    # 开启动图抽帧检测（首帧+中间帧聚合）
  /鉴图 抽帧 关    # 关闭动图抽帧检测
  /鉴图 动图       # 查看当前动图设置状态
  ```
- **抽帧检测模式**：遇到动图时，提取第一帧与正中静帧分别请求 Sightengine 检测，最后聚合置信度并输出每帧详细结果，如：
  `AI 生成(动图抽帧聚合 AI 概率 90.0%, 第1帧 92.0%, 中间帧(第5帧) 88.0%, 阈值 0.5, 消耗 10 ops)`
- **屏蔽跳过模式**：若未开启抽帧检测但开启了动图屏蔽，bot 回复 `检测到动图，已跳过检测`，不调用 Sightengine API 且不占用冷却与 ops 额度。

**LLM 工具**:bot 在觉得该验图时也会自动调,无需手动触发。若开启了动图抽帧检测或屏蔽同样会自动应用对应策略。

**冷却**:同会话 30 秒内重复触发会被拒:
```
冷却中,还剩 27 秒
```

## 实现

- 端点:`https://api.sightengine.com/1.0/check.json?models=genai&url=<图片 URL>`
- 单次消耗:**5 ops**（抽帧检测消耗 2 次即 10 ops）
- 返回字段: `ai_generated` (0~1)
- 冷却: 内存 dict,以 `unified_msg_origin` 为 key
- 不缓存,同图重复请求不省 ops(配额够用,不优化)

## 依赖

- `httpx` (AstrBot v4 自带)
- `Pillow` (AstrBot 自带，用于动图特征与抽帧解析)
- `aiofiles`
- AstrBot 已在 `astrbot/api` 提供 `@command` / `@llm_tool` 装饰器,无需额外装包
