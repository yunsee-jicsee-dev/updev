"""What models exist, where they come from, and what they cost.

Weights are not vendored — they are large, they have their own licences, and
they change on their own schedule. This is the catalogue; `camtoy models
pull` fetches from it into MODEL_DIR.

Every `size` here was measured from the server, so a truncated download is
detectable without trusting the file to parse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
_ONNX = "https://github.com/onnx/models/raw/main/validated/vision/"
_HF = "https://huggingface.co/"

# Override for a shared or external drive: CAMTOY_MODELS=/mnt/big/models
MODEL_DIR = Path(os.environ.get("CAMTOY_MODELS") or
                 Path(__file__).resolve().parent.parent.parent / "models")


# Above this, a model cannot keep a camera feeling live no matter how the
# rest of the pipeline is tuned.
INTERACTIVE_MS = 250


@dataclass(frozen=True)
class ModelSpec:
    key: str            # "detect/yolox" -> models/detect/yolox.onnx
    url: str
    size: int           # bytes, as served
    task: str
    note: str
    ms: int             # measured median inference, Raspberry Pi 5 CPU, 4 threads
    licence: str = ""
    broken: str = ""    # non-empty means it loads but will not execute here

    @property
    def path(self) -> Path:
        return MODEL_DIR / f"{self.key}.onnx"

    @property
    def present(self) -> bool:
        return self.path.is_file() and self.path.stat().st_size == self.size

    @property
    def megabytes(self) -> float:
        return self.size / 1048576

    @property
    def realtime(self) -> bool:
        return not self.broken and 0 < self.ms <= INTERACTIVE_MS

    @property
    def fps(self) -> float:
        return 1000 / self.ms if self.ms else 0.0


def _s(key, url, size, task, note, ms, licence="", broken="") -> tuple[str, ModelSpec]:
    return key, ModelSpec(key, url, size, task, note, ms, licence, broken)


MODELS: dict[str, ModelSpec] = dict([
    # -- detection ---------------------------------------------------------
    _s("detect/yolox", _ZOO + "object_detection_yolox/object_detection_yolox_2022nov.onnx",
       35858002, "detect", "YOLOX-S, 80 COCO classes", 2050, "Apache-2.0"),
    _s("detect/nanodet", _ZOO + "object_detection_nanodet/object_detection_nanodet_2022nov.onnx",
       3800954, "detect", "NanoDet-Plus-m, 80 COCO classes, much faster", 150, "Apache-2.0"),
    _s("plate/lpd", _ZOO + "license_plate_detection_yunet/license_plate_detection_lpd_yunet_2023mar.onnx",
       4146213, "plate", "licence plate localiser", 240, "Apache-2.0"),

    # -- classification ----------------------------------------------------
    _s("classify/mobilenetv2", _ZOO + "image_classification_mobilenet/image_classification_mobilenetv2_2022apr.onnx",
       13964571, "classify", "MobileNetV2, 1000 ImageNet classes", 102, "Apache-2.0"),
    _s("classify/ppresnet50", _ZOO + "image_classification_ppresnet/image_classification_ppresnet50_2022jan.onnx",
       102567035, "classify", "PP-ResNet50, more accurate and much slower", 706, "Apache-2.0"),

    # -- faces -------------------------------------------------------------
    _s("face/yunet", _ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
       232589, "face", "YuNet detector, 5 landmarks, 227KB", 164, "MIT"),
    _s("face/sface", _ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
       38696353, "face", "SFace 128-d identity embedding", 107, "Apache-2.0"),
    _s("face/expression", _ZOO + "facial_expression_recognition/facial_expression_recognition_mobilefacenet_2022july.onnx",
       4791892, "face", "MobileFaceNet, 7 expressions", 68, "Apache-2.0"),
    _s("emotion/ferplus", _ONNX + "body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx",
       35040571, "face", "FER+ emotion, 8 classes, grayscale", 107, "MIT"),

    # -- people ------------------------------------------------------------
    _s("pose/person", _ZOO + "person_detection_mediapipe/person_detection_mediapipe_2023mar.onnx",
       11990159, "pose", "person/ROI detector feeding the pose model", 144, "Apache-2.0"),
    _s("pose/body", _ZOO + "pose_estimation_mediapipe/pose_estimation_mediapipe_2023mar.onnx",
       5557238, "pose", "33-point body landmarks", 131, "Apache-2.0"),
    _s("hand/palm", _ZOO + "palm_detection_mediapipe/palm_detection_mediapipe_2023feb.onnx",
       3905734, "hand", "palm detector feeding the landmark model", 146, "Apache-2.0"),
    _s("hand/landmark", _ZOO + "handpose_estimation_mediapipe/handpose_estimation_mediapipe_2023feb.onnx",
       4099621, "hand", "21-point hand landmarks", 53, "Apache-2.0"),
    _s("reid/person", _ZOO + "person_reid_youtureid/person_reid_youtu_2021nov.onnx",
       106878407, "reid", "person re-identification embedding", 533, "Apache-2.0"),

    # -- pixels ------------------------------------------------------------
    _s("segment/pphumanseg", _ZOO + "human_segmentation_pphumanseg/human_segmentation_pphumanseg_2023mar.onnx",
       6163938, "segment", "person/background matte", 118, "Apache-2.0"),
    _s("segment/efficientsam", _ZOO + "image_segmentation_efficientsam/image_segmentation_efficientsam_ti_2024may.onnx",
       47777193, "segment", "segment anything from a point prompt", 14173, "Apache-2.0"),
    _s("depth/depthanything", _HF + "onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx",
       99060839, "depth", "Depth Anything V2 small, monocular depth", 1777, "Apache-2.0"),
    _s("edge/dexined", _ZOO + "edge_detection_dexined/edge_detection_dexined_2024sep.onnx",
       47235563, "edge", "learned edge detection — Sobel's successor", 0, "MIT",
       broken="its quantised weights use a blocked QDQ encoding that "
              "onnxruntime 1.28 rejects at run time (DequantizeLinear: "
              "block_size must be 0 for per-tensor quantization). The graph "
              "loads and then fails on the first inference. Use `camtoy live -e` "
              "for Sobel edges instead."),
    _s("flow/raft", _ZOO + "optical_flow_estimation_raft/optical_flow_estimation_raft_2023aug.onnx",
       64119337, "flow", "RAFT dense optical flow", 25120, "BSD-3-Clause"),
    # Measured at its minimum workable 384px; below that the exported graph
    # collapses a spatial axis to zero and the Pad op fails.
    _s("restore/nafnet", _ZOO + "deblurring_nafnet/deblurring_nafnet_2025may.onnx",
       91736251, "restore", "NAFNet motion deblurring, 384px minimum", 22058, "MIT"),
    _s("restore/lama", _ZOO + "inpainting_lama/inpainting_lama_2025jan.onnx",
       92591623, "restore", "LaMa inpainting — paint a mask, erase the object", 34297, "Apache-2.0"),

    # -- text --------------------------------------------------------------
    _s("text/detect_en", _ZOO + "text_detection_ppocr/text_detection_en_ppocrv3_2023may.onnx",
       2423490, "text", "PP-OCRv3 text box detector", 102, "Apache-2.0"),
    _s("text/recog_en", _ZOO + "text_recognition_crnn/text_recognition_CRNN_EN_2021sep.onnx",
       33823087, "text", "CRNN English text recognition", 105, "Apache-2.0"),

    # -- tracking ----------------------------------------------------------
    _s("track/vittrack", _ZOO + "object_tracking_vittrack/object_tracking_vittrack_2023sep.onnx",
       714726, "track", "ViT single-object tracker, 745KB", 37, "Apache-2.0"),

    # -- style -------------------------------------------------------------
    *[_s(f"style/{name.replace('-', '_')}",
         _ONNX + f"style_transfer/fast_neural_style/model/{name}-9.onnx",
         6728029, "style", f"fast-neural-style: {name}", 900, "BSD-3-Clause")
      for name in ("mosaic", "candy", "udnie", "rain-princess", "pointilism")],
])

LABEL_URLS = {
    "imagenet": "https://raw.githubusercontent.com/pytorch/hub/master/imagenet_classes.txt",
}

TASKS = ("detect", "classify", "face", "pose", "hand", "segment", "depth",
         "edge", "style", "text", "track", "plate", "reid", "flow", "restore")


def by_task(task: str) -> list[ModelSpec]:
    return [m for m in MODELS.values() if m.task == task]


def missing() -> list[ModelSpec]:
    return [m for m in MODELS.values() if not m.present]


def total_bytes(specs=None) -> int:
    return sum(m.size for m in (specs if specs is not None else MODELS.values()))
