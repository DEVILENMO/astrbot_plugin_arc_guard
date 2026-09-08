# 弧光护卫

> 插件 ID：`astrbot_plugin_arc_guard`（由 `astrbot_plugin_enmo_guard` 重命名）。

检测群聊中的可配置违禁关键词（默认组合 `sb enmo`：两者同时出现即命中），按出现次数禁言；若联动机器人被教唆说出违禁内容，则连坐其 `@` 的人。群图片会经本地 RapidOCR 提取文字后再做同样检测。

## 功能

### 基础处罚

- 扫描**所有群成员发言**、**群昵称/群名片**、**@ 对象群名片**、**图片 OCR 文字**与 **AI 回复**
- **AI 回复**命中违禁词：拦截原文、改为警告，并禁言对话对象（白名单用户不禁言，但原文仍拦截）；Minecraft 消息中枢里的弧光天星回复同样检测
- 关键词列表可配置（`forbidden_keywords`，WebUI 逐项添加），默认 `sb enmo`
- **组合模式**：单项内用空格（或 `+` / `&`）分隔多个词，**全部同时出现**即命中（顺序不限），例如 `sb enmo` 可拦 `sb ... enmo`、`enmo ... sb`
- **连续模式**：单项无空格时仍为整段连续匹配，例如 `enmosb`
- 中间可夹杂分隔符 / 汉字等；连续匹配时例如：`e,n,m,o,s,b`
- 识别常见**形似字**（希腊文、西里尔文等）
- **出现几次就禁言几分钟**
- **白名单**（默认 `593405016`）发言完全跳过
- **黑名单**：配置的 QQ 群消息一律拦截（`stop_event`）
- **累加禁言**：新禁言 = 当前剩余禁言 + 本次追加
- **禁言失败降级屏蔽**：群禁言不可用时按相同时长屏蔽该用户；日常聊天不回复，但仍检测关键词并累加处罚；求饶同样减屏蔽时间
- **群内求饶**：`pardon_in_group` 开启时（默认开），在群里发送求饶台词也可减刑，只减免**当前群**的禁言/屏蔽（适合不支持私聊的平台）
- **私聊求饶**：仍可用；成功后会向涉及群广播
- **跨插件禁言 API**：`mute_user` / `mute_user_in_group`（`respect_whitelist`）/ `unmute_user` / `unmute_user_in_group`
- **LLM 工具** `arc_mute_user`：大模型可对指定 QQ 追加禁言（同样累加）
- **LLM 工具** `arc_unmute_user`：大模型可解除指定 QQ 在本群的禁言与限时屏蔽
- **机器人群名片守护**：`card_tamper_protect` 开启时每秒检测，被改则改回 `bot_group_card`（默认天星大人）并制裁
- **图片 OCR**：本地 `rapidocr` + `onnxruntime`（small 模型），`ocr_enabled` 可开关；带透明通道的图会白底+黑底各识别一次（防幻影坦克）

### 机器人连坐

联动机器人（默认 QQ `2267236706`）发出含违禁序列的消息时：

1. **不禁言**该机器人本身
2. 解析其消息里 `@` 了谁
3. 对被 `@` 的教唆犯连坐禁言

## 要求

- 平台：`aiocqhttp`（OneBot）
- AstrBot `>= 4.0.0`
- 机器人需要有**群禁言权限**
- OCR：插件 `requirements.txt` 会安装 `rapidocr`、`onnxruntime`

## 配置要点

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `forbidden_keywords` | `sb enmo` | 禁言关键词；空格分隔表示同时出现 |
| `mute_seconds_per_hit` | `60` | 每次命中禁言秒数 |
| `ocr_enabled` | `true` | 是否对图片做 OCR 违禁检测 |
| `pardon_enabled` | `true` | 是否开启求饶减刑（私聊+群内） |
| `pardon_in_group` | `true` | 是否允许在群内发送求饶台词减刑 |
| `pardon_phrase` | `天星大人我错了，我跪下求你放过我` | 求饶台词 |
| `whitelist_ids` | `593405016` | 白名单 QQ |
| `blacklist_ids` | （空） | 黑名单 QQ |
| `shield_fallback` | `true` | 禁言失败时降级为限时屏蔽 |

## 安装

### 链接安装

```text
https://github.com/DEVILENMO/astrbot_plugin_arc_guard
```

### 手动安装

1. 将本插件放在 `data/plugins/astrbot_plugin_arc_guard`
2. 重载插件（首次会按 `requirements.txt` 安装 OCR 依赖）并在 WebUI 中配置

## 仓库

https://github.com/DEVILENMO/astrbot_plugin_arc_guard

## 作者

DEVILENMO
