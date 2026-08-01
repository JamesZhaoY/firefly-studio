// explore: model library page. Real generated outputs lead the page; model cards
// remain the functional entry point into the composer.

import { useState } from 'react'

function outputKind(output) {
  if (output.type) return output.type
  return /\.(mp4|webm|mov)(\?|$)/i.test(output.url || '') ? 'video' : 'image'
}

function RecentOutput({ output, prompt }) {
  const kind = outputKind(output)
  return (
    <a className="explore-output" href={output.url} target="_blank" rel="noreferrer">
      {kind === 'video' ? (
        <video src={output.url} muted preload="metadata" />
      ) : (
        <img src={output.url} alt={prompt || '已生成作品'} loading="lazy" />
      )}
      <span className="explore-output-shade" />
      {prompt && <span className="explore-output-prompt">{prompt}</span>}
    </a>
  )
}

export function ExploreCard({ model, selectedKey, onPick }) {
  const k = `${model.id}@@${model.version}`
  const isSelected = k === selectedKey
  return (
    <div
      className={`explore-card ${isSelected ? 'selected' : ''}`}
      onClick={() => onPick(model)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onPick(model)
        }
      }}
    >
      <span className="explore-card-kind">{model.kind || 'image'}</span>
      <span className="explore-card-title">{model.id}</span>
      <span className="explore-card-ver">{model.version}</span>
      <div className="explore-card-foot">
        {model.provider && (
          <span className="explore-card-provider">{model.provider}</span>
        )}
        {model.release && (
          <span className={`explore-card-rel ${model.release === 'alpha' ? 'alpha' : ''}`}>
            {model.release}
          </span>
        )}
      </div>
    </div>
  )
}

export function ExplorePage({
  models, totalCount, filter, setFilter, selectedKey, onPick, jobs,
}) {
  const [kindFilter, setKindFilter] = useState('all')
  const recentOutputs = jobs
    .flatMap((job) => (job.outputs || job.files || []).map((output) => ({
      output,
      prompt: job.params?.prompt || job.prompt,
    })))
    .filter(({ output }) => output.url)
    .slice(0, 6)
  const matching = models.filter((model) =>
    kindFilter === 'all' || (model.kind || 'image') === kindFilter,
  )
  const visible = filter ? matching : matching.slice(0, 36)

  return (
    <section className="explore-content">
      <div className="explore-hero">
        <div>
          <span className="explore-kicker">创作探索</span>
          <h1>从一个模型开始创作</h1>
          <p>浏览 {totalCount} 个可用模型，选择后直接回到工作台生成。</p>
        </div>
        <div className="explore-hero-stat" aria-label={`${totalCount} 个可用模型`}>
          <strong>{totalCount}</strong>
          <span>个可用模型</span>
          <small>图像 / 视频 / 音频</small>
        </div>
      </div>

      {recentOutputs.length > 0 && (
        <section className="explore-recent" aria-labelledby="recent-work-title">
          <div className="explore-section-head">
            <h2 id="recent-work-title">最近生成</h2>
            <span>{recentOutputs.length} 个作品</span>
          </div>
          <div className="explore-output-grid">
            {recentOutputs.map(({ output, prompt }, index) => (
              <RecentOutput
                key={`${output.url}-${index}`}
                output={output}
                prompt={prompt}
              />
            ))}
          </div>
        </section>
      )}

      <div className="explore-section-head models-head">
        <h2>选择模型</h2>
        <span>按能力筛选</span>
      </div>
      <div className="explore-filter">
        <input
          type="search"
          placeholder="按名字 / 提供商过滤"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <div className="explore-kind-filter" aria-label="模型类型">
          {[
            ['all', '全部'],
            ['image', '图像'],
            ['video', '视频'],
            ['audio', '音频'],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={kindFilter === value ? 'active' : ''}
              onClick={() => setKindFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="explore-grid">
        {visible.map((m) => (
          <ExploreCard
            key={`${m.id}@@${m.version}`}
            model={m}
            selectedKey={selectedKey}
            onPick={onPick}
          />
        ))}
      </div>
      {!visible.length && <div className="explore-empty">没有匹配的模型</div>}
    </section>
  )
}
