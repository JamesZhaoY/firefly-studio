"""模型预设 + 上游 discovery 展开。"""

from __future__ import annotations

import json
import math
import re
from typing import Any


# 离线兜底（discovery 失败时用）
IMAGE_MODELS = [
    {
        "id": "gpt-image",
        "version": "2",
        "label": "GPT Image",
        "kind": "image",
        "sizes": ["auto", "1024x1024", "1536x1024", "1024x1536"],
        "default_size": "auto",
        "detail_levels": [1, 2, 3, 4, 5],
        "default_detail": 3,
        "max_n": 4,
    },
    {
        "id": "gemini-flash",
        "version": "nano-banana-2",
        "label": "Nano Banana",
        "kind": "image",
        "sizes": ["auto", "1024x1024", "1376x768", "768x1376"],
        "default_size": "auto",
        "detail_levels": [1, 2, 3, 4, 5],
        "default_detail": 3,
        "max_n": 4,
    },
]

VIDEO_MODELS = [
    {
        "id": "veo",
        "version": "3.1-generate",
        "label": "Veo 3.1",
        "kind": "video",
        "durations": [4, 5, 6, 8],
        "default_duration": 6,
        "aspect_ratios": ["16:9", "9:16"],
        "default_aspect_ratio": "16:9",
        "sizes": {"16:9": "1280x720", "9:16": "720x1280"},
        "max_n": 1,
        "audio": True,
    },
    {
        "id": "kling",
        "version": "kling_v3_standard_t2v",
        "label": "Kling 3.0 T2V",
        "kind": "video",
        "durations": [5, 10, 15],
        "default_duration": 5,
        "aspect_ratios": ["16:9", "9:16"],
        "default_aspect_ratio": "16:9",
        "sizes": {"16:9": "1280x720", "9:16": "720x1280"},
        "max_n": 1,
        "audio": True,
    },
]


def all_models() -> list[dict]:
    return IMAGE_MODELS + VIDEO_MODELS


def find_model(kind: str, model_id: str, version: str) -> dict | None:
    pool = IMAGE_MODELS if kind == "image" else VIDEO_MODELS
    for m in pool:
        if m["id"] == model_id and str(m["version"]) == str(version):
            return m
    return None


def _walk_schema_enums(node: Any, out: list[Any]) -> None:
    if isinstance(node, dict):
        if "enum" in node and isinstance(node["enum"], list):
            out.extend(node["enum"])
        if "const" in node:
            out.append(node["const"])
        for v in node.values():
            _walk_schema_enums(v, out)
    elif isinstance(node, list):
        for it in node:
            _walk_schema_enums(it, out)


def _size_strings(size_schema: Any) -> list[str]:
    found: list[Any] = []
    _walk_schema_enums(size_schema, found)
    sizes: list[str] = []
    for item in found:
        if isinstance(item, dict) and "width" in item and "height" in item:
            try:
                width, height = int(item["width"]), int(item["height"])
            except (TypeError, ValueError):
                continue
            if width > 0 and height > 0:
                sizes.append(f"{width}x{height}")
        elif isinstance(item, str):
            match = re.fullmatch(r"\s*(\d+)\s*[x×*]\s*(\d+)\s*", item, re.IGNORECASE)
            if match:
                sizes.append(f"{int(match.group(1))}x{int(match.group(2))}")
    # 去重保序
    seen = set()
    uniq = []
    for s in sizes:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


_COMMON_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (
    ("21:9", 21 / 9),
    ("16:9", 16 / 9),
    ("4:3", 4 / 3),
    ("1:1", 1.0),
    ("3:4", 3 / 4),
    ("9:16", 9 / 16),
)


def _aspect_from_size(size: str) -> str:
    """把 960x540 等 discovery 尺寸转换为用户可选的常见长宽比。"""
    if not isinstance(size, str) or "x" not in size.lower():
        return ""
    try:
        width_text, height_text = size.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError):
        return ""
    if width <= 0 or height <= 0:
        return ""

    ratio = width / height
    for label, target in _COMMON_ASPECT_RATIOS:
        if abs(ratio - target) / target <= 0.025:
            return label

    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _explicit_aspect_ratios(schema: Any) -> list[str]:
    """只读取 aspectRatio 相关字段，避免误收集 Schema 中其它字符串枚举。"""
    found: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                if "aspectratio" in normalized:
                    _walk_schema_enums(value, found)
                    if isinstance(value, dict) and value.get("default") is not None:
                        found.append(value.get("default"))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    ratios: list[str] = []
    seen: set[str] = set()
    for value in found:
        if not isinstance(value, str):
            continue
        ratio = value.strip()
        if not ratio:
            continue
        if re.fullmatch(r"\d+\s*:\s*\d+", ratio):
            ratio = ratio.replace(" ", "")
        elif ratio.lower() not in ("auto", "square", "portrait", "landscape"):
            continue
        if ratio in seen:
            continue
        seen.add(ratio)
        ratios.append(ratio)
    return ratios


def _aspect_ratios(schema: Any, sizes: list[str]) -> list[str]:
    """合并 discovery 显式比例与尺寸推导比例，保持上游顺序并去重。"""
    values = _explicit_aspect_ratios(schema)
    values.extend(_aspect_from_size(size) for size in sizes)
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value != "auto" and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _sizes_by_aspect(sizes: list[str]) -> dict[str, str]:
    """为每个真实支持的长宽比选择 discovery 返回的首个合法尺寸。"""
    out: dict[str, str] = {}
    for size in sizes:
        aspect = _aspect_from_size(size)
        if aspect:
            out.setdefault(aspect, size)
    return out


def video_capabilities_from_sizes(sizes: list[str]) -> dict[str, Any]:
    """把模型返回的合法尺寸列表转换成前后端统一消费的能力字段。"""
    clean: list[str] = []
    seen: set[str] = set()
    for value in sizes:
        if not isinstance(value, str):
            continue
        match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", value, re.IGNORECASE)
        if not match:
            continue
        size = f"{int(match.group(1))}x{int(match.group(2))}"
        if size not in seen:
            seen.add(size)
            clean.append(size)
    aspects = _aspect_ratios({}, clean)
    size_map = _sizes_by_aspect(clean)
    return {
        "sizes": ["auto", *clean] if clean else ["auto"],
        "aspect_ratios": aspects,
        "default_aspect_ratio": "16:9" if "16:9" in aspects else (aspects[0] if aspects else ""),
        "sizes_by_aspect": size_map,
    }


def parse_allowed_video_sizes(payload: Any) -> list[str]:
    """从 Adobe 验证错误中提取 allowed width/height 组合，不保留其它响应内容。"""
    values = _size_strings(payload)
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(payload)
    lower = text.lower()
    marker = lower.find("allowed width, height combinations")
    if marker >= 0:
        segment = text[marker : marker + 3000]
        list_start = segment.find("[")
        list_end = segment.find("]", list_start + 1) if list_start >= 0 else -1
        if list_start >= 0 and list_end > list_start:
            segment = segment[list_start : list_end + 1]
        values.extend(
            f"{int(width)}x{int(height)}"
            for width, height in re.findall(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", segment)
        )
    # 兼容 allowedDimensions: [{width, height}] 一类 JSON 响应。
    elif "allowed" in lower and "dimension" in lower:
        values.extend(
            f"{int(width)}x{int(height)}"
            for width, height in re.findall(
                r'["\']?width["\']?\s*[:=]\s*(\d+).{0,80}?["\']?height["\']?\s*[:=]\s*(\d+)',
                text,
                re.IGNORECASE,
            )
        )
    return video_capabilities_from_sizes(values)["sizes"][1:]


def _int_enum(schema: Any) -> list[int]:
    found: list[Any] = []
    _walk_schema_enums(schema, found)
    vals: list[int] = []
    for item in found:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            vals.append(int(item))
    # min/max fallback
    if not vals and isinstance(schema, dict):
        for node in (schema, *(schema.get("anyOf") or [])):
            if not isinstance(node, dict):
                continue
            mn, mx = node.get("minimum"), node.get("maximum")
            default = node.get("default")
            if isinstance(mn, (int, float)) and isinstance(mx, (int, float)):
                step = 1
                vals = list(range(int(mn), int(mx) + 1, step))
                break
            if isinstance(default, (int, float)):
                vals = [int(default)]
    seen = set()
    out = []
    for v in vals:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return sorted(out)


def _str_enum(schema: Any) -> list[str]:
    found: list[Any] = []
    _walk_schema_enums(schema, found)
    out: list[str] = []
    seen = set()
    for item in found:
        if isinstance(item, str) and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _default_of(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    if "default" in schema:
        return schema.get("default")
    for node in schema.get("anyOf") or []:
        if isinstance(node, dict) and "default" in node:
            return node.get("default")
    return None


def _modality_to_kind(mods: list[str]) -> str:
    mset = {str(x).lower() for x in (mods or [])}
    if "video" in mset:
        return "video"
    if "image" in mset:
        return "image"
    if "audio" in mset:
        return "audio"
    return "other"


def flatten_discovery_models(families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 discovery 的 model family 展开成 (modelId, modelVersion) 条目。

    上游结构:
      [{ modelId, modelVersions: { versionKey: { outputModality, requestSchema, ...}}}]
    展开后每个 version 一条，供 CLI / Web 使用。
    """
    items: list[dict[str, Any]] = []
    for fam in families or []:
        if not isinstance(fam, dict):
            continue
        model_id = str(fam.get("modelId") or fam.get("id") or "").strip()
        if not model_id:
            continue
        provider = str(fam.get("acModelFamilyProviderDisplayName") or "").strip()
        versions = fam.get("modelVersions") or {}
        if not isinstance(versions, dict) or not versions:
            # 兼容扁平结构
            items.append(
                {
                    "id": model_id,
                    "version": str(fam.get("modelVersion") or fam.get("version") or ""),
                    "label": model_id,
                    "kind": "other",
                    "provider": provider,
                    "family": model_id,
                    "release": "",
                    "sizes": ["auto"],
                    "default_size": "auto",
                    "durations": [],
                    "default_duration": None,
                    "aspect_ratios": [],
                    "default_aspect_ratio": "",
                    "detail_levels": [1, 2, 3, 4, 5],
                    "default_detail": 3,
                    "max_n": 1,
                    "audio": False,
                }
            )
            continue

        for ver_key, ver_body in versions.items():
            if not isinstance(ver_body, dict):
                ver_body = {}
            mods = ver_body.get("outputModality") or []
            if not isinstance(mods, list):
                mods = [mods] if mods else []
            kind = _modality_to_kind([str(x) for x in mods])
            request_schema = ver_body.get("requestSchema") or {}
            if not isinstance(request_schema, dict):
                request_schema = {}
            props = (request_schema.get("properties") or {})
            if not isinstance(props, dict):
                props = {}

            # size 可能位于 modelSpecificPayload / anyOf / $defs 中，必须遍历完整 Schema。
            sizes = _size_strings(request_schema)
            if sizes:
                sizes = ["auto", *sizes]
            else:
                sizes = ["auto"]

            durations = _int_enum(props.get("duration"))
            # veo parameters.durationSeconds 等可能在 modelSpecificPayload defs 里，简化：无 enum 时给常见档
            if kind == "video" and not durations:
                durations = [4, 5, 6, 8, 10, 12]

            gen_set = props.get("generationSettings") or {}
            aspect = _aspect_ratios(request_schema, sizes)

            n_schema = props.get("n") or {}
            n_vals = _int_enum(n_schema)
            max_n = max(n_vals) if n_vals else (4 if kind == "image" else 1)
            if max_n < 1:
                max_n = 1

            detail_levels = [1, 2, 3, 4, 5]
            has_detail = "detailLevel" in str(gen_set)
            default_detail = 3 if has_detail else 3

            default_dur = _default_of(props.get("duration"))
            if default_dur is None and durations:
                default_dur = durations[0 if len(durations) == 1 else min(1, len(durations) - 1)]
            try:
                default_dur_i = int(default_dur) if default_dur is not None else None
            except Exception:
                default_dur_i = durations[0] if durations else None

            size_map = _sizes_by_aspect(sizes)

            label = f"{model_id} / {ver_key}"
            if provider:
                label = f"{label} · {provider}"

            items.append(
                {
                    "id": model_id,
                    "version": str(ver_key),
                    "label": label,
                    "kind": kind,
                    "provider": provider,
                    "family": model_id,
                    "release": str(ver_body.get("releaseReadiness") or ""),
                    "modalities": [str(x) for x in mods],
                    "sizes": sizes,
                    "default_size": "auto" if "auto" in sizes else (sizes[0] if sizes else "auto"),
                    "durations": durations,
                    "default_duration": default_dur_i,
                    "aspect_ratios": aspect,
                    "default_aspect_ratio": (
                        "16:9"
                        if "16:9" in aspect
                        else (aspect[0] if aspect else "")
                    ),
                    "sizes_by_aspect": size_map,
                    "detail_levels": detail_levels if has_detail or kind == "image" else [],
                    "default_detail": default_detail,
                    "max_n": int(max_n),
                    "audio": "generateAudio" in props or kind == "video",
                    "input_use": ver_body.get("inputMediaUseCase") or [],
                }
            )
    # 稳定排序：kind, id, version
    kind_order = {"image": 0, "video": 1, "audio": 2, "other": 3}
    items.sort(
        key=lambda x: (
            kind_order.get(x.get("kind") or "other", 9),
            str(x.get("id") or ""),
            str(x.get("version") or ""),
        )
    )
    return items


def split_by_kind(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {
        "image": [],
        "video": [],
        "audio": [],
        "other": [],
    }
    for it in items:
        k = it.get("kind") or "other"
        out.setdefault(k, []).append(it)
    return out
