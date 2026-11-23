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
- **屏蔽（独立功能）**：机器人不再回复其聊天，但仍检测违禁词并照常禁言，与禁言互不影响。永久屏蔽（`blacklist_ids`）在所有群和私聊生效；限时屏蔽只作用于当前群，可求饶减时。带唤醒前缀的命令（如 `/要牌`）不被拦截，被屏蔽的人仍可正常交互
- **累加禁言**：新禁言 = 当前剩余禁言 + 本次追加
- **真禁言平台**：aiocqhttp（OneBot `set_group_ban`）与 qq_official（机器人为**群管理员**时走官方 `restrict_chat_setting` 接口，上限 30 天、仅普通成员可被禁）
- **命中自动撤回**：`recall_on_hit` 开启时（默认开），命中违禁词先撤回该消息再警告禁言；qq_official 走官方撤回接口（发出超 2 分钟不可撤），aiocqhttp 走 `delete_msg`；联动机器人的违禁消息同样撤回；撤回失败不影响警告与禁言
- **群内求饶**：`pardon_in_group` 开启时（默认开），在群里发送求饶台词也可减刑，只减免**当前群**的禁言/限时屏蔽（适合不支持私聊的平台）
- **私聊求饶**：仍可用；成功后会向涉及的群广播（`qq_official` 上只私聊回复，不进群广播）
- **超级管理员硬保护**：AstrBot 管理员（`admins_id`）无法通过弧光护卫禁言系统的**任何途径**被禁言——违禁词自动处罚、游戏插件调用（即使 `respect_whitelist=false`）、LLM 工具一律拒绝；检测与警告照常，仅不执行禁言。此项不可配置
- **跨插件禁言 API**：`mute_user` / `mute_user_in_group`（`respect_whitelist`）/ `unmute_user` / `unmute_user_in_group`。两个平台通用；其他插件的禁言建议优先调用这些接口，以保留叠加时长（[21点](https://github.com/DEVILENMO/astrbot_plugin_blackjack21)、[自爆卡车](https://github.com/DEVILENMO/astrbot_plugin_self_destruct_truck) 即如此）
- **LLM 工具**（全部**仅超级管理员可用**，即 AstrBot 管理员 `admins_id`；发起人不是超级管理员时强制拒绝）：
  - `arc_mute_user`：对指定用户追加禁言（同样累加）
  - `arc_unmute_user`：解除指定用户在本群的禁言（不影响屏蔽）
  - `arc_shield_user`：屏蔽指定用户，`minutes` 为本群限时屏蔽，`permanent=true` 为永久屏蔽
  - `arc_unshield_user`：取消永久屏蔽和所有群的限时屏蔽（不影响禁言）
  - `arc_recall_user_messages`：撤回指定用户在本群最近的几条消息（上限 5，官方平台限 2 分钟内）
  - `arc_register_bot` / `arc_unregister_bot`：维护联动机器人名单
- **机器人群名片守护**（仅 `aiocqhttp`，官方平台没有名片接口）：`card_tamper_protect` 开启时每秒检测，被改则改回 `bot_group_card`（默认天星大人）并制裁
- **图片 OCR**：本地 `rapidocr` + `onnxruntime`（small 模型），`ocr_enabled` 可开关；带透明通道的图会白底+黑底各识别一次（防幻影坦克）

### 机器人连坐

联动机器人（默认 QQ `2267236706`）发出含违禁序列的消息时：

1. **不禁言**该机器人本身
2. 解析其消息里 `@` 了谁
3. 对被 `@` 的教唆犯连坐禁言

连坐名单支持 **LLM 动态维护**（仅超级管理员）：超级管理员 @ 机器人说「@xx 把这个记录为机器人」即写入名单（`arc_register_bot`），「移除」用 `arc_unregister_bot`；持久化到 `linked_bot_ids` 配置，官方 openid 与 QQ 号均可（解决官方平台消息里机器人 openid 对不上 QQ 号的问题）。白名单与 AstrBot 管理员不可被记录。

## 要求

- 平台：`aiocqhttp`（OneBot）或 `qq_official`
- AstrBot `>= 4.0.0`
- 真禁言/撤回需要机器人为**群管理员**（OneBot 群禁言权限；qq_official 为群管理员身份，且开放平台已开通群管理与撤回权限）
- `qq_official` 只能禁言**普通成员**、撤回**普通成员的消息**；群主、管理员和其他机器人不受影响（官方接口限制）
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
| `whitelist_ids` | `593405016` | 白名单 ID（QQ 号或官方 openid），发言完全不处理 |
| `blacklist_ids` | （空） | 永久屏蔽名单（所有群和私聊都不回复其聊天，仍检测违禁词；命令交互不受影响） |
| `linked_bot_ids` | `2267236706` | 联动机器人（连坐）名单，可填 QQ 号或官方 openid |
| `accomplice_mute_seconds` | `120` | 连坐时每次命中的禁言秒数 |
| `recall_on_hit` | `true` | 命中违禁词自动撤回消息（需群管理员） |

## 更新记录

### 1.12.3

- 超级管理员（AstrBot `admins_id`）受硬保护：任何走弧光护卫禁言系统的调用都无法禁言他们，`respect_whitelist=false` 也不例外；原来的 `exempt_admins` 开关移除（保护不再可关闭）
- 联动机器人名单同样不再接受 AstrBot 管理员（不再受 `exempt_admins` 开关影响）

### 1.12.2

- 修复 1.12.1 的命令放行不生效：核心管线在唤醒阶段会把唤醒前缀从消息文本中剥掉，导致按前缀判断永远失败；改为按「本条消息激活的命令处理器」判断（与核心区分命令/监听的方式一致）

### 1.12.1

- 屏蔽只拦截聊天：命令消息不再被拦截，被屏蔽的人仍可与机器人交互；违禁词审查照常进行

### 1.12.0

- **禁言与屏蔽拆成两个独立功能**：禁言失败不再自动变成屏蔽（配置项 `shield_fallback` 已移除），禁言成功也不再清除屏蔽，解禁工具只解除禁言
- **屏蔽**成为独立功能：`arc_shield_user` 支持本群限时屏蔽和永久屏蔽（所有群和私聊），`arc_unshield_user` 取消；永久屏蔽的人发私聊也不再回复。原来的“黑名单”就是永久屏蔽名单（配置项 `blacklist_ids` 不变）
- **所有 LLM 工具现在只允许超级管理员使用**（AstrBot 管理员 `admins_id`），其他人发起的请求会被直接拒绝
- 新增 `arc_recall_user_messages`：撤回指定用户在本群最近的几条消息
- 禁言成功后，播报失败不再被误报为禁言失败

### 1.11.0

- 新增 `arc_register_bot` / `arc_unregister_bot`：由 LLM 动态维护联动机器人（连坐）名单

### 1.10.1

- 修复 `qq_official` 上叠加禁言播报里的 `@` 显示为原始标签

### 1.10.0

- `qq_official` 真禁言：机器人为群管理员时走官方 `restrict_chat_setting` 接口，支持累加与求饶减刑
- 命中违禁词自动撤回消息（`recall_on_hit`，默认开启），联动机器人的违禁消息同样撤回

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
