# 飞书群上下文与话题回复

群消息只有明确 @ 当前机器人时才触发；@ 其他机器人、@ 全体和普通聊天直接在入口丢弃，不添加处理中反应、不进入模型、不缓存被动聊天。每次 @ 只查询一次历史，不启动持续监控。现有及新建通道默认启用 mention 激活与 history=none。

群消息 @ 机器人后，Octop 读取触发时间前 30 分钟、最多 50 条消息；已有话题使用话题容器，普通群使用群容器。话题接口不支持时间参数，因此在本地过滤时间。超过条数或 24000 字符的背景会标为不完整，单条文本最多 2000 字符并保留截断标记。历史文件、图片以及富文本里的图片会通过原消息的资源接口下载，优先处理直接引用，然后按时间从新到旧处理近期消息。每轮最多 5 个附件，单个最多 20 MiB，总下载等待最多 60 秒；仍受工作区自身上传大小限制。使用原消息资源 URL 的哈希缓存，避免同名文件覆盖或重复下载。附件通过 agent workspace 保存到 inbound/，并作为本轮文件路径或图片输入供模型使用。来源消息保留发送者、时间和下载状态；下载失败或超过上限不得声称已读取。音视频、云文档链接和卡片内部的外部附件不在自动下载范围内。

触发消息、直接引用的 parent 消息、话题 root 消息、近期聊天分别标注。引用可早于回溯窗口，但必须属于同一群。向模型提供消息 ID、发送者 ID、时间和原文；要求区分引用与回答、不能将历史命令当作当前授权。普通群回复也使用 reply_in_thread=true；失败不会改投为群顶层消息。

飞书应用需要 `im:message.group_msg`，以及 `im:message` / `im:message:readonly` / `im:message.history:readonly` 中任一读取权限，机器人必须在目标群中。新建应用默认申请历史读取与资源下载权限，按飞书页面完成授权；当前租户免审权限可直接生效。缺少权限时仍响应当前消息，但上下文标为不可用；不能保证理解裸 @ 的意图。

官方接口：
- https://open.feishu.cn/document/server-docs/im-v1/message/list
- https://open.feishu.cn/document/server-docs/im-v1/message/get
- https://open.feishu.cn/document/server-docs/im-v1/message/reply

## 记忆辅助调用隔离

HarnessAgentLLMClient 的辅助调用使用独立 runnable config、空 callbacks 和 nostream 标签。记忆比较仍收到内部模型返回的 JSON，但内部结果不进入用户对话的流式消息回调。不会按 JSON 文本内容删除输出，因此正常回答中的 JSON 保留。旧消息不做清理。

## 验证

`tests/unit/gateway/test_feishu_context.py` 验证引用、时间/群边界、话题容器、权限失败降级、被动消息不读取历史、话题发送失败不转发群消息文本预算，以及原消息附件 URL、下载存储/缓存、大小限制、失败降级和模型可见文件路径。`tests/unit/agents/test_memory_stream_isolation.py` 用真实 LangGraph 流与假模型验证内部比较结果保留且用户只收到正常回答。
