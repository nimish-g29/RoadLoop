"""YOLO (v8 / 11, Ultralytics export) inference with onnxruntime. Same pre/post-processing
the phone does with the TFLite export, so laptop demos and the app agree.

Output layout of an Ultralytics detect export: [1, 4 + num_classes, N] with boxes as
(cx, cy, w, h) in input-pixel space, class scores already sigmoid-ed, no objectness.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    cls: int

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


def letterbox(img: np.ndarray, size: int = 640) -> tuple[np.ndarray, float, tuple[int, int]]:
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, left = (size - nh) // 2, (size - nw) // 2
    out = np.full((size, size, 3), 114, dtype=np.uint8)
    out[top:top + nh, left:left + nw] = resized
    return out, r, (left, top)


def decode(output: np.ndarray, ratio: float, pad: tuple[int, int], orig_shape: tuple[int, int],
           conf_thres: float = 0.35, iou_thres: float = 0.5) -> list[Box]:
    pred = np.squeeze(output, 0)              # [4+nc, N]
    if pred.shape[0] > pred.shape[1]:         # some exports are [N, 4+nc]
        pred = pred.T
    boxes_cxcywh, scores = pred[:4].T, pred[4:].T
    cls = scores.argmax(1)
    conf = scores[np.arange(len(cls)), cls]
    keep = conf >= conf_thres
    if not keep.any():
        return []
    b, conf, cls = boxes_cxcywh[keep], conf[keep], cls[keep]
    x1 = (b[:, 0] - b[:, 2] / 2 - pad[0]) / ratio
    y1 = (b[:, 1] - b[:, 3] / 2 - pad[1]) / ratio
    w, h = b[:, 2] / ratio, b[:, 3] / ratio
    H, W = orig_shape
    rects = [[float(a), float(c), float(d), float(e)] for a, c, d, e in zip(x1, y1, w, h)]
    idx = cv2.dnn.NMSBoxes(rects, conf.astype(float).tolist(), conf_thres, iou_thres)
    out = []
    for i in np.array(idx).reshape(-1):
        rx, ry, rw, rh = rects[i]
        out.append(Box(max(0.0, rx), max(0.0, ry), min(float(W), rx + rw), min(float(H), ry + rh),
                       float(conf[i]), int(cls[i])))
    return sorted(out, key=lambda bx: -bx.conf)


class PotholeModel:
    def __init__(self, onnx_path: str, size: int = 640, conf_thres: float = 0.35):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.size, self.conf_thres = size, conf_thres

    def __call__(self, frame_bgr: np.ndarray) -> list[Box]:
        lb, r, pad = letterbox(frame_bgr, self.size)
        x = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        out = self.sess.run(None, {self.inp: x})[0]
        return decode(out, r, pad, frame_bgr.shape[:2], self.conf_thres)
