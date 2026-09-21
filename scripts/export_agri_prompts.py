#!/usr/bin/env python3
"""Freeze the prompts the farm-machinery check scores a frame against.

Run on a laptop, never on the gate: it needs ``open_clip_torch`` and ``torch``,
neither of which the controller installs. The text side of CLIP never changes
at run time, so it is embedded once here and shipped as numbers in
``gate_controller/models/agri-clip-v1.json``; only the image tower
(``clip-vit-b32-visual-int8.onnx``, the same file direction_vision uses) runs
on the Pi.

    python3 scripts/export_agri_prompts.py

Changing a prompt changes every score, so it invalidates the thresholds in
``gate_controller/agricultural.py``. Re-measure against stored photos
(docs/agricultural-admit.md) before shipping a new file.
"""
from __future__ import annotations

import json
from pathlib import Path

MODEL = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
OUT = Path(__file__).resolve().parents[1] / "gate_controller" / "models" / "agri-clip-v1.json"

#: label -> (kind, prompts). ``machine`` is what is admitted; every ``road``
#: label is something that must never be mistaken for it, and gets a class of
#: its own so that machinery has to beat each of them and not their average.
CLASSES = {
    "machine": ("machine", [
        "a photo of a tractor",
        "a telehandler with forks, a yellow farm loader",
        "a farm machine with big chunky tyres seen up close",
        "agricultural machinery, a tractor pulling a farm implement",
    ]),
    "car": ("road", [
        "a photo of a car",
        "a photo of the front of a car",
        "a photo of the rear of a car",
        "a close-up of the side of a car",
    ]),
    "van": ("road", ["a photo of a van", "a white delivery van"]),
    "lorry": ("road", ["a photo of a lorry", "a large truck, a heavy goods vehicle"]),
    "pickup": ("road", ["a pickup truck or 4x4 jeep", "an SUV"]),
    "trailer": ("road", [
        "a car towing a trailer",
        "a jeep pulling a horse box or livestock trailer",
        "a flatbed trailer behind a vehicle",
    ]),
    "plant": ("other", ["a yellow digger, an excavator, construction machinery"]),
    "other": ("other", [
        "a motorcycle", "a person walking", "a person on a bicycle",
        "a dog or an animal", "a quad bike",
    ]),
    "empty": ("other", [
        "an empty gravel driveway beside a wooden fence",
        "an empty driveway at night",
    ]),
}


def main() -> int:
    import open_clip
    import torch

    model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
    tokenizer = open_clip.get_tokenizer(MODEL)
    model.eval()
    labels, prompts = [], []
    for label, (_kind, texts) in CLASSES.items():
        for text in texts:
            labels.append(label)
            prompts.append(text)
    with torch.no_grad():
        text = model.encode_text(tokenizer(prompts))
        text = text / text.norm(dim=-1, keepdim=True)
    normalise = [t for t in preprocess.transforms if t.__class__.__name__ == "Normalize"][0]
    OUT.write_text(json.dumps({
        "schema": 1,
        "model": f"open_clip {MODEL} {PRETRAINED} (image tower)",
        "input_size": 224,
        "resize": "shortest side to 224, bicubic, then centre crop",
        "mean": [float(value) for value in normalise.mean],
        "std": [float(value) for value in normalise.std],
        "logit_scale": 100.0,
        "kinds": {label: kind for label, (kind, _texts) in CLASSES.items()},
        "labels": labels,
        "prompts": prompts,
        "text_embeddings": [[round(float(v), 6) for v in row] for row in text.numpy()],
    }, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(prompts)} prompts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
