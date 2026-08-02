# 前端复核修复 TODO

来源：`FRONTEND_TODO.md` 实现后的复核。当前前端存在两个阻断问题，必须先修复并重新构建。

## P0：恢复可运行状态

### 1. 删除未定义的 `imageModels` 引用

文件：`frontend/src/components/video.jsx`

问题：

`FormView` 已不接收 `imageModels`，但 `VideoPanel` 仍传递：

```jsx
<FormView
  voices={voices}
  imageModels={imageModels}
  videoModels={videoModels}
  ...
/>
```

`imageModels` 在 `VideoPanel` 作用域不存在，React 渲染时直接抛 `ReferenceError`，应用无法加载。

修改：

- 删除 `imageModels={imageModels}`。
- 确保 `VideoPanel` props 与 `FormView` props 一致。
- 全仓搜索 `imageModels`，删除成片流程中已失效的 prop、state 和注释；不要影响普通图片生成页面。

验收：

- 页面加载无 React runtime error。
- 点击“一键成片”可打开表单。
- 表单中不存在图片模型选择。

### 2. 修复 API 请求 headers 的 undefined 展开

文件：`frontend/src/api.js`

问题：

当前代码：

```js
headers: { 'Content-Type': 'application/json', ...opts.headers },
```

`api.health()` 等调用不传 `headers`，对 `undefined` 展开会抛错，导致初始化请求失败。

修改：

```js
headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
```

验收：

- `api.health()`、`api.models()`、`api.jobs()` 无需传 headers 也可正常调用。
- 页面初始化可加载模型、健康状态、任务列表。
- 使用浏览器 console 确认没有 TypeError。

## P1：完成已写 JSX 的视觉与交互实现

### 3. 为新增状态组件补齐 CSS

文件：`frontend/src/App.css`

当前未定义 class：

- `.vp-shot-thumb`
- `.vp-idle-hint`
- `.vp-result-summary`
- `.project-card`
- `.explore-card-selected`
- `.explore-card-capabilities`

修改：

- 为每个 class 补足桌面、窄屏、focus、状态色样式。
- `vp-shot-thumb` 固定缩略图尺寸，视频 `object-fit: cover`，无视频时用一致占位。
- `vp-result-summary.warn` 与 `.err` 用文字、图标/边框和语义色区分。
- `project-card` 呈现为 compact project summary，不要复用普通气泡视觉。
- `explore-card-capabilities` 用可换行的低对比能力 chips，避免长 `input_use` 撑破卡片。
- `explore-card-selected` 必须可见，不依赖颜色。

验收：

- 成片进度、部分失败、模型能力信息均有明确视觉层级。
- 375px 宽下没有横向滚动。
- 任何状态不只依赖颜色表达。

### 4. 补齐成片镜头阶段文案

文件：`frontend/src/components/video.jsx`

修改 `stageLabel()`：

- 支持 `planned`、`video`、`tts`、`done`、`concatenated`、`failed`。
- 成功和失败优先用 status；运行中展示当前 stage。
- 不向用户暴露原始英文 stage 值。

验收：

- 当前后端返回 `concatenated` 时显示中文“等待合成”或“已加入成片”。
- 无任何 raw stage string 直接出现在 UI。

### 5. 修正镜头视频缩略图实现

文件：`frontend/src/components/video.jsx`

问题：

`poster={shot.videoUrl}` 把 MP4 URL 当图片 poster 使用，浏览器不会稳定展示缩略图。

修改：

- 无真实图片 URL 时移除 `poster`。
- 进度列表的 `<video>` 保持 `muted preload="metadata"`，不加 `controls`。
- 结果页分镜可以保留播放能力，但不把 MP4 作为 poster。
- 若后端未来返回 thumbnail URL，再使用正确图片 URL 作为 `poster`。

验收：

- 控制台无 poster MIME / 资源错误。
- 已生成镜头在进度页和结果页可预览或有一致占位。

## P1：修正成片任务入口和表单层级

### 6. 按点击的 job ID 打开对应成片任务

文件：`frontend/src/App.jsx`、`frontend/src/components/chat.jsx`

问题：

项目卡派发的事件包含 `job.id`，但监听器忽略它，`openVideoPanel()` 只恢复最近任务。点击旧任务会打开最新任务。

修改：

- 让 `openVideoPanel(jobId?)` 接收可选任务 ID。
- 有 jobId 时请求并恢复该任务；没有时才使用 `latestVideoJobId`。
- 事件监听器传入 `e.detail`。
- 侧栏点击成片任务也传入对应 ID。
- 不新增路由。

验收：

- 有多个成片任务时，点击任一项目卡和最近任务都打开正确任务。
- 刷新后仍可打开指定历史任务。

### 7. 补齐成片表单的创作摘要和高级分组

文件：`frontend/src/components/video.jsx`、`frontend/src/App.css`

修改：

- 在描述后显示输出摘要：镜头数、每镜时长、估算总时长、比例、预计耗时。
- 视频模型与语音保留在主表单。
- 将 AI 拆镜、分镜模型放入可展开“高级选项”。
- 模型切换时显示简短提示，例如“已更新为支持的 6s / 16:9”。
- 无视频模型时禁用提交并显示“正在加载模型”或“没有可用视频模型”。

验收：

- 常见 900px 高笔记本上能同时看见描述、输出摘要、视频模型、语音和提交按钮。
- 用户能理解总时长是估算，不是最终保证。

## P1：响应式布局

### 8. 实现 640px 以下底部导航

文件：`frontend/src/App.css`，必要时 `frontend/src/components/chrome.jsx`

修改：

- 640px 以下，`.app` 改为单列，侧栏改为 viewport 底部导航。
- 保留对话、探索、日志三个入口；隐藏品牌文字、最近任务、账号状态、刷新模型按钮。
- 使用 `env(safe-area-inset-bottom)` 处理 iOS 安全区。
- 主内容、composer 和视频面板避免被底部导航遮住。

验收：

- 375px 宽没有横向滚动。
- 底部导航、composer、提交按钮均可点击。
- 页面内容底部不会被遮住。

### 9. 移除聊天区固定 280px 高度魔数

文件：`frontend/src/App.css`

修改：

- 移除 `.thread` 的 `max-height: calc(100dvh - var(--topbar-h) - 280px)`。
- 让 `.main`、`.thread`、`.composer-wrap` 的 flex / sticky 关系使用剩余可用高度。
- 小屏缩短 welcome 的顶部空白和建议卡尺寸。

验收：

- 桌面和手机均可同时看到最新任务与 composer。
- 打开参数浮层时不覆盖输入内容。

## P2：可访问性完成项

### 10. 正确实现 dialog 焦点管理

文件：`frontend/src/components/video.jsx`、`frontend/src/components/primitives.jsx`

问题：

- 标题有 `tabIndex="-1"`，但没有调用 `.focus()`。
- 没有 focus trap。
- `GhostButton` 不支持 ref 转发，因此关闭时无法归还触发按钮焦点。

修改：

- 使用 `forwardRef` 让 `GhostButton` 支持 ref。
- dialog 打开后，将焦点移到描述框或标题。
- 实现最小 focus trap：Tab / Shift+Tab 在 dialog 内循环。
- Escape 和关闭按钮都能关闭并将焦点归还触发按钮。

验收：

- 键盘可打开、填写、关闭成片 dialog。
- Tab 不会进入背后的页面。
- 关闭后焦点回到“一键成片”按钮。

### 11. 完成自定义下拉框键盘操作，或改用原生 select

文件：`frontend/src/components/video.jsx`

修改：

- 支持 Escape 关闭。
- 支持 ArrowUp / ArrowDown 移动活动选项，Enter 选择。
- 如果这会明显增加复杂度，替换为原生 `<select>`；优先正确、少代码的方案。

验收：

- 不用鼠标可选择视频模型和语音。

## P2：状态和选择反馈

### 12. 从探索页选模型后显示确认

文件：`frontend/src/App.jsx`

修改：

- `onPickFromExplore()` 设置模型后调用已有 `showMsg()`。
- 文案示例：`已选择 seedance:seedance_2.0`。
- 不增加 toast 库。

验收：

- 选择模型后回到对话页，composer 下方出现一次确认信息。

## 验证顺序

1. `npm run build`。
2. `docker compose up -d --build`。
3. 浏览器访问 `http://localhost:19999/firefly-studio`，确认 console 无 error。
4. 桌面 1440px：打开、关闭、填写一键成片表单。
5. 移动端 375px：检查无横向滚动，导航和 composer 可用。
6. 键盘：Tab、Shift+Tab、Escape、Enter 完成 dialog 与模型选择。
7. 用至少两个历史成片任务验证点击项目卡会打开正确 job。
