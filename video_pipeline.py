"""全自动视频生成流水线（文字 → 多镜头脚本 → 关键帧 → 镜头 → 配音 → 合成）。

职责：
  1. 把一段自然语言描述拆成 N 个分镜（3-6 镜头，纯启发式，不调 LLM）
  2. 每个分镜：先出关键帧（image）→ 出镜头视频（video）→ 出配音（TTS）
  3. 把所有镜头按顺序拼接，并把每段配音混进对应时间轴
  4. 落盘 + 把结果写回 SQLite jobs 表

外部依赖：
  - firefly_pipeline.generate_image / generate_video （已有）
  - https://tts.22y.workers.dev/tts  （外部 TTS，已探测接口）
  - 系统 ffmpeg（可选，没有则降级为 manifest 模式）
"""

from __future__ import annotations

import json
import asyncio
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
import os
from typing import Any, Callable, Iterable

import firefly_pipeline as fp

# ── 常量 ───────────────────────────────────────────────────────

APP_ROOT = Path(__file__).resolve().parent
OUT_DIR = APP_ROOT / "outputs"
VIDEO_DIR = OUT_DIR / "videos"
TTS_DIR = OUT_DIR / "tts"
TEMP_DIR = OUT_DIR / "tmp"

TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_FALLBACK_VOICE = "en-US-JennyNeural"
TTS_TIMEOUT = 60

# 默认镜头数范围
MIN_SHOTS = 3
MAX_SHOTS = 6
DEFAULT_SHOT_DURATION = 6  # 秒

# 默认视频 / 图片模型（与现有 app.py 行为一致，让 firefly_pipeline 选预设）
DEFAULT_IMAGE_MODEL = ""
DEFAULT_IMAGE_MODEL_VERSION = ""
DEFAULT_VIDEO_MODEL = ""
DEFAULT_VIDEO_MODEL_VERSION = ""
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_VOICE = TTS_DEFAULT_VOICE

# 关键帧解析度（firefly_pipeline.VIDEO_SIZE_BY_ASPECT 的 16:9 是 854x480）
DEFAULT_KEYFRAME_SIZE = "1280x720"


# ── Storyboard 拆分（纯函数，方便测试） ──────────────────────────


# 显式分镜提示词（zh + en）
_SEGMENT_CUES_ZH = [
    r"第[一二三四五六七八九十]幕",
    r"第[一二三四五六七八九十]场",
    r"第[一二三四五六七八九十]段",
    r"首先",
    r"然后",
    r"接着",
    r"随后",
    r"之后",
    r"最后",
    r"结尾",
    r"开头",
]
_SEGMENT_CUES_EN = [
    r"\bfirstly\b",
    r"\bthen\b",
    r"\bnext\b",
    r"\bafter\s+that\b",
    r"\bfinally\b",
    r"\bopening\b",
    r"\bclosing\b",
    r"\bscene\s+\d+\b",
]

_CUE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in _SEGMENT_CUES_ZH + _SEGMENT_CUES_EN
]


def _count_cues(prompt: str) -> int:
    """从提示词里数显式镜头提示词个数（去重，每个模式只算 1 次）。"""
    seen = 0
    for pat in _CUE_PATTERNS:
        if pat.search(prompt):
            seen += 1
    return seen


def _length_based_shot_count(prompt: str) -> int:
    """提示词长度 → 镜头数。
    ≤ 40 字 → 3  短（一条社交动态）
    ≤ 120 字 → 4  中（产品 demo）
    ≤ 240 字 → 5  长（短广告）
    其它 → 6   超长（专题片段）
    """
    n = len(prompt.strip())
    if n <= 40:
        return 3
    if n <= 120:
        return 4
    if n <= 240:
        return 5
    return 6


@dataclass
class ShotPlan:
    """一个分镜的预规划（还没真去生成）。"""

    index: int
    visual_prompt: str  # 给图 / 视频用的英文式 prompt
    narration: str      # 给 TTS 的旁白（直接用原文叙述 + 标记）
    duration_sec: int   # 这个镜头在最终片长里的目标时长


_CUE_PATTERN = re.compile(
    r"(首先|然后|接着|随后|之后|最后|开头|结尾|firstly|then|next|after\s+that|finally|opening|closing)",
    re.IGNORECASE,
)


def _split_by_cues(text: str) -> list[str]:
    """按显式提示词切成段（提示词保留在它后面的那段里）。"""
    parts = _CUE_PATTERN.split(text)
    out: list[str] = []
    buf = ""
    for piece in parts:
        if _CUE_PATTERN.fullmatch(piece or ""):
            buf = (buf + " " + piece).strip() if buf else piece
        else:
            chunk = (piece or "").strip()
            if chunk:
                buf = (buf + " " + chunk).strip() if buf else chunk
                out.append(buf)
                buf = ""
    if buf:
        out.append(buf)
    return [s for s in out if s]


def _split_into_segments(prompt: str, n: int) -> list[str]:
    """把原文切成 n 段叙述。
    1. 有显式分镜提示 → 按提示符劈
    2. 句子边界 ≥ n → 按句劈
    3. 都不够 → 按字符均分到 n 段（保留原文顺序）
    """
    text = prompt.strip()
    if not text:
        return []

    cue_parts = _split_by_cues(text)
    if len(cue_parts) >= n:
        head = cue_parts[: n - 1]
        tail = " ".join(cue_parts[n - 1 :])
        return head + [tail]

    sentence_parts = [s.strip() for s in re.split(r"[。！？!?\.\n]+", text) if s.strip()]
    parts = sentence_parts if len(sentence_parts) >= len(cue_parts) else cue_parts
    if len(parts) >= n:
        head = parts[: n - 1]
        tail = " ".join(parts[n - 1 :])
        return head + [tail]

    # 都不够：按字符均分到 n 段
    if len(text) <= n:
        return [text[i:i+1] for i in range(len(text))] + [text] * (n - len(text))
    size, rem = divmod(len(text), n)
    out: list[str] = []
    cursor = 0
    for i in range(n):
        take = size + (1 if i < rem else 0)
        chunk = text[cursor : cursor + take].strip()
        if not chunk:
            chunk = text[cursor : cursor + take]
        out.append(chunk)
        cursor += take
    return out


def split_storyboard(
    prompt: str,
    *,
    shot_count: int | None = None,
    duration_sec: int = DEFAULT_SHOT_DURATION,
) -> list[ShotPlan]:
    """把一段自然语言 prompt 拆成 N 个分镜的纯函数。
    不调 LLM，不调远端。
    """
    text = (prompt or "").strip()
    if not text:
        return []

    if shot_count is None:
        explicit = _count_cues(text)
        if explicit >= MIN_SHOTS:
            n = min(explicit, MAX_SHOTS)
        else:
            n = _length_based_shot_count(text)
    else:
        n = max(MIN_SHOTS, min(int(shot_count), MAX_SHOTS))

    segments = _split_into_segments(text, n)
    plans: list[ShotPlan] = []
    for i, seg in enumerate(segments, start=1):
        label = _shot_label(i, n)
        # visual_prompt: 用原片段描述 + 镜头序号引导，避免原样复制
        visual = f"{label} cinematic shot: {seg}. high quality, stable composition, cinematic lighting"
        plans.append(
            ShotPlan(
                index=i,
                visual_prompt=visual,
                narration=seg,
                duration_sec=int(duration_sec),
            )
        )
    return plans


def _shot_label(i: int, total: int) -> str:
    if total <= 4:
        order = ["opening", "middle", "climax", "closing"]
        if i - 1 < len(order):
            return order[i - 1]
    if i == 1:
        return "Opening"
    if i == total:
        return "Closing"
    return f"Scene {i}"


# ── LLM 驱动的分镜拆分（可选，默认走启发式） ──────────────────────


@dataclass
class LLMConfig:
    """OpenAI 兼容 Chat Completions 配置；env 读取。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = 20.0


def _load_llm_config() -> LLMConfig:
    """从环境变量读 LLM 配置；缺省用 opencode 本地 provider 的 baseURL/key/model。"""
    return LLMConfig(
        base_url=str(os.environ.get("LLM_BASE_URL") or "http://127.0.0.1:8317/v1").rstrip("/"),
        api_key=str(os.environ.get("LLM_API_KEY") or "local-dev-key"),
        model=str(os.environ.get("LLM_MODEL") or "gpt-5.5"),
        timeout=float(os.environ.get("LLM_TIMEOUT") or 20.0),
    )


_SYSTEM_PROMPT = (
    "你是一名分镜师。用户给一段自然语言描述,你把它拆成 N 个分镜"
    "(N 由用户指定,3-6)。每个分镜输出:\n"
    "- visual: 一行适合图像生成的视觉提示词(英文)\n"
    "- narration: 一行该镜头的旁白(中文)\n"
    "- duration: 整数秒\n"
    "输出 JSON 数组,字段 visual/narration/duration。"
)


def _strip_code_fence(text: str) -> str:
    """吃掉 markdown ```json ... ``` 围栏；找不到就把原文返回。"""
    t = (text or "").strip()
    if t.startswith("```"):
        # 去掉首行 fence
        nl = t.find("\n")
        if nl >= 0:
            t = t[nl + 2:]
        # 去掉尾 fence
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _parse_shot_array(payload: str) -> list[dict[str, Any]]:
    """从 LLM 文本里抽 JSON 数组。失败抛 ValueError。"""
    s = _strip_code_fence(payload)
    # 直接是数组
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return v
    except json.JSONDecodeError:
        pass
    # 在文本里找第一个 '[' 到匹配的 ']'
    start = s.find("[")
    if start < 0:
        raise ValueError("no JSON array found")
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                chunk = s[start : i + 1]
                v = json.loads(chunk)
                if isinstance(v, list):
                    return v
                raise ValueError("JSON not an array")
    raise ValueError("unbalanced JSON brackets")


def _coerce_shot(item: Any, *, idx: int, default_duration: int) -> ShotPlan | None:
    """把 LLM 给的一条 item 归一化到 ShotPlan；字段缺失/类型错就丢掉。"""
    if not isinstance(item, dict):
        return None
    visual = str(item.get("visual") or item.get("visual_prompt") or "").strip()
    narration = str(item.get("narration") or item.get("voice") or "").strip()
    if not visual or not narration:
        return None
    raw_dur = item.get("duration")
    try:
        dur = int(float(raw_dur)) if raw_dur is not None else int(default_duration)
    except (TypeError, ValueError):
        dur = int(default_duration)
    dur = max(3, min(12, dur))  # 夹到 [3, 12]
    return ShotPlan(index=idx, visual_prompt=visual, narration=narration, duration_sec=dur)


def _call_llm_chat(
    cfg: LLMConfig,
    messages: list[dict[str, str]],
) -> str:
    """POST /chat/completions；返回 content 字符串。失败抛 RuntimeError。"""
    body = json.dumps(
        {"model": cfg.model, "messages": messages, "temperature": 0.6},
        ensure_ascii=False,
    ).encode("utf-8")
    url = f"{cfg.base_url}/chat/completions"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")[:200]
        except Exception:
            pass
        raise RuntimeError(f"LLM HTTP {e.code}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 网络错误: {e.reason}") from e

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise RuntimeError(f"LLM 返回无 choices: {str(payload)[:200]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise RuntimeError("LLM content 字段缺失或非字符串")
    return content


def split_storyboard_llm(
    prompt: str,
    *,
    shot_count: int,
    duration_sec: int,
    llm_config: LLMConfig | None = None,
    on_log: Callable[[str], None] | None = None,
) -> list[ShotPlan]:
    """调 LLM 拆 N 个分镜；任何错误都抛 RuntimeError 让上层 fallback。"""
    cfg = llm_config or _load_llm_config()
    n = max(MIN_SHOTS, min(int(shot_count), MAX_SHOTS))
    user_msg = (
        f"用户描述：{(prompt or '').strip()}\n"
        f"N={n},每镜约 {int(duration_sec)} 秒。"
    )
    if on_log:
        on_log(f"llm.split model={cfg.model} shots={n}")
    content = _call_llm_chat(
        cfg,
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = _parse_shot_array(content)
    plans: list[ShotPlan] = []
    for i, item in enumerate(raw[:n], start=1):
        plan = _coerce_shot(item, idx=i, default_duration=duration_sec)
        if plan is not None:
            plans.append(plan)
    if not plans:
        raise RuntimeError("LLM 返回的镜头全无效")
    # 不足 N → 用启发式补齐到 N
    if len(plans) < n:
        fillers = split_storyboard(
            prompt,
            shot_count=n - len(plans),
            duration_sec=duration_sec,
        )
        for j, filler in enumerate(fillers, start=len(plans) + 1):
            plans.append(
                ShotPlan(
                    index=j,
                    visual_prompt=filler.visual_prompt,
                    narration=filler.narration,
                    duration_sec=int(duration_sec),
                )
            )
    # 重排 index 到 1..N
    for i, p in enumerate(plans, start=1):
        p.index = i
    if on_log:
        on_log(f"llm.split ok shots={len(plans)}")
    return plans[:n]


# ── Shot 数据结构（生成后的真实状态） ────────────────────────────


@dataclass
class Shot:
    plan: ShotPlan
    image_url: str = ""
    image_path: Path | None = None
    video_url: str = ""
    video_path: Path | None = None
    audio_path: Path | None = None
    video_duration_sec: float = 0.0
    audio_duration_sec: float = 0.0
    error: str = ""
    status: str = "queued"
    stage: str = "planned"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "index": self.plan.index,
            "visual_prompt": self.plan.visual_prompt,
            "narration": self.plan.narration,
            "duration_sec": self.plan.duration_sec,
            "image_url": self.image_url,
            "image_path": str(self.image_path) if self.image_path else "",
            "video_url": self.video_url,
            "video_path": str(self.video_path) if self.video_path else "",
            "audio_path": str(self.audio_path) if self.audio_path else "",
            "video_duration_sec": self.video_duration_sec,
            "audio_duration_sec": self.audio_duration_sec,
            "error": self.error,
            "status": self.status,
            "stage": self.stage,
        }
        return d


# ── ffmpeg 探测 + 拼接 + 混音 ──────────────────────────────────


def ffmpeg_available() -> str | None:
    """返回 ffmpeg 可执行文件绝对路径，没有就 None。"""
    return shutil.which("ffmpeg")


def ffprobe_available() -> str | None:
    return shutil.which("ffprobe")


def probe_duration(path: Path, *, ffprobe_bin: str | None = None) -> float:
    """ffprobe 取时长（秒），失败返回 0。"""
    bin_ = ffprobe_bin or ffprobe_available()
    if not bin_ or not path or not Path(path).exists():
        return 0.0
    try:
        out = subprocess.run(
            [bin_, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def concat_with_audio(
    clips: list[Path],
    audios: list[Path],
    *,
    output: Path,
    shot_durations: list[float] | None = None,
    ffmpeg_bin: str | None = None,
) -> Path:
    """把所有镜头按顺序拼起来，并把每段配音按 offset 混进时间轴。

    clips / audios 同长度；shot_durations 给的是「这个镜头在最终片长里占几秒」，
    缺省用每个 clip 实际 ffprobe 时长。
    """
    bin_ = ffmpeg_bin or ffmpeg_available()
    if not bin_:
        raise RuntimeError("ffmpeg 不可用")

    n = len(clips)
    assert len(audios) == n, "clips / audios 数量必须一致"

    # 先用 input 把每个 clip 拉到统一时长（避免 concat 因 fps 不一致炸）
    # 然后 concat 所有 normalized clip；audio 单独 pad + delay + amix
    seg_lens: list[float] = []
    for i, clip in enumerate(clips):
        d = (shot_durations[i] if shot_durations and shot_durations[i] else 0.0)
        if d <= 0:
            d = probe_duration(clip)
        seg_lens.append(max(d, 0.1))

    # 第一段 offset = 0ms, 之后累加
    offsets_ms: list[int] = []
    cursor = 0
    for d in seg_lens[:-1]:
        offsets_ms.append(cursor)
        cursor += int(round(d * 1000))
    offsets_ms.append(cursor)  # 最后一段不算

    inputs: list[str] = []
    for c in clips:
        inputs.extend(["-i", str(c)])
    for a in audios:
        inputs.extend(["-i", str(a)])

    fc_parts: list[str] = []
    # 先 normalize 每个视频流到 yuv420p / 统一 fps，避免 concat 失败
    # concat filter 本身要求各输入同分辨率 / 像素格式，所以这里先 scale+pad+setpts
    norm_labels: list[str] = []
    for i in range(n):
        lbl = f"v{i}"
        fc_parts.append(
            f"[{i}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2:force_original_aspect_ratio=decrease,"
            f"pad=iw:ih:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps=24,format=yuv420p[{lbl}]"
        )
        norm_labels.append(lbl)

    concat_inputs = "".join(f"[{l}]" for l in norm_labels)
    fc_parts.append(
        f"{concat_inputs}concat=n={n}:v=1:a=0[cv]"
    )

    # audio: pad 到对应镜头时长，adelay 偏移，amix
    audio_labels: list[str] = []
    for i in range(n):
        lbl = f"a{i}"
        seg_ms = int(round(seg_lens[i] * 1000))
        fc_parts.append(
            f"[{n + i}:a]apad=whole_dur={seg_ms}ms[a{i}_p];"
            f"[a{i}_p]adelay={offsets_ms[i]}|all=1[{lbl}]"
        )
        audio_labels.append(lbl)
    mix_inputs = "".join(f"[{l}]" for l in audio_labels)
    fc_parts.append(
        f"{mix_inputs}amix=inputs={n}:duration=first:dropout_transition=0[a]"
    )

    cmd: list[str] = [
        bin_, "-y",
        *inputs,
        "-filter_complex", ";".join(fc_parts),
        "-map", "[cv]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ]
    print("[ffmpeg] " + " ".join(cmd[:8]) + f" ... → {output.name}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:] + "\n")
        raise RuntimeError(f"ffmpeg 拼接失败 (code={proc.returncode})")
    return output


# ── TTS 客户端 ─────────────────────────────────────────────────


class TTSError(RuntimeError):
    pass


class TTSClient:
    """Microsoft Edge TTS。"""

    def __init__(
        self,
        timeout: int = TTS_TIMEOUT,
        voice: str = TTS_DEFAULT_VOICE,
    ) -> None:
        self.timeout = timeout
        self.voice = voice

    def list_voices(self) -> list[dict[str, Any]]:
        """获取 Edge TTS 的可用语音，失败时返回空列表。"""
        try:
            import edge_tts

            return asyncio.run(edge_tts.list_voices())
        except Exception as e:
            sys.stderr.write(f"[TTS] list_voices failed: {e}\n")
            return []

    def synthesize_to_file(
        self, text: str, output: Path, *, voice: str | None = None
    ) -> Path:
        text = (text or "").strip()
        if not text:
            raise TTSError("text 不能为空")
        chosen = (voice or self.voice or TTS_DEFAULT_VOICE).strip()
        try:
            import edge_tts

            output.parent.mkdir(parents=True, exist_ok=True)
            asyncio.run(edge_tts.Communicate(text, chosen).save(str(output)))
        except Exception as e:
            raise TTSError(f"Edge TTS 失败: {e}") from e
        return output


def summarize_tts_error(payload: Any) -> str:
    """归一化 TTS 错误给前端用户看。"""
    blob = str(payload)
    lower = blob.lower()
    if any(s in lower for s in ("timed out", "timeout")):
        return "TTS 服务超时，请稍后重试。"
    if any(s in lower for s in ("network", "urlerror", "connection")):
        return "TTS 网络异常，请稍后重试。"
    if "invalid_request" in lower or "text" in lower and "required" in lower:
        return "TTS 文案为空或非法。"
    return "TTS 合成失败，请稍后重试。"


# ── 流水线主体 ─────────────────────────────────────────────────


ProgressFn = Callable[[float, str], None]
StateFn = Callable[[list[dict[str, Any]], str], None]


@dataclass
class VideoOptions:
    """入参归一化。"""

    shot_count: int | None = None
    duration_sec: int = DEFAULT_SHOT_DURATION
    voice: str = DEFAULT_VOICE
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    image_model: str = DEFAULT_IMAGE_MODEL
    image_model_version: str = DEFAULT_IMAGE_MODEL_VERSION
    video_model: str = DEFAULT_VIDEO_MODEL
    video_model_version: str = DEFAULT_VIDEO_MODEL_VERSION
    generate_audio: bool = True  # 视频自带的音轨；和 TTS 配音是两套
    use_llm: bool = False        # True → 用 LLM 拆 storyboard（opt-in）
    llm_model: str = ""          # 覆盖 LLM 拆镜模型（空 = env LLM_MODEL）
    max_wait: float = 1800.0
    poll_interval: float = 6.0


def _safe_run[T](fn: Callable[[], T], *, label: str, default: T) -> T:
    try:
        return fn()
    except Exception as e:
        sys.stderr.write(f"[{label}] {type(e).__name__}: {e}\n")
        return default


def generate_shot_image(
    plan: ShotPlan, options: VideoOptions, *, work: Path
) -> tuple[str, Path | None]:
    """调 fp.generate_image，下载到 work/<name>.<ext>，返回 (url, path)。"""
    work.mkdir(parents=True, exist_ok=True)
    out = fp.generate_image(
        plan.visual_prompt,
        model=options.image_model,
        model_version=options.image_model_version,
        n=1,
        size=DEFAULT_KEYFRAME_SIZE,
        seeds=None,
        detail_level=3,
        poll_interval=options.poll_interval,
        max_wait=options.max_wait,
        download_dir=work,
    )
    outputs = out.get("outputs") or []
    for o in outputs:
        url = o.get("url")
        if not url:
            continue
        ext = o.get("ext") or fp.guess_ext(url, ".jpg")
        local = o.get("local_path")
        return url, (Path(local) if local else work / f"image{ext}")
    return "", None


def generate_shot_video(
    plan: ShotPlan,
    options: VideoOptions,
    *,
    work: Path,
) -> tuple[str, Path | None]:
    """调 fp.generate_video，下载到 work/<name>.<ext>。
    当前上游接口对 image-to-video 走 reference_blobs（要先上传图片拿 blob id），
    为了不让这条路径爆炸，v1 直接用同一段 visual_prompt 跑 text-to-video；
    关键帧图仍然保留下来（poster / 后续切换）。
    """
    work.mkdir(parents=True, exist_ok=True)
    aspect = options.aspect_ratio or DEFAULT_ASPECT_RATIO
    size = dict(
        fp.VIDEO_SIZE_BY_ASPECT.get(aspect) or fp.VIDEO_SIZE_BY_ASPECT["16:9"]
    )
    out = fp.generate_video(
        plan.visual_prompt,
        model=options.video_model,
        model_version=options.video_model_version,
        n=1,
        seeds=None,
        duration=options.duration_sec,
        size=size,
        aspect_ratio=aspect,
        generate_audio=options.generate_audio,
        negative_prompt="",
        poll_interval=options.poll_interval,
        max_wait=options.max_wait,
        download_dir=work,
    )
    outputs = out.get("outputs") or []
    for o in outputs:
        url = o.get("url")
        if not url:
            continue
        ext = o.get("ext") or fp.guess_ext(url, ".mp4")
        local = o.get("local_path")
        return url, (Path(local) if local else work / f"video{ext}")
    return "", None


def generate_shot_tts(
    plan: ShotPlan,
    *,
    client: TTSClient,
    work: Path,
) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    out_path = work / "audio.mp3"
    client.synthesize_to_file(plan.narration, out_path)
    return out_path


def _media_duration_sec(path: Path | None) -> float:
    if not path:
        return 0.0
    return probe_duration(path)


def generate_full_video(
    prompt: str,
    options_dict: dict[str, Any] | None = None,
    *,
    on_progress: ProgressFn | None = None,
    on_state: StateFn | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    """主入口。返回：
      {
        final_video_path: str | "",
        manifest_path: str (no-ffmpeg 模式),
        shots: [Shot.to_dict() ...],
        tts_segments: [audio_path, ...],
        errors: [str, ...],
        used_ffmpeg: bool,
        ffprobe_duration_total: float,
      }
    """
    options_dict = options_dict or {}
    options = VideoOptions(
        shot_count=options_dict.get("shot_count"),
        duration_sec=int(options_dict.get("duration_sec") or DEFAULT_SHOT_DURATION),
        voice=str(options_dict.get("voice") or DEFAULT_VOICE),
        aspect_ratio=str(options_dict.get("aspect_ratio") or DEFAULT_ASPECT_RATIO),
        image_model=str(options_dict.get("image_model") or DEFAULT_IMAGE_MODEL),
        image_model_version=str(
            options_dict.get("image_model_version") or DEFAULT_IMAGE_MODEL_VERSION
        ),
        video_model=str(options_dict.get("video_model") or DEFAULT_VIDEO_MODEL),
        video_model_version=str(
            options_dict.get("video_model_version") or DEFAULT_VIDEO_MODEL_VERSION
        ),
        generate_audio=bool(options_dict.get("generate_audio", True)),
        use_llm=bool(options_dict.get("use_llm", False)),
        llm_model=str(options_dict.get("llm_model") or ""),
        max_wait=float(options_dict.get("max_wait") or 1800.0),
        poll_interval=float(options_dict.get("poll_interval") or 6.0),
    )

    n_target = max(
        MIN_SHOTS,
        min(int(options.shot_count or _length_based_shot_count(prompt)), MAX_SHOTS),
    )
    plans: list[ShotPlan] = []
    if options.use_llm:
        llm_cfg = _load_llm_config()
        if options.llm_model:
            llm_cfg.model = options.llm_model
        try:
            plans = split_storyboard_llm(
                prompt,
                shot_count=n_target,
                duration_sec=options.duration_sec,
                llm_config=llm_cfg,
                on_log=lambda m: sys.stderr.write(f"[video_pipeline] {m}\n"),
            )
        except Exception as e:
            sys.stderr.write(f"[video_pipeline] LLM split failed: {type(e).__name__}: {e}\n")
            plans = []
    if not plans:
        plans = split_storyboard(
            prompt,
            shot_count=options.shot_count or n_target,
            duration_sec=options.duration_sec,
        )
    if not plans:
        raise RuntimeError("prompt 不能为空")

    job_root = (VIDEO_DIR / job_id) if job_id else (VIDEO_DIR / _stamp())
    tts_root = TTS_DIR / job_id if job_id else (TTS_DIR / _stamp())
    job_root.mkdir(parents=True, exist_ok=True)
    tts_root.mkdir(parents=True, exist_ok=True)

    tts = TTSClient(voice=options.voice)
    shots = [Shot(plan=plan) for plan in plans]
    errors: list[str] = []

    def publish(message: str) -> None:
        if on_state:
            on_state([shot.to_dict() for shot in shots], message)

    publish(f"已规划 {len(shots)} 个分镜")

    n = len(plans)
    base = 5.0
    span = 80.0  # 5 → 85 给镜头生成
    for i, shot in enumerate(shots, start=1):
        plan = shot.plan
        shot_work = job_root / f"shot_{i:02d}"
        shot.status, shot.stage = "running", "keyframe"
        publish(f"分镜 {i}/{n}：出关键帧")
        if on_progress:
            on_progress(base + span * (i - 1) / n, f"分镜 {i}/{n}：出关键帧")
        # 1) image
        try:
            shot.image_url, shot.image_path = generate_shot_image(plan, options, work=shot_work)
        except Exception as e:
            shot.error += f"image:{type(e).__name__}:{e}; "
            errors.append(f"shot{i}.image: {e}")
        shot.stage = "video"
        publish(f"分镜 {i}/{n}：生成视频")
        # 2) video
        try:
            shot.video_url, shot.video_path = generate_shot_video(plan, options, work=shot_work)
        except Exception as e:
            shot.error += f"video:{type(e).__name__}:{e}; "
            errors.append(f"shot{i}.video: {e}")
        shot.stage = "tts"
        publish(f"分镜 {i}/{n}：合成配音")
        # 3) tts
        try:
            shot.audio_path = generate_shot_tts(plan, client=tts, work=tts_root / f"shot_{i:02d}")
        except Exception as e:
            shot.error += f"tts:{type(e).__name__}:{e}; "
            errors.append(f"shot{i}.tts: {e}")
        # 时长探测
        shot.video_duration_sec = _media_duration_sec(shot.video_path)
        shot.audio_duration_sec = _media_duration_sec(shot.audio_path)
        shot.status = "failed" if shot.error and not shot.video_path else "succeeded"
        shot.stage = "done"
        publish(f"分镜 {i}/{n}：{'完成' if shot.status == 'succeeded' else '部分失败'}")

    # ── 拼接 + 混音 ──
    final_path = job_root / "final.mp4"
    manifest_path = job_root / "manifest.json"
    used_ffmpeg = False

    clips_ok = [s.video_path for s in shots if s.video_path]
    audio_ok = [s.audio_path for s in shots if s.video_path and s.audio_path]
    shot_durs = [s.video_duration_sec for s in shots if s.video_path]

    if not clips_ok:
        # 一个镜头都没成功：写 manifest 让前端至少能看见进度
        _write_manifest(manifest_path, prompt, options, plans, shots, final_path=None)
        return {
            "final_video_path": "",
            "manifest_path": str(manifest_path),
            "shots": [s.to_dict() for s in shots],
            "tts_segments": [str(s.audio_path) for s in shots if s.audio_path],
            "errors": errors,
            "used_ffmpeg": False,
            "ffprobe_duration_total": 0.0,
        }

    bin_ = ffmpeg_available()
    if not bin_:
        sys.stderr.write(
            "[video_pipeline] ffmpeg 不在 PATH；只写 manifest，最终视频不生成。\n"
        )
        _write_manifest(manifest_path, prompt, options, plans, shots, final_path=None)
        return {
            "final_video_path": "",
            "manifest_path": str(manifest_path),
            "shots": [s.to_dict() for s in shots],
            "tts_segments": [str(s.audio_path) for s in shots if s.audio_path],
            "errors": errors + ["ffmpeg 不可用，仅生成 manifest"],
            "used_ffmpeg": False,
            "ffprobe_duration_total": 0.0,
        }

    if on_progress:
        on_progress(90.0, "拼接 + 混音中…")
    publish("所有分镜完成，正在拼接 + 混音")
    try:
        concat_with_audio(
            clips_ok, audio_ok,
            output=final_path,
            shot_durations=shot_durs,
            ffmpeg_bin=bin_,
        )
        used_ffmpeg = True
    except Exception as e:
        errors.append(f"concat: {e}")
        sys.stderr.write(f"[video_pipeline] concat failed: {e}\n")

    total_dur = _media_duration_sec(final_path) if used_ffmpeg else 0.0
    _write_manifest(manifest_path, prompt, options, plans, shots, final_path=final_path if used_ffmpeg else None)

    if on_progress:
        on_progress(100.0, "完成")

    return {
        "final_video_path": str(final_path) if used_ffmpeg else "",
        "manifest_path": str(manifest_path),
        "shots": [s.to_dict() for s in shots],
        "tts_segments": [str(s.audio_path) for s in shots if s.audio_path],
        "errors": errors,
        "used_ffmpeg": used_ffmpeg,
        "ffprobe_duration_total": total_dur,
    }


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _write_manifest(
    path: Path,
    prompt: str,
    options: VideoOptions,
    plans: list[ShotPlan],
    shots: list[Shot],
    *,
    final_path: Path | None,
) -> None:
    payload = {
        "prompt": prompt,
        "options": asdict(options),
        "storyboard": [asdict(p) for p in plans],
        "shots": [s.to_dict() for s in shots],
        "final_video_path": str(final_path) if final_path else "",
        "created_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ── CLI smoke ──────────────────────────────────────────────────


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="storyboard 拆分 + 流水线 CLI")
    ap.add_argument("prompt", nargs="?", default="一段清晨森林里的小鹿走入薄雾，远处有鹿群奔过，最后太阳升起照亮山谷。")
    ap.add_argument("--shots", type=int, default=None)
    ap.add_argument("--duration", type=int, default=DEFAULT_SHOT_DURATION)
    args = ap.parse_args()

    plans = split_storyboard(args.prompt, shot_count=args.shots, duration_sec=args.duration)
    print(f"[storyboard] shots={len(plans)}")
    for p in plans:
        print(f"  #{p.index} ({p.duration_sec}s) visual={p.visual_prompt!r}")
        print(f"          narration={p.narration!r}")


if __name__ == "__main__":
    _cli()
