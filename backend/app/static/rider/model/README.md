# Pothole model for the volunteer app

Drop the trained model here and drive mode switches on automatically:

- `pothole.onnx`: export from `detector/train.py` (Ultralytics). For phones use **imgsz 320**:
  `yolo export model=runs/detect/pothole/weights/best.pt format=onnx imgsz=320 simplify=True`
- `model.json`: `{"imgsz": 320, "conf": 0.35}` (must match the export size)

After replacing the model, bump `CACHE` in `../sw.js` so phones fetch the new file.
