"""模型预设 + 上游 discovery 展开。"""

from __future__ import annotations

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
            sizes.append(f"{item['width']}x{item['height']}")
        elif isinstance(item, str) and "x" in item:
            sizes.append(item)
    # 去重保序
    seen = set()
    uniq = []
    for s in sizes:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


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
            props = ((ver_body.get("requestSchema") or {}).get("properties") or {})
            if not isinstance(props, dict):
                props = {}

            sizes = _size_strings(props.get("size"))
            if sizes:
                sizes = ["auto", *sizes]
            else:
                sizes = ["auto"]

            durations = _int_enum(props.get("duration"))
            # veo parameters.durationSeconds 等可能在 modelSpecificPayload defs 里，简化：无 enum 时给常见档
            if kind == "video" and not durations:
                durations = [4, 5, 6, 8, 10, 12]

            aspect = []
            gen_set = props.get("generationSettings") or {}
            aspect = _str_enum(gen_set)
            # 只保留像比例的
            aspect = [a for a in aspect if ":" in a or a in ("square", "auto", "portrait", "landscape")]
            # TODO: 从 discovery 返回的 modelSpecificPayload / JSON Schema 约束中解析每个模型
            # 实际支持的长宽比；当前 schema 缺失时仍使用固定回退值，可能与模型能力不一致。
            if not aspect and kind in ("image", "video"):
                aspect = ["1:1", "16:9", "9:16", "4:3", "3:4"] if kind == "image" else ["16:9", "9:16"]

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

            size_map = {}
            for s in sizes:
                if s == "auto" or "x" not in s:
                    continue
                try:
                    w, h = s.lower().split("x", 1)
                    wi, hi = int(w), int(h)
                    if wi == hi:
                        size_map.setdefault("1:1", s)
                    elif wi > hi:
                        size_map.setdefault("16:9", s)
                    else:
                        size_map.setdefault("9:16", s)
                except Exception:
                    pass

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
