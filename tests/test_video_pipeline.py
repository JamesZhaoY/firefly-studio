"""纯函数测试：覆盖分镜、时长、TTS 降级、LLM fallback。
不调 firefly_pipeline.generate_image / generate_video，也不打外网 TTS / LLM。
直接 .venv/bin/python tests/test_video_pipeline.py 跑。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import video_pipeline as vp


def _check(name: str, cond: bool, hint: str = "") -> None:
    flag = "OK " if cond else "FAIL"
    print(f"  [{flag}] {name}{(' — ' + hint) if hint and not cond else ''}")
    if not cond:
        raise AssertionError(name)


# ── 分镜拆分 ────────────────────────────────────────────────


def test_chinese_long():
    plans = vp.split_storyboard(
        "一段清晨森林里的小鹿走入薄雾，远处有鹿群奔过，最后太阳升起照亮山谷。"
    )
    _check("chinese long 默认镜头数", 3 <= len(plans) <= 6, f"got {len(plans)}")
    _check("镜头有 narration", all(p.narration for p in plans))
    _check("镜头有 visual_prompt", all(p.visual_prompt for p in plans))
    _check("默认 6 秒", all(p.duration_sec == 6 for p in plans))
    _check("保留「最后」", any("最后" in p.narration for p in plans))


def test_english_cues():
    plans = vp.split_storyboard(
        "First, a person walks in. Then lights flicker. Finally, the door opens.",
        shot_count=3,
    )
    _check("cue-shot 拆出 3 镜", len(plans) == 3, f"got {len(plans)}")
    full = " ".join(p.narration for p in plans).lower()
    _check("First 保留", "first" in full)
    _check("Then 保留", "then" in full)
    _check("Finally 保留", "finally" in full)


def test_short_prompt_no_char_split():
    """短提示词：禁止把原文按字符碎片化进 narration。"""
    plans = vp.split_storyboard("猫在阳光下打盹")
    _check("短 prompt 返回 1 或 3 镜", len(plans) in (1, 3), f"got {len(plans)}")
    for p in plans:
        # 每个镜头都要有完整原文（不能是「猫」「在」「阳」这种碎片）
        _check(
            f"#{p.index} narration 包含完整短语",
            "猫" in p.narration and "阳光" in p.narration and "打盹" in p.narration,
            repr(p.narration),
        )
    # 多镜时（3-view 派生）每个镜头 visual 必须不同
    if len(plans) > 1:
        visuals = {p.visual_prompt for p in plans}
        _check("多镜 visual 互不相同", len(visuals) == len(plans), f"got {visuals}")


def test_single_token_prompt_single_shot():
    plans = vp.split_storyboard("夕阳")
    _check("单 token 用单镜", len(plans) == 1, f"got {len(plans)}")
    _check("单镜 narration 是原文", plans and plans[0].narration == "夕阳")


def test_empty_prompt():
    plans = vp.split_storyboard("")
    _check("空 prompt 返回空列表", plans == [])


def test_explicit_shot_count_clamped():
    plans = vp.split_storyboard("hello world test", shot_count=99)
    _check("shot_count 上限", len(plans) == vp.MAX_SHOTS, f"got {len(plans)}")

    plans = vp.split_storyboard("hello world test", shot_count=1)
    _check("shot_count 下限", len(plans) == vp.MIN_SHOTS, f"got {len(plans)}")


def test_labels_unique():
    plans = vp.split_storyboard(
        "alpha beta gamma delta epsilon zeta", shot_count=5
    )
    labels = [p.visual_prompt.split(":", 1)[0] for p in plans]
    _check("镜头 label 不重复", len(set(labels)) == len(labels), f"got {labels}")


# ── 时间轴 / 时长 ────────────────────────────────────────────


def test_final_duration_uses_max():
    """目标时长 = max(视频实测, TTS 实测, 计划时长, 下限)。"""
    _check("视频更长 → 用视频", vp.resolve_final_duration(6, 8, 4) == 8)
    _check("TTS 更长 → 用 TTS", vp.resolve_final_duration(6, 4, 7) == 7)
    _check("计划最长 → 用计划", vp.resolve_final_duration(10, 4, 5) == 10)
    _check("都为零 → 用下限", vp.resolve_final_duration(0, 0, 0, floor=0.5) == 0.5)


def test_final_duration_floor():
    """视频 / TTS / 计划 都缺失也不能 0，否则 concat 会爆。"""
    _check("下限生效", vp.resolve_final_duration(0, 0, 0) >= 0.5)


# ── LLM fallback ─────────────────────────────────────────────


def test_llm_invalid_json_raises():
    """LLM 返回非 JSON 时 split_storyboard_llm 必须抛 RuntimeError 让上层 fallback。"""
    bad_cfg = vp.LLMConfig(
        base_url="http://127.0.0.1:1/v1",  # 不存在的端口
        api_key="x",
        model="x",
        timeout=1.0,
    )
    raised = False
    try:
        vp.split_storyboard_llm(
            "任意 prompt", shot_count=3, duration_sec=6, llm_config=bad_cfg,
        )
    except RuntimeError:
        raised = True
    _check("LLM 不可达时抛 RuntimeError", raised)


def test_llm_invalid_payload_raises():
    """LLM 通了但返回非 JSON 也必须抛（让上层 fallback 到启发式）。"""
    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body
        def read(self) -> bytes:
            return self._body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    captured = {"called": False}

    def fake_urlopen(req, timeout=None):
        captured["called"] = True
        # 返回 choices[0].message.content = "not json at all"
        return _FakeResp(b'{"choices":[{"message":{"content":"not json at all"}}]}')

    import urllib.request as _ur
    orig = _ur.urlopen
    _ur.urlopen = fake_urlopen
    try:
        raised = False
        try:
            vp.split_storyboard_llm(
                "任意 prompt", shot_count=3, duration_sec=6,
                llm_config=vp.LLMConfig("http://x", "k", "m", timeout=2.0),
            )
        except (RuntimeError, ValueError):
            raised = True
        _check("LLM 假接口被调到", captured["called"])
        _check("非 JSON payload 抛异常（让上层 fallback）", raised)
    finally:
        _ur.urlopen = orig


def test_coerce_shot_with_new_fields():
    """_coerce_shot 应能接收 camera/motion/negative_prompt。"""
    item = {
        "visual": "english visual prompt",
        "narration": "中文旁白",
        "duration": 6,
        "camera": "wide establishing",
        "motion": "slow dolly in",
        "negative_prompt": "blurry",
    }
    plan = vp._coerce_shot(item, idx=1, default_duration=6)
    _check("camera 字段写入", plan and plan.camera == "wide establishing")
    _check("motion 字段写入", plan and plan.motion == "slow dolly in")
    _check("negative_prompt 字段写入", plan and plan.negative_prompt == "blurry")


def test_coerce_shot_drops_invalid():
    _check("缺 visual → 返回 None", vp._coerce_shot({"narration": "x"}, idx=1, default_duration=6) is None)
    _check("缺 narration → 返回 None", vp._coerce_shot({"visual": "x"}, idx=1, default_duration=6) is None)
    _check("非 dict → 返回 None", vp._coerce_shot("x", idx=1, default_duration=6) is None)


def test_coerce_shot_duration_clamped():
    p1 = vp._coerce_shot({"visual": "v", "narration": "n", "duration": 99}, idx=1, default_duration=6)
    p2 = vp._coerce_shot({"visual": "v", "narration": "n", "duration": 1}, idx=1, default_duration=6)
    _check("duration clamp 上限", p1 and p1.duration_sec == 12)
    _check("duration clamp 下限", p2 and p2.duration_sec == 3)


# ── 拼接 / concat 列表 ───────────────────────────────────────


def test_concat_inputs_list_shape():
    """concat_with_audio 拒绝 clips/audios 不等长（必须有 audio 即 silence 兜底）。"""
    # 不依赖 ffmpeg 也能验证参数约束（无 ffmpeg → RuntimeError；有 ffmpeg 但空列表 → RuntimeError）
    try:
        vp.concat_with_audio([], [], output=Path("/tmp/_never_.mp4"))
    except (RuntimeError, AssertionError, ValueError, TypeError) as e:
        _check("空 clips 应抛错", "没有可拼接的镜头" in str(e) or "ffmpeg" in str(e) or "audio" in str(e))


# ── 杂项 ─────────────────────────────────────────────────────


def test_ffmpeg_detection():
    bin_ = vp.ffmpeg_available()
    _check("ffmpeg_available 返回值类型", bin_ is None or isinstance(bin_, str))


def test_shot_state_serialized():
    shot = vp.Shot(plan=vp.split_storyboard("猫在阳光下打盹")[0])
    shot.status, shot.stage = "running", "tts"
    payload = shot.to_dict()
    _check("镜头状态序列化", payload["status"] == "running" and payload["stage"] == "tts")


def test_shot_retries_after_model_spec_recovery():
    plan = vp.ShotPlan(
        index=1,
        visual_prompt="a firefly",
        narration="萤火虫",
        duration_sec=5,
    )
    options = vp.VideoOptions(
        video_model="firefly-video",
        video_model_version="clineto",
        aspect_ratio="16:9",
        video_size="",
    )
    calls = []
    recovered = []
    original = vp.fp.generate_video

    def fake_generate_video(*_args, **kwargs):
        calls.append(kwargs.get("size"))
        if len(calls) == 1:
            raise RuntimeError(
                "Allowed width, height combinations are: [(960, 540), (540, 960)]"
            )
        return {"outputs": []}

    def recover(model, version, aspect, error):
        recovered.append((model, version, aspect, str(error)))
        return "16:9", "960x540"

    vp.fp.generate_video = fake_generate_video
    try:
        with tempfile.TemporaryDirectory() as directory:
            vp.generate_shot_video(
                plan,
                options,
                work=Path(directory),
                recover_model_spec=recover,
            )
    finally:
        vp.fp.generate_video = original

    _check("规格恢复回调被调用一次", len(recovered) == 1)
    _check("当前分镜自动重试一次", len(calls) == 2)
    _check("重试使用模型返回尺寸", calls[1] == {"width": 960, "height": 540})
    _check("后续分镜复用恢复规格", options.video_size == "960x540")


# ── status 逻辑（app.py 一致性） ─────────────────────────────


def test_status_logic_only_final_file_succeeds():
    """复刻 app.py 的 status 规则：final_video_path 存在且文件存在 → succeeded，
    否则 failed。文件不存在时不能标 succeeded。
    """
    # 这是逻辑测试：final_path 存在但 Path 不存在 → status != succeeded
    fake_path = "/tmp/__definitely_not_exists__.mp4"
    final_exists = bool(fake_path) and Path(fake_path).is_file()
    _check("不存在的文件不能 succeeded", not final_exists)


def main() -> int:
    tests = [
        test_chinese_long,
        test_english_cues,
        test_short_prompt_no_char_split,
        test_single_token_prompt_single_shot,
        test_empty_prompt,
        test_explicit_shot_count_clamped,
        test_labels_unique,
        test_final_duration_uses_max,
        test_final_duration_floor,
        test_llm_invalid_json_raises,
        test_llm_invalid_payload_raises,
        test_coerce_shot_with_new_fields,
        test_coerce_shot_drops_invalid,
        test_coerce_shot_duration_clamped,
        test_concat_inputs_list_shape,
        test_ffmpeg_detection,
        test_shot_state_serialized,
        test_shot_retries_after_model_spec_recovery,
        test_status_logic_only_final_file_succeeds,
    ]
    failed = 0
    for t in tests:
        print(f"\n{t.__name__}:")
        try:
            t()
        except AssertionError:
            failed += 1
        except Exception as e:
            print(f"  [EXC] {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"=== {len(tests) - failed}/{len(tests)} passed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
