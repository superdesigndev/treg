# AIGC 模型全景盘点（接入备选清单）

状态：原始盘点，供接入评估用 · 2026-09-01
数据来源：ThankYouAI 公开模型目录（`GET https://api.thankyouai.com/open/v1/models`，105 个模型）、
Replicate collections（text-to-video 94 个、text-to-image 79 个）。价格与型号时效性强，评估时需复核。

配套设计文档：`docs/AIGC-PLAN-CN.md`。treg 接入的单位是 **provider（模型厂商）**，不是单个模型：
接入一家即获得其全系模型（model 是请求体里的一个参数）。因此本文先给厂商族汇总（决策用），
再附两个平台的原始全量清单（查漏用）。

## 一、厂商族汇总 —— 生视频

| 厂商族 | 模型系 | 官方 API | 凭证形态 | 在聚合器 | 备注 |
|---|---|---|---|---|---|
| MiniMax | Hailuo 2.3 / 2.3-fast / 02 · video-01(-director) · H3 / H3-Max | ✅ platform.minimax.io | Bearer（静态） | Replicate、ThankYouAI | M1 已选 |
| 字节 Seedance | 2.0 / 2.0-fast / 2.0-mini · 1.5-pro · 1-pro(-fast) · 1-lite | ✅ 火山方舟 | Bearer（静态） | Replicate、ThankYouAI | M1 已选；token 计量 |
| 快手 Kling | v3-omni · v3 · o3 (standard/pro/4K) · v2.6 / v2.1 / v2.0 / v1.6 | ✅ klingai | **JWT 按请求签发** | Replicate、ThankYouAI | 直连超出 M1 注入器边界；可经聚合器间接接入 |
| Google Veo | 3.1 / 3.1-fast / 3.1-lite · 3 / 3-fast · 2 | ✅ Gemini API | API key（静态） | Replicate、ThankYouAI | Gemini API 也是异步任务形态 |
| OpenAI Sora | sora-2 · sora-2-pro | ✅ /v1/videos | Bearer（静态） | Replicate | 产物是二进制 `/content`（描述符 fetch 模式） |
| Runway | gen-4.5 · gen-3a | ✅ dev API | Bearer + 版本头 | Replicate | |
| 阿里 Wan（通义万相） | wan-3 · 2.7(-t2v) · 2.6 · 2.5 · 2.2 · 2.1 · video-edit | ✅ DashScope/百炼 | Bearer（静态） | Replicate（含开源加速版）、ThankYouAI | 开源权重另有 wavespeedai/prunaai 托管变体 |
| Luma | ray-3.2 · ray-2 (540p/720p) · ray-flash-2 | ✅ lumalabs | Bearer | Replicate | |
| PixVerse | v6 · v5.6 · v5 · v4.5 · v4 · lipsync | ✅ | key | Replicate、ThankYouAI | |
| 生数 Vidu | q3-pro / q3-turbo · q2 | ✅ | key | Replicate、ThankYouAI | |
| 腾讯混元 | hunyuan-video (fast) | 开源权重 + 腾讯云 | - | Replicate、ThankYouAI(经 novita) | 直连需腾讯云签名体系 |
| xAI | grok-imagine-video / -1.5 | ✅ x.ai | Bearer | Replicate | |
| 口播/数字人 | HeyGen avatar4 · Creatify aurora · VEED fabric · Infinitalk · sync.so lipsync | 各家有 API | 各异 | ThankYouAI、Replicate | 与"生成"分属不同 job，暂不入 M1 分类 |
| 开源长尾 | LTX-Video · Mochi-1 · CogVideoX · AnimateDiff 系 · Pyramid-Flow · Zeroscope 等 | 无官方托管 | - | Replicate | 只能经聚合器接入 |

## 二、厂商族汇总 —— 生图

| 厂商族 | 模型系 | 官方 API | 凭证形态 | 在聚合器 | 备注 |
|---|---|---|---|---|---|
| BFL Flux | flux-2 (max/pro/flex/klein) · 1.1-pro(-ultra) · kontext (pro/max) · dev/schnell | ✅ api.bfl.ai | `x-key`（静态） | Replicate、ThankYouAI(经 novita) | M1 候选；异步 + 动态轮询 URL |
| Google | nano-banana (·2·pro) · Imagen 4 (fast/ultra) · Gemini 图像 | ✅ Gemini API | API key | Replicate、ThankYouAI | |
| OpenAI | gpt-image-2 · gpt-image-1.5 | ✅ | Bearer | Replicate、ThankYouAI | 同步 |
| 字节 Seedream | 5 · 5-lite · 4.5 | ✅ 火山方舟 | Bearer | Replicate、ThankYouAI(经 novita) | 与 Seedance 同账号，接入边际成本低 |
| Recraft | v4.1 · v4 (pro/svg) · v3 (svg) | ✅ | Bearer | Replicate | M1 同步类候选；SVG 独有 |
| Ideogram | v3 (turbo/balanced/quality) | ✅ | key | Replicate | 排版强项；同步 |
| Midjourney | v7 (fast/turbo/upscale/variation) | ❌ **无官方 API** | - | ThankYouAI(经 youchuan 转售) | 只能靠非官方转售，treg 不宜直listing |
| 阿里 | wan 2.7 image (pro) · Qwen-Image (edit) | ✅ DashScope | Bearer | Replicate、ThankYouAI | |
| Stability | SD 3.5 (large/turbo/medium) · SDXL | ✅ | key | Replicate | |
| MiniMax | image-01 | ✅ | Bearer | Replicate、ThankYouAI | 与视频同账号 |
| 其他闭源 | 腾讯混元 image-3 · Luma Photon(-flash) · xAI grok-imagine-image · Leonardo lucid-origin · bria 3.2 | 各家有 | 各异 | Replicate | |
| 编辑/增强工具 | Topaz enhance · novita (背景移除/擦除/inpaint/outpaint/upscale) · flux kontext edit · qwen edit | 各异 | - | ThankYouAI、Replicate | job 属 image-gen.edit / 增强，另行分类 |
| 开源长尾 | HiDream · SANA · Kandinsky · Playground · Proteus · sticker/emoji 微调系 | 无 | - | Replicate | 经聚合器 |

## 三、音频（ThankYouAI 有、暂不入 M1）

fish-audio TTS · ElevenLabs sound-effect · vits-svc 变声。将来若开 `audio-gen` platform 再议。

## 四、聚合器本身作为 provider 的路线

盘点中发现的结构性事实：ThankYouAI 自己也在转售聚合器（大量模型 vendor=novita；Midjourney 经
youchuan）。对 treg 的含义：**接一家聚合器 = 一次拿到整个长尾**。

| 聚合器 | 特点 | 与描述符的适配 |
|---|---|---|
| Replicate | 模型最全（官方厂商多有一方入驻）；统一 predictions 协议 | submit-poll + `urls.get` 动态 URL，天然适配 |
| fal.ai | 快、闭源模型全 | queue API：`status_url`/`response_url` 动态 URL |
| novita | 便宜、开源模型全 | 任务式，静态轮询 |

权衡：经聚合器接入省 N 次 listing、统一一套协议与账单，但多一层加价、依赖其可用性，
且"比价"变成比聚合器而非比厂商。可作为长尾补充路线与直连并存（直连家族在前，聚合器兜长尾）。

## 五、原始清单 A：ThankYouAI（105 模型，按其分类）

计价单位 points（1 point 的美元换算评估时确认）。

**Text to Video (22)**：wan/v2.7/video · wan/v2.6 · wan/v2.5/preview/video ·
bytedance/seedance/v2/video · v2/fast · v2/mini · v1.5/pro · seedance/v2/lotus ·
kling/v3.0/standard · v3.0-turbo · v3.0/pro · v3/4k · o3/standard · o3/pro · o3/4k ·
vidu/q3/pro · minimax/hailuo/v2.3 · hailuo/v2 · video-01 · pixverse/v4.5 ·
google/veo/v3.1 · hunyuan/video/fast（11-100 points/秒不等）

**Image to Video (22)**：kling o3 系 (standard/pro/4k x i2v/reference/@7-ref) ·
kling v3.0/pro · v3/4k · vidu q2 (i2v/首尾帧) · minimax hailuo v2.3/fast · v2 ·
google veo v3.1 (i2v/首尾帧/reference) · wan2.2-animate-replace · wan2.6-reference-flash ·
数字人：creatify/aurora · heygen/avatar4 · veed/fabric-1.0 · infinitalk

**Video to Video (11)**：kling o3 (video-to-video/video-edit x standard/pro) ·
heygen/video-translate · video/face-swap · dreamactor-v2（字节）·
sync-lipsync v3 · v2/pro · veed/lipsync · pixverse/lipsync

**Video Edit (1)**：wan/v2.7/video-edit

**Text to Image (19)**：wan v2.7(/pro) · google nano-banana (基础/pro/v2) ·
youchuan/midjourney-v7(-fast/-turbo) · openai/gpt-image-2 · minimax/image/v1 ·
seedream v4.5 · v5/lite · flux v2 (dev/flex/pro) · qwen/image · z-image/turbo(-lora) · glm/image

**Image to Image (16)**：novita 工具组（背景移除/upscale x2/erase/inpaint/cleanup/outpaint/
artifact-repair/reimagine）· nano-banana edit 系 x3 · gpt-image-2/edit ·
minimax image/v1/live · flux/v1/kontext/pro/edit · qwen/image/edit

**Image Enhance (5)**：midjourney upscale/variation · topaz enhance/enhance-gen/tool

**Image to Text (5)**：gemini 2.5 flash/pro · deepseek/ocr/v2 · qwen2.5-vl-72b · novita to-prompt

**音频 (4)**：fish-audio TTS · selfhost/voice-design · elevenlabs/sound-effect · vits-svc 变声

## 六、原始清单 B：Replicate

**text-to-video collection（94 条，去重后按族）**：
alibaba/wan-3 · happyhorse-1.0/1.1 · wan-video/wan-2.7-t2v · 2.5-t2v(-fast) ·
2.5-i2v(-fast) · 2.2-t2v/i2v-fast · 2.2-i2v-a14b · 2.1-1.3b · wavespeedai/wan-2.1 (t2v-720p ·
i2v-720p/480p) · bytedance/seedance-2.0(-fast) · 1.5-pro · 1-pro(-fast) · 1-lite ·
kwaivgi/kling-v3-omni · v3 · v2.6 · v2.1(-master) · v2.0 · v1.6-pro/standard ·
google/veo-3.1(-fast/-lite) · veo-3(-fast) · veo-2 · openai/sora-2(-pro) ·
minimax/hailuo-2.3(-fast) · hailuo-02 · video-01(-director) · runwayml/gen-4.5 ·
luma/ray-3.2 · ray-2 (540p/720p) · ray-flash-2 (540p/720p) · pixverse v4-v6 ·
vidu/q3-pro/turbo · xai/grok-imagine-video(-1.5) · veed/fabric-1.0 · leonardoai/motion-2.0 ·
prunaai/p-video(-animate) · tencent/hunyuan-video · lightricks/ltx-video · genmoai/mochi-1 ·
开源长尾：cogvideox-5b · pyramid-flow · animatediff 系 x4 · zeroscope · videocrafter ·
i2vgen-xl · pia · tooncrafter · video-morpher · hotshot-xl · text2video-zero · damo ·
deforum · 工具类（非生成）：sam-2-video · deoldify · basicvsr 超分 · robust_video_matting 等

**text-to-image collection（79 条，按族）**：
google/nano-banana-2/-pro · imagen-4(-fast/-ultra) · openai/gpt-image-2 · 1.5 ·
black-forest-labs/flux-2 (max/pro/flex/klein-4b) · flux-1.1-pro(-ultra) · kontext (pro/max) ·
dev(-lora) · schnell · bytedance/seedream-5(-lite) · 4.5 · sdxl-lightning-4step ·
recraft-ai/recraft-v4.1 · v4 (pro/svg 系) · v3 (svg) · ideogram-ai/v3 (turbo/balanced/quality) ·
quiverai/arrow-1.1(-max) · xai/grok-imagine-image · stability-ai/sd-3.5 (large/turbo/medium) ·
sdxl · qwen/qwen-image · wan-video/wan-2.7-image(-pro) · minimax/image-01 · luma/photon(-flash) ·
tencent/hunyuan-image-3 · bria/image-3.2 · fibo · leonardoai/lucid-origin ·
prunaai 加速系（p-image/z-image-turbo/hidream x3/flux-fast/sdxl-lightning/wan-2.2-image）·
nvidia/sana(-sprint) · 开源长尾：playground-v2.5 · kandinsky-2/2.2 · proteus · realvisxl ·
dreamshaper · open-dalle · ssd-1b · realistic-vision · controlnet/lora 工具系 · comfyui 万能入口
