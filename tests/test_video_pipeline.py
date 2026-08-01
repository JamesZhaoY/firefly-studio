"""纯函数测试：只覆盖 split_storyboard / _split_into_segments / ShotPlan 元数据。
不调 firefly_pipeline.generate_image / generate_video，也不打外网 TTS。
直接 python tests/test_video_pipeline.py 跑。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import video_pipeline as vp


def _check(name: str, cond: bool, hint: str = "") -> None:
    flag = "OK " if cond else "FAIL"
    print(f"  [{flag}] {name}{(' — ' + hint) if hint and not cond else ''}")
    if not cond:
        raise AssertionError(name)


def test_chinese_long():
    plans = vp.split_storyboard(
        "一段清晨森林里的小鹿走入薄雾，远处有鹿群奔过，最后太阳升起照亮山谷。"
    )
    _check("chinese long 默认镜头数", 3 <= len(plans) <= 6, f"got {len(plans)}")
    _check("镜头有 narration", all(p.narration for p in plans))
    _check("镜头有 visual_prompt", all(p.visual_prompt for p in plans))
    _check("默认 6 秒", all(p.duration_sec == 6 for p in plans))
    # 显式提示（最后）应保留
    _check("保留「最后」", any("最后" in p.narration for p in plans))


def test_english_cues():
    plans = vp.split_storyboard(
        "First, a person walks in. Then lights flicker. Finally, the door opens.",
        shot_count=3,
    )
    _check("cue-shot 拆出 3 镜", len(plans) == 3, f"got {len(plans)}")
    # 每个 cue 都出现在 narration 里
    full = " ".join(p.narration for p in plans).lower()
    _check("First 保留", "first" in full)
    _check("Then 保留", "then" in full)
    _check("Finally 保留", "finally" in full)


def test_short_prompt():
    plans = vp.split_storyboard("猫在阳光下打盹")
    _check("短 prompt 至少 3 镜", len(plans) >= 3, f"got {len(plans)}")
    _check("短 prompt 不超过 6 镜", len(plans) <= 6)


def test_empty_prompt():
    plans = vp.split_storyboard("")
    _check("空 prompt 返回空列表", plans == [])


def test_explicit_shot_count_clamped():
    plans = vp.split_storyboard("hello world test", shot_count=99)
    _check("shot_count 上限", len(plans) == vp.MAX_SHOTS, f"got {len(plans)}")

    plans = vp.split_storyboard("hello world test", shot_count=1)
    _check("shot_count 下限", len(plans) == vp.MIN_SHOTS, f"got {len(plans)}")


def test_labels_unique():
    plans = vp.split_storyboard("alpha beta gamma delta epsilon zeta", shot_count=5)
    labels = [p.visual_prompt.split(":", 1)[0] for p in plans]
    _check("镜头 label 不重复", len(set(labels)) == len(labels), f"got {labels}")


def test_ffmpeg_detection():
    # ponytail: 不强制环境有 ffmpeg；只要函数返回 str 或 None 即可
    bin_ = vp.ffmpeg_available()
    _check("ffmpeg_available 返回值类型", bin_ is None or isinstance(bin_, str))


def test_shot_state_serialized():
    shot = vp.Shot(plan=vp.split_storyboard("猫在阳光下打盹")[0])
    shot.status, shot.stage = "running", "keyframe"
    payload = shot.to_dict()
    _check("镜头状态序列化", payload["status"] == "running" and payload["stage"] == "keyframe")


def main() -> int:
    tests = [
        test_chinese_long,
        test_english_cues,
        test_short_prompt,
        test_empty_prompt,
        test_explicit_shot_count_clamped,
        test_labels_unique,
        test_ffmpeg_detection,
        test_shot_state_serialized,
    ]
    failed = 0
    for t in tests:
        print(f"\n{t.__name__}:")
        try:
            t()
        except AssertionError:
            failed += 1
    print()
    print(f"=== {len(tests) - failed}/{len(tests)} passed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
