"""Privacy before upload: only the pothole stays sharp.

Two layers:
 1. Everything outside the (expanded) pothole box is heavily blurred. Number plates of the
    car ahead, pedestrians and shopfronts are all outside a pothole's box, so this alone
    covers most cases without needing a face/plate model.
 2. If OpenCV's Haar cascades are installed (pip opencv ships them), faces and plates found
    INSIDE the sharp region are blurred too.
"""
from __future__ import annotations

import os

import cv2
import numpy as np


def _cascade(name: str):
    path = os.path.join(getattr(cv2, "data", None).haarcascades if hasattr(cv2, "data") else "", name)
    if os.path.exists(path):
        c = cv2.CascadeClassifier(path)
        return None if c.empty() else c
    return None


_FACE = _cascade("haarcascade_frontalface_default.xml")
_PLATE = _cascade("haarcascade_russian_plate_number.xml")


def expand(box, w: int, h: int, frac: float = 0.4) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    return (max(0, int(x1 - bw * frac)), max(0, int(y1 - bh * frac)),
            min(w, int(x2 + bw * frac)), min(h, int(y2 + bh * frac)))


def redact(frame: np.ndarray, box, keep_frac: float = 0.4) -> np.ndarray:
    h, w = frame.shape[:2]
    k = max(31, (min(h, w) // 12) | 1)
    out = cv2.GaussianBlur(frame, (k, k), 0)
    x1, y1, x2, y2 = expand(box, w, h, keep_frac)
    out[y1:y2, x1:x2] = frame[y1:y2, x1:x2]
    region = out[y1:y2, x1:x2]
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.size else None
    for cascade in (c for c in (_FACE, _PLATE) if c is not None and gray is not None):
        for (fx, fy, fw, fh) in cascade.detectMultiScale(gray, 1.1, 4, minSize=(16, 16)):
            roi = region[fy:fy + fh, fx:fx + fw]
            region[fy:fy + fh, fx:fx + fw] = cv2.GaussianBlur(roi, (k, k), 0)
    return out


def encode_jpeg(frame: np.ndarray, max_side: int = 1280, quality: int = 80) -> bytes:
    h, w = frame.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1:
        frame = cv2.resize(frame, (int(w * s), int(h * s)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes()
