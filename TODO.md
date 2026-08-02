# Firefly Studio 优化 TODO

目标：优先修复会导致成片失败、状态错误、资源浪费和公网暴露的问题；不引入 Celery、Redis、微服务或复杂的模型路由系统。

## P0：成片正确性

### 1. 修复 TTS 失败导致合成崩溃

文件：`video_pipeline.py`

现状：

- `clips_ok` 包含所有成功视频。
- `audio_ok` 只包含同时拥有视频和 TTS 音频的镜头。
- `concat_with_audio()` 要求两者长度相同。
- 任意一个镜头 TTS 失败，最终合成会因断言失败而失败。

修改：

- 保证每个参与拼接的视频镜头都有对应音轨。
- TTS 失败时，为该镜头生成对应长度的静音音频，或在 `concat_with_audio()` 中支持无音轨镜头。
- 移除依赖 `assert len(audios) == n` 的脆弱调用路径。
- 合成失败后，任务不得标记为成功。

验收：

- 模拟一个镜头 TTS 失败、其他镜头视频成功。
- 最终 MP4 仍可生成。
- 失败镜头对应片段静音。
- 任务结果和日志明确标记 TTS 部分失败。

### 2. 以实际媒体时长生成稳定时间轴

文件：`video_pipeline.py`

现状：

- `shot_durations` 只用于音频延时计算。
- 视频未按计划时长裁剪或补齐。
- 上游实际视频时长与 TTS 时长不一致时，旁白可能被截断，最终总时长漂移。

修改：

- 每个镜头的最终时长取以下最大值：

```text
max(实际视频时长, 实际 TTS 时长, 目标镜头时长)
```

- 视频短于最终时长时，冻结最后一帧补齐。
- 视频长于最终时长时，裁剪至最终时长。
- 音频短于最终时长时补静音。
- 基于处理后的最终镜头时长计算所有音频 offset。
- 统一处理视频分辨率、帧率、像素格式后再 concat。

验收：

- 4 秒视频 + 7 秒旁白不会截断旁白。
- 8 秒视频 + 4 秒旁白在目标时长下可稳定裁剪。
- 最终时长等于所有镜头最终时长的总和。
- 每段旁白从对应镜头开始位置播放。

### 3. 修正视频任务成功状态

文件：`app.py`

现状：

```python
status = "succeeded" if final_path else ("failed" if not shots else "succeeded")
```

只要有镜头，即使最终 MP4 不存在，任务也会标记成功。

修改：

- `final_video_path` 存在且文件存在时才标记 `succeeded`。
- 有可用镜头但无法合成时，增加 `partial` 状态。
- 如果不扩展前端状态枚举，先统一标记为 `failed`，但保留 manifest 和分镜产物。
- 前端必须能显示“分镜已生成，成片失败”的明确状态。

验收：

- 无 FFmpeg 时任务不显示“完成”。
- 合成失败时结果页不显示为成功。
- 有最终 MP4 时才显示最终预览播放器。

## P0：减少无效生成消耗

### 4. 删除当前未使用的关键帧生成步骤

文件：`video_pipeline.py`

现状：

- 每个镜头先文生图。
- 视频生成仍是纯文生视频。
- 关键帧没有作为 `referenceBlobs` 传给视频模型。
- 每镜额外消耗一次图片生成时间和额度。

修改：

- 第一阶段删除 `generate_shot_image()` 的强制调用。
- 取消关键帧进度阶段，或改为“生成视频”。
- 最终缩略图从生成视频中抽取首帧或使用视频 URL。
- 保留未来图生视频接口所需的数据结构，但不要提前调用图片模型。

不做：

- 本轮不实现参考图上传和 `referenceBlobs`。
- 等确认上游上传 API 后，再单独实现真正的图生视频路径。

验收：

- 成片任务每个镜头只调用视频生成和 TTS。
- 相同镜头数下生成请求数量减少。
- 前端仍能显示镜头缩略信息或视频预览。

## P1：任务执行与恢复

### 5. 消除多 worker 下本地 semaphore 失效的问题

文件：`Dockerfile`、`app.py`

现状：

- `threading.Semaphore(2)` 只对单个 Python 进程有效。
- Docker 使用 `gunicorn -w 2`，实际可能并发执行 4 个成片任务。
- daemon thread 在 Gunicorn worker 重启后会直接丢失。

修改：

- Docker 的 Gunicorn 改为单 worker。
- 保持现有 SQLite + thread 方式，不引入 Celery / Redis。
- 在服务启动时将遗留的 `queued` 和 `running` 任务更新为失败或中断状态。
- 提示用户重新提交，不伪造续跑能力。

验收：

- 重启服务后，遗留任务不再永久显示“生成中”。
- 同时提交多个任务时，总并发符合 semaphore 限制。
- Docker 配置中只有一个 Gunicorn worker。

### 6. 恢复页面刷新后的成片任务详情

文件：`frontend/src/App.jsx`、`frontend/src/components/video.jsx`

现状：

- 关闭面板后任务仍运行。
- 刷新页面后 `videoJob` 丢失。
- 用户无法回到当前视频任务的分镜进度、manifest 或最终播放器。

修改：

- 从已有 `/api/jobs` 列表中找到最近的 `kind === "video_pipeline"` 任务。
- 打开“一键成片”时恢复最近任务：
  - `queued` / `running`：进入进度页并恢复轮询。
  - `succeeded` / `partial` / `failed`：进入结果或失败详情页。
- 不新增前端路由，不新增项目表。

验收：

- 视频生成中刷新页面后，重新打开面板可看到实时进度。
- 完成任务刷新后，仍可回到最终视频和镜头详情。
- 不会同时对同一任务创建多个轮询定时器。

## P1：部署与访问安全

### 7. 修复生产 Docker 数据挂载目录不一致

文件：`docker-compose.yml`

现状：

- 主 Compose 将 `./data` 挂载到 `/data`。
- 应用默认读取 `/app/data`。
- 本地 Compose 使用 `/app/data`，两套行为不一致。
- SQLite 和 token 可能在容器重建后丢失或无法读取。

修改：

```yaml
volumes:
  - ./data:/app/data
  - ./outputs:/app/outputs
```

- 检查 Dockerfile 中 `STORAGE_JSON` / `TOKEN_JSON` 写入路径，统一写入 `/app/data`。
- 或显式设置 `FIREFLY_STORAGE` 和 `FIREFLY_TOKEN_FILE`，但不要同时维护两套目录逻辑。

验收：

- 容器重启后 SQLite 任务记录仍存在。
- token 文件可被应用正常读取。
- 主 Compose 和 local Compose 使用同一数据目录。

### 8. 收紧公网 API 访问

文件：`docker-compose.yml`、`nginx.conf`、`app.py`、部署文档

现状：

- `19999` 可公开暴露。
- `CORS_ORIGINS: "*"`。
- `/api/generate`、`/api/video/generate` 可直接消耗账号额度。
- `/api/jobs`、`/api/logs`、`/api/health`、`/outputs` 可暴露隐私与系统信息。

修改：

- 默认部署不开放公网端口，优先通过 Tailscale、SSH Tunnel 或 Cloudflare Access 访问。
- 若必须公网访问，至少使用 Nginx Basic Auth 或 Cloudflare Access。
- `/api/health` 不返回绝对数据库路径、token client ID、详细鉴权状态。
- 生产环境将 `CORS_ORIGINS` 设置为实际前端域名。
- 在部署文档中明确“不能裸露公网端口”。

验收：

- 未认证请求无法调用生成 API。
- `/api/health` 不暴露本地绝对路径或 token 元信息。
- 跨域来源不再默认允许全部域名。

## P2：分镜与模型控制

### 9. 改善短提示词的分镜策略

文件：`video_pipeline.py`

现状：

- 短提示词最少强制拆 3 镜。
- 字符均分会产生不自然旁白，例如“猫在 / 阳光 / 下打盹”。
- 非 LLM 模式下，视觉提示词混合英文模板与中文碎片。

修改：

- 短提示词允许单镜，或派生为同一场景的三种明确镜头：
  - 建立镜头
  - 主体近景
  - 细节镜头
- 禁止将用户输入按字符直接分段作为旁白。
- LLM 分镜输出增加最小字段：
  - `visual`
  - `narration`
  - `duration`
  - `camera`
  - `motion`
  - `negative_prompt`
- 视觉提示词统一使用英文；旁白保留用户语言。
- LLM 系统提示词要求：镜头之间必须有明确视觉变化，避免重复描述。

验收：

- “猫在阳光下打盹”不会产生字符碎片旁白。
- 每个镜头都有独立视觉变化。
- LLM 返回格式异常时仍能可靠回退。

### 10. 按模型能力约束视频参数

文件：`frontend/src/components/video.jsx`、`frontend/src/App.jsx`、必要时 `app.py`

现状：

- 成片面板统一提供固定时长、比例和音频选项。
- 未根据所选模型的 `durations`、`aspect_ratios`、`audio`、`input_use` 限制参数。
- 不支持的参数会到上游才失败。

修改：

- 从已有 `/api/models` 数据读取模型能力。
- 时长只显示当前视频模型支持的 `durations`。
- 比例只显示当前模型支持的 `aspect_ratios`。
- 不支持音频时禁用或隐藏音频选项。
- 模型变更后自动归一化当前不兼容选项。

验收：

- 前端不能提交当前模型不支持的时长和比例。
- 用户切换模型时，参数自动切换到有效值。
- 后端仍保留基础校验，不能只依赖前端。

## P2：文档、测试与可观测性

### 11. 对齐 TTS 文档与实际实现

文件：`video_pipeline.py`、`README.md`

修改：

- 删除已废弃的 `tts.22y.workers.dev` 描述。
- 明确当前使用 `edge-tts`。
- 删除未使用的 `TTS_TIMEOUT`，或将其真正用于 TTS 调用的超时控制。
- 文档说明 TTS 失败时的降级策略。

### 12. 补最小测试

文件：`tests/test_video_pipeline.py`，必要时新增单个测试文件

增加以下测试：

- 有视频、无 TTS 的镜头仍能生成可合成的输入列表。
- 时间轴长度计算正确。
- 视频短于旁白时最终时长正确。
- 没有 `final_video_path` 时任务不可标记 `succeeded`。
- 短提示词不产生按字符切割的旁白。
- LLM 返回无效 JSON 时回退到启发式分镜。

保持：

- 不引入 pytest 或新测试框架。
- 继续使用现有 `assert` / 独立 Python 测试脚本方式。

### 13. 修正文档中的测试命令

文件：`README.md`

现状：

```bash
python tests/test_video_pipeline.py
```

在系统 Python 未安装依赖时会失败；项目 `.venv/bin/python` 下可通过。

修改：

- 文档要求先执行依赖安装。
- 若仓库维护 `.venv`，明确使用：

```bash
.venv/bin/python tests/test_video_pipeline.py
```

验收：

- README 中的命令在推荐环境中可直接运行。
- 测试输出应包含通过数量。

## 实施顺序

1. 完成 P0 的 1-4。
2. 完成 P1 的 5-8。
3. 完成 P2 的 9-13。

## 实现完成后的复核材料

- 变更文件列表。
- 所有测试命令及完整输出。
- 一次“单镜 TTS 失败”的真实或 mock 验证结果。
- 一次无 FFmpeg 或合成失败时的任务状态验证结果。
- Docker 数据目录与认证保护的验证说明。
