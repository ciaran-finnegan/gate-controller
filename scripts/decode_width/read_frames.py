"""Task 1 (decode width), docs/reviews/2026-10-09-latency-and-reliability.md.

Read decoded NVR frames through the Pi's real local path.

read_frames.py NAME WIDTH VARIANT  ->  $DW_ROOT/out/NAME/WIDTH-VARIANT.jsonl
VARIANT: sweep (local_sweep.crop_with_geometry, band q90, uncapped) or
         capNNNN (ocr.PlateRecognizerClient._open_upload with max_upload_width=NNNN: band, Lanczos, q85).
Engine: local_recognizer.OnnxPlateReadEngine, yolo-v9-t-384 + cct-xs-v2, one thread.
"""
import json, sys, time
from pathlib import Path
from types import SimpleNamespace
import os
# DW_ROOT holds frames/<name>/<width>/, frames/<name>/pts.csv, models/ and out/.
WORK = Path(os.environ.get("DW_ROOT", ".")).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gate_controller.local_recognizer import (
    OnnxPlateReadEngine, LocalRecognizerConfig, box_to_frame, MODE_ACTIVE)
from gate_controller.local_sweep import crop_with_geometry, plate_width_px
from gate_controller.ocr import PlateRecognizerClient
from gate_controller.plate_region import parse_plate_region

REGION = parse_plate_region("0.05,0.10,0.90,0.85")
name, width, variant = sys.argv[1], sys.argv[2], sys.argv[3]
lo = int(sys.argv[4]) if len(sys.argv) > 4 else 1
hi = int(sys.argv[5]) if len(sys.argv) > 5 else 10**9
frames = WORK / "frames" / name / width
pts = [float(l.split(",")[0]) for l in open(WORK / "frames" / name / "pts.csv")]
engine = OnnxPlateReadEngine(LocalRecognizerConfig(
    mode=MODE_ACTIVE, threads=1, model_dir=WORK / "models"))
engine.load(); engine.warmup()
if variant.startswith("cap"):
    client = SimpleNamespace(_plate_region=REGION, _max_upload_width=int(variant[3:]),
                             _precropped_directory=None)
    client._is_precropped = lambda p: False
out = WORK / "out" / name; out.mkdir(parents=True, exist_ok=True)
with open(out / f"{width}-{variant}.jsonl", "w") as sink:
    for path in sorted(frames.glob("*.jpg")):
        index = int(path.stem)
        if not lo <= index <= hi:
            continue
        t0 = time.perf_counter()
        if variant == "sweep":
            image, geometry = crop_with_geometry(path.read_bytes(), REGION)
        else:
            upload, geometry = PlateRecognizerClient._open_upload(client, path)
            image = upload.read(); upload.close()
        prep_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        result = engine.read(image)
        read_ms = (time.perf_counter() - t1) * 1000
        best = result.reads[0] if result.reads else None
        box = box_to_frame(best.box, geometry, REGION, result.width, result.height) if best else None
        sink.write(json.dumps({
            "i": index, "t": pts[index - 1], "plate": best.plate if best else None,
            "score": round(best.confidence, 4) if best else 0.0,
            "px": plate_width_px(box) if box else None,
            "in_w": result.width, "prep_ms": round(prep_ms, 1), "read_ms": round(read_ms, 1),
            "decode_ms": round(result.decode_ms, 1), "detect_ms": round(result.detect_ms, 1),
            "ocr_ms": round(result.ocr_ms, 1),
            "others": [(r.plate, round(r.confidence, 3)) for r in result.reads[1:]],
        }) + "\n")

# Run on a workstation only, never on the Pi (CLAUDE.md "The Pi has no fan").
# Models: copy yolo-v9-t-384-license-plate-end2end/ and cct-xs-v2-global-model/
# into DW_ROOT/models so nothing is downloaded.
