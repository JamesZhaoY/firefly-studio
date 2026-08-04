"""模型 discovery 规格解析测试；不访问外网。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import models_catalog as catalog


def _clineto_discovery() -> list[dict]:
    return [
        {
            "modelId": "firefly-video",
            "modelVersions": {
                "clineto": {
                    "outputModality": ["video"],
                    "requestSchema": {
                        "properties": {
                            "modelSpecificPayload": {
                                "anyOf": [
                                    {
                                        "properties": {
                                            "size": {
                                                "enum": [
                                                    {"width": 960, "height": 540},
                                                    {"width": 540, "height": 960},
                                                    {"width": 540, "height": 540},
                                                    {"width": 1280, "height": 720},
                                                ]
                                            }
                                        }
                                    }
                                ]
                            },
                            "duration": {"enum": [5]},
                        }
                    },
                }
            },
        }
    ]


def test_unrelated_string_enums_are_not_sizes() -> None:
    families = _clineto_discovery()
    schema = families[0]["modelVersions"]["clineto"]["requestSchema"]
    schema["properties"]["inputType"] = {"enum": ["text", "image"]}
    model = catalog.flatten_discovery_models(families)[0]
    assert "text" not in model["sizes"]
    assert "image" not in model["sizes"]


def test_nested_sizes_drive_aspect_ratios() -> None:
    model = catalog.flatten_discovery_models(_clineto_discovery())[0]
    assert model["sizes"] == ["auto", "960x540", "540x960", "540x540", "1280x720"]
    assert model["aspect_ratios"] == ["16:9", "9:16", "1:1"]
    assert model["sizes_by_aspect"] == {
        "16:9": "960x540",
        "9:16": "540x960",
        "1:1": "540x540",
    }
    assert "854x480" not in model["sizes"]


def test_explicit_and_derived_ratios_are_merged() -> None:
    families = _clineto_discovery()
    schema = families[0]["modelVersions"]["clineto"]["requestSchema"]
    schema["properties"]["generationSettings"] = {
        "properties": {"aspectRatio": {"enum": ["16:9", "9:16"]}}
    }
    model = catalog.flatten_discovery_models(families)[0]
    assert model["aspect_ratios"] == ["16:9", "9:16", "1:1"]


def test_validation_error_dimensions_are_learned() -> None:
    message = (
        "Validation error from clineto: Provided dimensions (854, 480) are outside "
        "the allowed ranges. Allowed width, height combinations are: "
        "[(960, 540), (540, 960), (540, 540), (1280, 720)]"
    )
    sizes = catalog.parse_allowed_video_sizes(message)
    assert sizes == ["960x540", "540x960", "540x540", "1280x720"]
    assert "854x480" not in sizes
    capabilities = catalog.video_capabilities_from_sizes(sizes)
    assert capabilities["aspect_ratios"] == ["16:9", "9:16", "1:1"]
    assert capabilities["sizes_by_aspect"]["16:9"] == "960x540"


def test_backend_corrects_stale_fixed_size() -> None:
    import app as backend

    model = catalog.flatten_discovery_models(_clineto_discovery())[0]
    original = backend._find_video_model_spec
    backend._find_video_model_spec = lambda _model, _version="": model
    try:
        aspect, size, error = backend._resolve_video_spec(
            "firefly-video", "clineto", "16:9", "854x480"
        )
    finally:
        backend._find_video_model_spec = original
    assert error is None
    assert aspect == "16:9"
    assert size == "960x540"


def test_backend_allows_one_probe_when_discovery_omits_dimensions() -> None:
    import app as backend

    model = {
        "id": "firefly-video",
        "version": "clineto",
        "kind": "video",
        "sizes": ["auto"],
        "aspect_ratios": [],
    }
    original = backend._find_video_model_spec
    backend._find_video_model_spec = lambda _model, _version="": model
    try:
        aspect, size, error = backend._resolve_video_spec(
            "firefly-video", "clineto", "auto", "auto"
        )
    finally:
        backend._find_video_model_spec = original
    assert error is None
    assert aspect == ""
    assert size == ""


def main() -> int:
    test_unrelated_string_enums_are_not_sizes()
    test_nested_sizes_drive_aspect_ratios()
    test_explicit_and_derived_ratios_are_merged()
    test_validation_error_dimensions_are_learned()
    test_backend_corrects_stale_fixed_size()
    test_backend_allows_one_probe_when_discovery_omits_dimensions()
    print("=== model catalog tests passed ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
