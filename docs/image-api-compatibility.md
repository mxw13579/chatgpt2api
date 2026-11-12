# 图片模型的 Chat / Responses 兼容调用

明确指定图片模型时，`/v1/chat/completions` 和 `/v1/responses` 会自动调用同一套绘图后端，不再要求客户端选择 Images 接口或额外声明 `image_generation` 工具。接口路径决定返回格式，不会做 HTTP 重定向。

## 最小请求

下面两个请求都能进入画图流程；仍需有效认证、可用账号及正常上游服务。

### Chat

```http
POST /v1/chat/completions
Authorization: Bearer <你的密钥>
Content-Type: application/json
```

```json
{
  "model": "gpt-image-2",
  "messages": [{"role": "user", "content": "画一只坐在窗台上的橘猫"}],
  "stream": false
}
```

返回 `chat.completion`，图片在 `choices[0].message.content` 中，以 Markdown 内嵌 Base64 data URI 表达。前端需要支持这种图片显示方式。

### Responses

```http
POST /v1/responses
Authorization: Bearer <你的密钥>
Content-Type: application/json
```

```json
{
  "model": "gpt-image-2",
  "input": "画一只坐在窗台上的橘猫",
  "stream": false
}
```

不需要 `tools`。返回 `response`，图片在 `output` 中的 `image_generation_call.result` 字段，是 Base64 字符串，不是 `output_text`。

## 自动路由范围

- `gpt-image-*`、`chatgpt-image-*`、`codex-gpt-image-2` 和 `plus/team/pro-codex-gpt-image-2` 视为明确图片模型。模型还必须在当前支持目录中，否则返回参数错误，不会静默换模型。
- 普通文本模型不会因提示词中出现“画图”而自动切换。尤其 `gpt-5-5`、`gpt-5-3` 等即使列在可生图目录中，也不再仅凭模型名触发 Chat 画图。
- 显式使用 `image_generation` 工具、相应 `tool_choice` 对象，或 `modalities: ["image"]`，也会进入图片流程。
- 普通模型加显式图片意图时，优先使用图片工具的有效 `model`；否则保留可生图的顶层模型，`auto` 或其他文本模型则使用配置中的默认图片模型。
- 明确图片模型与 `tool.model` 不一致、显式选择 `tool_choice: "none"` 或另一个工具时，报参数冲突，不忽略明确指令。

## 输入适配规则

仅图片分支提供以下兼容，普通文本分支维持自身协议：

1. 非空字符串 `prompt` 优先。
2. 未提供直接提示词时，Chat 优先读 `messages`，Responses 优先读 `input`。
3. 原生字段缺失或为 `null` 时，允许读取另一个接口的字段。明确传入空数组/空字符串不会偷偷改读另一套内容。
4. 不合并 `messages` 和 `input`。选中来源中的用户文本按顺序组成提示词；`instructions` 作为前缀加入。`system/developer/assistant` 消息不会被当作用户画图提示词。
5. 参考图从选中来源中最近一条含图的用户消息提取，并保留该消息的全部图片；直接的 Responses 内容块数组视为同一条消息。直接 `prompt` 可以搭配这些参考图。
6. 内容过滤检查最终规范化的提示词，使用备用字段不会绕过过滤。

支持 HTTP(S) 图片 URL、data URI、带 MIME 的 Base64 图片等已有输入形式。远程图仍通过现有代理感知下载器获取；最多 10 张参考图，每张最大 10MB。`file_id` 图片引用不支持，会明确报错而不是丢图后继续文生图。

例如，即使把 Chat 请求体发送到了 `/v1/responses`，也可以正常进入图片流程：

```json
{
  "model": "gpt-image-2",
  "messages": [{"role": "user", "content": "画一张城市宣传海报"}],
  "n": 2,
  "size": "1536x1024",
  "quality": "high"
}
```

## 参数和结果约束

- `n` 为 1–4 的整数；不接受布尔值、0 或小数。
- 顶层非 `null` 的 `n/size/quality` 优先于第一个图片工具里的同名设置，两种入口均传递给后端。`size` 接受 `auto` 或正整数 `宽x高`；`quality` 接受 `auto/low/medium/high/xhigh/max/standard/hd`，不接受任意提示文本。
- Chat/Responses 为保留各自响应结构，内部统一使用 Base64 图片结果；`response_format: "url"` 不会把它们改成 Images 接口的 `data[].url` 返回。
- `size/quality` 的实际效果取决于上游。Web 画图链路可能将它们转为提示要求，并非硬性像素约束。
- 目前不提供 JPEG/WebP 输出转码，也不提供 `partial_images` 实时预览；显式请求这些未支持选项会报错。
- 不支持通过 `previous_response_id` 自动恢复之前的画图会话。需要编辑时，应重新提供参考图和本次要求。

## 流式与失败

`stream: true` 保持对应接口的流式格式：

- Chat：`chat.completion.chunk`，最终图片仍为消息内容中的 Markdown data URI。
- Responses：`response.created` → 每张图片的 `response.output_item.added/done` → `response.completed`。多个生成结果会完整累计，不再只返回第一批。

流式格式不代表上游能实时提供图片预览。只有拿到足够的有效图片结果、且生成任务未失败时才会成功结束；纯文字、空结果、图片数量不足或中途异常不会被包装成成功。并行任务中一项失败时，也不会用另一项偶然多出的图片掩盖该失败。

非流式错误返回错误 HTTP 状态。已开始的流无法更改 HTTP 状态码：Chat 输出错误数据，Responses 输出 `response.failed`，不会继续发送成功的 `stop/completed` 或 `[DONE]`。客户端必须检查终止事件，不能只判断 HTTP 200。失败的 Responses 流也会按失败记录日志。

Chat 与 Responses 的文本缓存已按协议隔离，避免混用接口时复用错误格式的流。
