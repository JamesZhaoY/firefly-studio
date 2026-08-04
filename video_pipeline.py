"""全自动视频生成流水线（文字 → 多镜头脚本 → 镜头视频 → 配音 → 合成）。

职责：
  1. 把一段自然语言描述拆成 N 个分镜（3-6 镜头，纯启发式，可选 LLM）
  2. 每个分镜：出镜头视频（video）→ 出配音（TTS，TTS 失败补静音）
  3. 按每个镜头的最终时长（视频 / TTS / 计划 取最大）归一化视频和音频
  4. ffmpeg 拼接 + 混音
  5. 落盘 + 把结果写回 SQLite jobs 表

外部依赖：
  - firefly_pipeline.generate_video （已有）
  - edge-tts（Microsoft Edge TTS，pip 依赖）
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
from dataclasses import asdict, dataclass
from pathlib import Path
import os
from typing import Any, Callable

import firefly_pipeline as fp

# ── 常量 ───────────────────────────────────────────────────────

APP_ROOT = Path(__file__).resolve().parent
OUT_DIR = APP_ROOT / "outputs"
VIDEO_DIR = OUT_DIR / "videos"
TTS_DIR = OUT_DIR / "tts"

TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_FALLBACK_VOICE = "en-US-JennyNeural"
TTS_TIMEOUT = 60

# 默认镜头数范围
MIN_SHOTS = 3
MAX_SHOTS = 6
DEFAULT_SHOT_DURATION = 6  # 秒

# 默认视频模型（让 firefly_pipeline 选预设）
DEFAULT_VIDEO_MODEL = ""
DEFAULT_VIDEO_MODEL_VERSION = ""
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_VOICE = TTS_DEFAULT_VOICE


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
    ≤ 40 字 → 3  短（一句话，三个镜头视角）
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


def _is_single_token(prompt: str) -> bool:
    """特别短的提示词：≤12 字或单句，用单镜即可。"""
    text = prompt.strip()
    if len(text) <= 12:
        return True
    sentences = [s for s in re.split(r"[。！？!?\.\n]+", text) if s.strip()]
    return len(sentences) <= 1 and len(text) <= 18


def _short_three_views(text: str, duration_sec: int) -> list[ShotPlan]:
    """短提示词派生 3 镜：建立 / 主体近景 / 细节。
    三个镜头共用同一段 narration（同一时刻的三个视角），避免把字符碎片化给 TTS。
    """
    angles = [
        ("establishing wide shot", "wide establishing"),
        ("close-up on the subject", "close-up subject"),
        ("detail close-up, shallow depth of field", "detail close-up"),
    ]
    per = max(int(duration_sec), 4)
    plans: list[ShotPlan] = []
    for i, (visual_suffix, camera) in enumerate(angles, start=1):
        plans.append(
            ShotPlan(
                index=i,
                visual_prompt=(
                    f"{visual_suffix}: {text}. high quality, stable composition, "
                    f"cinematic lighting, {camera}"
                ),
                narration=text,
                duration_sec=per,
                camera=camera,
            )
        )
    return plans


def _single_shot_plan(text: str, duration_sec: int) -> list[ShotPlan]:
    """单镜：直接把整段原文当 narration + visual。"""
    return [
        ShotPlan(
            index=1,
            visual_prompt=(
                f"single cinematic shot: {text}. high quality, stable composition, "
                f"cinematic lighting"
            ),
            narration=text,
            duration_sec=max(int(duration_sec), 4),
            camera="single",
        )
    ]


@dataclass
class ShotPlan:
    """一个分镜的预规划（还没真去生成）。"""

    index: int
    visual_prompt: str  # 给图 / 视频用的英文式 prompt
    narration: str      # 给 TTS 的旁白（直接用原文叙述 + 标记）
    duration_sec: int   # 这个镜头在最终片长里的目标时长
    camera: str = ""            # 镜头语言（如 "wide establishing", "close-up"）
    motion: str = ""            # 运动描述（如 "slow dolly in"）
    negative_prompt: str = ""   # 反向提示词


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
    3. 都不够 → 把整段原文当成「一段」放进最后一个镜头的 narration，
                  之前的镜头留视觉占位（避免字符碎片化进 TTS）
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

    # ponytail: 都不够时，按字符切会产生 "猫在 / 阳光 / 下打盹" 这种碎片旁白。
    # 改为：把整段原文集中在最后一个镜头的 narration，前面的镜头留空字符串
    # （视觉占位由 ShotPlan.camera/motion/visual_prompt 引导，不靠 narration）。
    blanks = [""] * (n - 1)
    return blanks + [text]


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
        if _is_single_token(text):
            return _single_shot_plan(text, duration_sec)
        explicit = _count_cues(text)
        if explicit >= MIN_SHOTS:
            n = min(explicit, MAX_SHOTS)
        elif len(text) <= 40:
            # 短提示词：走三视图派生，避免按字符切
            return _short_three_views(text, duration_sec)
        else:
            n = _length_based_shot_count(text)
    else:
        n = max(MIN_SHOTS, min(int(shot_count), MAX_SHOTS))

    segments = _split_into_segments(text, n)
    plans: list[ShotPlan] = []
    for i, seg in enumerate(segments, start=1):
        label = _shot_label(i, n)
        # visual_prompt: 用原片段描述 + 镜头序号引导，避免原样复制
        visual = f"{label} cinematic shot: {seg or text}. high quality, stable composition, cinematic lighting"
        plans.append(
            ShotPlan(
                index=i,
                visual_prompt=visual,
                narration=seg,  # 空白 narration → generate_shot_tts 会抛 TTSError，被 silence 兜底
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
    "(N 由用户指定,3-6)。每个分镜必须输出 JSON 对象,字段:\n"
    "- visual: 一行适合图像生成的视觉提示词(纯英文,无中文字符)\n"
    "- narration: 一行该镜头的旁白(中文,保留用户语言)\n"
    "- duration: 整数秒(3-12)\n"
    "- camera: 镜头语言(如 wide establishing / close-up / dolly in)\n"
    "- motion: 运动描述(如 slow dolly in / static / pan right)\n"
    "- negative_prompt: 反向提示词(英文,空字符串表示无)\n"
    "要求: 各镜头的 visual 必须有明显视觉变化(景别/角度/主体不同), "
    "禁止用同一段 prompt 改几个字。\n"
    "输出严格 JSON 数组,字段 visual/narration/duration/camera/motion/negative_prompt。"
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
    return ShotPlan(
        index=idx,
        visual_prompt=visual,
        narration=narration,
        duration_sec=dur,
        camera=str(item.get("camera") or "").strip(),
        motion=str(item.get("motion") or "").strip(),
        negative_prompt=str(item.get("negative_prompt") or "").strip(),
    )


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


def _ffmpeg_bin_or_raise(ffmpeg_bin: str | None) -> str:
    bin_ = ffmpeg_bin or ffmpeg_available()
    if not bin_:
        raise RuntimeError("ffmpeg 不可用")
    return bin_


def _extract_thumbnail(video: Path, out: Path, *, ffmpeg_bin: str | None = None) -> Path | None:
    """从视频抽第一帧为 jpg；失败返回 None（不影响主流程）。"""
    bin_ = _ffmpeg_bin_or_raise(ffmpeg_bin)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [bin_, "-y", "-i", str(video), "-vframes", "1", "-an", "-q:v", "3", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            sys.stderr.write(f"[video_pipeline] thumb extract failed: {proc.stderr[-500:]}\n")
            return None
        return out
    except Exception as e:
        sys.stderr.write(f"[video_pipeline] thumb extract exception: {e}\n")
        return None


def _silence_for_duration(dur_sec: float, out: Path, *, ffmpeg_bin: str | None = None) -> Path:
    """生成 dur_sec 秒的静音 mp3。"""
    bin_ = _ffmpeg_bin_or_raise(ffmpeg_bin)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = max(float(dur_sec), 0.1)
    cmd = [
        bin_, "-y",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", f"{dur}",
        "-c:a", "libmp3lame", "-b:a", "64k",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-1000:] + "\n")
        raise RuntimeError(f"生成静音失败 (code={proc.returncode})")
    return out


def _normalize_clip(clip: Path, target_dur: float, out: Path, *, ffmpeg_bin: str | None = None) -> Path:
    """视频归一化：
      - 视频短于 target_dur → 冻结最后一帧补齐（tpad clone）
      - 视频长于 target_dur → 裁剪到 target_dur（-t）
      - 分辨率取偶数像素，固定 fps=24，yuv420p

    实现：tpad 加 target_dur 秒的冻结尾帧，再由 `-t target_dur` 截到精确时长。
    这样无论输入比 target 短 / 长 / 相等，输出都恰好 target_dur 秒。
    """
    bin_ = _ffmpeg_bin_or_raise(ffmpeg_bin)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = max(float(target_dur), 0.1)
    vf = (
        "scale=trunc(iw/2)*2:trunc(ih/2)*2:force_original_aspect_ratio=decrease,"
        "pad=iw:ih:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        "fps=24,"
        f"tpad=stop_mode=clone:stop_duration={dur},"
        "format=yuv420p,trim=end={d},setpts=PTS-STARTPTS"
    ).format(d=dur)
    cmd = [
        bin_, "-y",
        "-i", str(clip),
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-t", f"{dur}",
        "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-1500:] + "\n")
        raise RuntimeError(f"视频归一化失败 (code={proc.returncode})")
    return out


def _normalize_audio(audio: Path, target_dur: float, out: Path, *, ffmpeg_bin: str | None = None) -> Path:
    """音频归一化：短了补静音（apad），长了裁剪（atrim），统一 44.1kHz 立体声。"""
    bin_ = _ffmpeg_bin_or_raise(ffmpeg_bin)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = max(float(target_dur), 0.1)
    seg_ms = int(round(dur * 1000))
    af = (
        "aresample=44100,"
        f"apad=whole_dur={seg_ms}ms,"
        f"atrim=end={dur},asetpts=PTS-STARTPTS"
    )
    cmd = [
        bin_, "-y",
        "-i", str(audio),
        "-af", af,
        "-c:a", "libmp3lame", "-b:a", "96k",
        "-t", f"{dur}",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-1500:] + "\n")
        raise RuntimeError(f"音频归一化失败 (code={proc.returncode})")
    return out


def concat_with_audio(
    clips: list[Path],
    audios: list[Path],
    *,
    output: Path,
    ffmpeg_bin: str | None = None,
) -> Path:
    """把所有镜头按顺序拼接并混音。

    约定：调用方已把 clips / audios 归一化到相同的目标时长；
    本函数不再 probe / pad / trim，只做 concat + amix。
    """
    bin_ = ffmpeg_bin or ffmpeg_available()
    if not bin_:
        raise RuntimeError("ffmpeg 不可用")

    n = len(clips)
    if n == 0:
        raise RuntimeError("没有可拼接的镜头")
    if len(audios) != n:
        # ponytail: 上层应保证每镜都有归一化音频（含 silence fallback）；
        # 漏一个就 raise，让调用方回退到 manifest 模式而不是伪装成功
        raise RuntimeError(f"audios 数 ({len(audios)}) 与 clips 数 ({n}) 不一致")

    inputs: list[str] = []
    for c in clips:
        inputs.extend(["-i", str(c)])
    for a in audios:
        inputs.extend(["-i", str(a)])

    fc_parts: list[str] = []
    # 视频：clip 已归一化（yuv420p / fps=24 / SAR=1），concat 仅做拼接
    v_labels = [f"v{i}" for i in range(n)]
    for i in range(n):
        fc_parts.append(f"[{i}:v]format=yuv420p[{v_labels[i]}]")
    concat_in = "".join(f"[{l}]" for l in v_labels)
    fc_parts.append(f"{concat_in}concat=n={n}:v=1:a=0[v]")

    # 音频：amix 所有归一化后的音轨（已 resample + 对齐到目标时长）
    a_labels = [f"a{i}" for i in range(n)]
    for i in range(n):
        fc_parts.append(f"[{n + i}:a]aresample=44100[{a_labels[i]}]")
    mix_in = "".join(f"[{l}]" for l in a_labels)
    fc_parts.append(f"{mix_in}amix=inputs={n}:duration=first:dropout_transition=0[a]")

    cmd: list[str] = [
        bin_, "-y",
        *inputs,
        "-filter_complex", ";".join(fc_parts),
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ]
    print(f"[ffmpeg] concat {n} clips → {output.name}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:] + "\n")
        raise RuntimeError(f"ffmpeg 拼接失败 (code={proc.returncode})")
    return output


# ── TTS 客户端 ─────────────────────────────────────────────────


class TTSError(RuntimeError):
    pass


class TTSClient:
    """Microsoft Edge TTS（通过 edge-tts Python 包）。"""

    def __init__(
        self,
        timeout: float = float(TTS_TIMEOUT),
        voice: str = TTS_DEFAULT_VOICE,
    ) -> None:
        self.timeout = float(timeout)
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
            communicate = edge_tts.Communicate(text, chosen)
            # edge-tts 没有原生 timeout；用 asyncio.wait_for 包一层
            try:
                asyncio.run(
                    asyncio.wait_for(communicate.save(str(output)), timeout=self.timeout)
                )
            except asyncio.TimeoutError as e:
                raise TTSError(
                    f"Edge TTS 超时（>{self.timeout:.0f}s），请稍后重试"
                ) from e
        except TTSError:
            raise
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
ModelSpecRecoveryFn = Callable[[str, str, str, Any], tuple[str, Any] | None]


@dataclass
class VideoOptions:
    """入参归一化。"""

    shot_count: int | None = None
    duration_sec: int = DEFAULT_SHOT_DURATION
    voice: str = DEFAULT_VOICE
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    video_model: str = DEFAULT_VIDEO_MODEL
    video_model_version: str = DEFAULT_VIDEO_MODEL_VERSION
    video_size: Any = None
    generate_audio: bool = True  # 视频自带的音轨；和 TTS 配音是两套
    use_llm: bool = False        # True → 用 LLM 拆 storyboard（opt-in）
    llm_model: str = ""          # 覆盖 LLM 拆镜模型（空 = env LLM_MODEL）
    max_wait: float = 1800.0
    poll_interval: float = 6.0


def generate_shot_video(
    plan: ShotPlan,
    options: VideoOptions,
    *,
    work: Path,
    recover_model_spec: ModelSpecRecoveryFn | None = None,
) -> tuple[str, Path | None]:
    """调 fp.generate_video（纯文生视频），下载到 work/<name>.<ext>。"""
    work.mkdir(parents=True, exist_ok=True)

    def run_upstream() -> dict[str, Any]:
        aspect = options.aspect_ratio or DEFAULT_ASPECT_RATIO
        size = fp.parse_size(options.video_size)
        if size is None:
            size = dict(
                fp.VIDEO_SIZE_BY_ASPECT.get(aspect) or fp.VIDEO_SIZE_BY_ASPECT["16:9"]
            )
        return fp.generate_video(
            plan.visual_prompt,
            model=options.video_model,
            model_version=options.video_model_version,
            n=1,
            seeds=None,
            duration=options.duration_sec,
            size=size,
            aspect_ratio=aspect,
            generate_audio=options.generate_audio,
            negative_prompt=plan.negative_prompt or "",
            poll_interval=options.poll_interval,
            max_wait=options.max_wait,
            download_dir=work,
        )

    try:
        out = run_upstream()
    except Exception as first_error:
        recovered = (
            recover_model_spec(
                options.video_model,
                options.video_model_version,
                options.aspect_ratio,
                first_error,
            )
            if recover_model_spec
            else None
        )
        if not recovered:
            raise
        options.aspect_ratio, options.video_size = recovered
        out = run_upstream()
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


def resolve_final_duration(
    plan_duration_sec: int | float,
    video_duration_sec: float,
    audio_duration_sec: float,
    *,
    floor: float = 0.5,
) -> float:
    """一个镜头在最终成片里的目标时长 = max(视频实测, TTS 实测, 计划, 下限)。
    抽出便于单测。
    """
    return max(
        float(video_duration_sec or 0),
        float(audio_duration_sec or 0),
        float(plan_duration_sec or 0),
        float(floor),
    )


def generate_full_video(
    prompt: str,
    options_dict: dict[str, Any] | None = None,
    *,
    on_progress: ProgressFn | None = None,
    on_state: StateFn | None = None,
    recover_model_spec: ModelSpecRecoveryFn | None = None,
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
        video_model=str(options_dict.get("video_model") or DEFAULT_VIDEO_MODEL),
        video_model_version=str(
            options_dict.get("video_model_version") or DEFAULT_VIDEO_MODEL_VERSION
        ),
        video_size=options_dict.get("video_size"),
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
    tmp_root = job_root / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)

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
        shot_work = tmp_root / f"shot_{i:02d}"
        shot_work.mkdir(parents=True, exist_ok=True)
        shot.status, shot.stage = "running", "video"
        publish(f"分镜 {i}/{n}：生成视频")
        if on_progress:
            on_progress(base + span * (i - 1) / n, f"分镜 {i}/{n}：生成视频")
        # 1) video（不做关键帧；缩略图后续从视频首帧抽）
        try:
            shot.video_url, shot.video_path = generate_shot_video(
                plan,
                options,
                work=shot_work,
                recover_model_spec=recover_model_spec,
            )
        except Exception as e:
            shot.error += f"video:{type(e).__name__}:{e}; "
            errors.append(f"shot{i}.video: {e}")
        shot.stage = "tts"
        publish(f"分镜 {i}/{n}：合成配音")
        # 2) tts（失败不中断，silence 兜底）
        try:
            shot.audio_path = generate_shot_tts(plan, client=tts, work=tts_root / f"shot_{i:02d}")
        except Exception as e:
            shot.error += f"tts:{type(e).__name__}:{e}; "
            errors.append(f"shot{i}.tts: {e}")
        # 时长探测
        shot.video_duration_sec = _media_duration_sec(shot.video_path)
        shot.audio_duration_sec = _media_duration_sec(shot.audio_path)
        # 缩略图：成功生成视频后从首帧抽；不调 image 模型
        if shot.video_path:
            thumb = shot_work / "thumb.jpg"
            if _extract_thumbnail(shot.video_path, thumb):
                shot.image_path = thumb
        shot.status = "failed" if shot.error and not shot.video_path else "succeeded"
        shot.stage = "done"
        publish(f"分镜 {i}/{n}：{'完成' if shot.status == 'succeeded' else '部分失败'}")

    # ── 拼接 + 混音 ──
    final_path = job_root / "final.mp4"
    manifest_path = job_root / "manifest.json"
    used_ffmpeg = False

    # 收集成功视频镜头 → 归一化到「最终时长」(max 视频 / TTS / 计划)
    final_clips: list[Path] = []
    final_audios: list[Path] = []
    final_durations: list[float] = []

    for shot in shots:
        if not shot.video_path:
            continue
        target = resolve_final_duration(
            shot.plan.duration_sec,
            shot.video_duration_sec,
            shot.audio_duration_sec,
        )
        idx = shot.plan.index
        clip_norm = tmp_root / f"shot_{idx:02d}" / "norm.mp4"
        try:
            _normalize_clip(shot.video_path, target, clip_norm)
        except Exception as e:
            errors.append(f"shot{idx}.normalize_clip: {e}")
            continue

        if shot.audio_path:
            audio_norm = tts_root / f"shot_{idx:02d}" / "norm.mp3"
            try:
                _normalize_audio(shot.audio_path, target, audio_norm)
                final_audio = audio_norm
            except Exception as e:
                errors.append(f"shot{idx}.normalize_audio: {e}")
                final_audio = None
        else:
            final_audio = None

        if final_audio is None:
            silence = tts_root / f"shot_{idx:02d}" / "silence.mp3"
            try:
                _silence_for_duration(target, silence)
                final_audio = silence
            except Exception as e:
                errors.append(f"shot{idx}.silence: {e}")
                continue

        final_clips.append(clip_norm)
        final_audios.append(final_audio)
        final_durations.append(target)
        shot.stage = "concatenated"

    if not final_clips:
        # 一个镜头都没法合成：写 manifest 让前端至少能看见进度
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
        concat_with_audio(final_clips, final_audios, output=final_path, ffmpeg_bin=bin_)
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
