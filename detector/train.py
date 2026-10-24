"""Train the pothole detector and export it for the laptop (ONNX) and the phone (TFLite).

  pip install ultralytics
  python train.py --data datasets/potholes/data.yaml --epochs 60

Dataset: any YOLO-format pothole set (images/ + labels/ + data.yaml). Public options include
Roboflow Universe pothole datasets and Kaggle pothole-detection sets; check each one's licence.
Add 200-300 frames from YOUR city's roads (Patna/Delhi dashcam) to cut domain shift, and include
negatives: speed breakers, manhole covers, puddles, shadows. Those are the false positives judges ask about.

Outputs (in runs/detect/<name>/weights/):
  best_640.onnx       -> detector/detect.py on a laptop
  best.onnx (320 px)  -> volunteer web app (backend/app/static/rider/model/pothole.onnx)
"""
import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", default="yolo11n.pt", help="nano keeps phone inference ~5-8 FPS")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--name", default="pothole")
    a = ap.parse_args()

    from ultralytics import YOLO

    model = YOLO(a.model)
    model.train(data=a.data, epochs=a.epochs, imgsz=a.imgsz, name=a.name,
                # Dashcam-realistic augmentation: motion, low light, no vertical flips (roads are not upside down).
                hsv_v=0.5, degrees=3, translate=0.1, scale=0.5, fliplr=0.5, flipud=0.0, mosaic=1.0)
    metrics = model.val()
    print("mAP50:", metrics.box.map50, "mAP50-95:", metrics.box.map)
    import shutil
    laptop = model.export(format="onnx", imgsz=a.imgsz, simplify=True)
    laptop = shutil.move(laptop, str(laptop).replace(".onnx", f"_{a.imgsz}.onnx"))  # next export reuses the name
    print("Laptop model:", laptop, "-> detector/detect.py --model", laptop)
    # Phone (volunteer web app): smaller input = 3-4x faster in the browser.
    phone = model.export(format="onnx", imgsz=320, simplify=True)
    print("Phone model:", phone, "-> copy to backend/app/static/rider/model/pothole.onnx")
    print('and create model.json next to it: {"imgsz": 320, "conf": 0.35}')


if __name__ == "__main__":
    main()
