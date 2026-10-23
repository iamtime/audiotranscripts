#!/usr/bin/env python3
"""Collect the models bundled into the macOS app into build/models/.

    build/python/bin/python3 packaging/fetch_models.py [--set all|core|none]

"none" keeps only Silero VAD (35 MB, needed by every recording); the app then
downloads each other model the first time it is used (see downloads.py).

Layout of build/models-<set> (the app copies it into its writable model cache on first launch):
    build/models/hf/hub/models--*      Hugging Face cache (HF_HOME=.../hf)
    build/models/torch/hub/snakers4_silero-vad_master   Silero VAD (VOICE_REC_SILERO_DIR)
    build/models/easyocr/model/*.pth   EasyOCR (EASYOCR_MODULE_PATH=.../easyocr)

Models already in the local caches (~/.cache/huggingface, ~/.cache/torch, ~/.EasyOCR)
are clone-copied instead of downloaded. Each model is then loaded once with its own
library, which downloads whatever is missing and proves it works offline later.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parent.parent / "build"
HF_REPOS = {   # repo -> set: core models are always bundled, "all" adds the big Whisper / Parakeet
    "istupakov/gigaam-v3-onnx": "core",
    "speechbrain/spkrec-ecapa-voxceleb": "core",
    "openai/clip-vit-base-patch32": "core",
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo": "all",
    "istupakov/parakeet-tdt-0.6b-v3-onnx": "all",
}


def clone(src: Path, dst: Path):
    if dst.exists() or not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  copy {src} -> {dst}")
    if subprocess.run(["cp", "-cR", str(src), str(dst)]).returncode != 0:
        shutil.copytree(src, dst, symlinks=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("all", "core", "none"), default="all")
    args = ap.parse_args()
    global OUT
    OUT = BUILD / f"models-{args.set}"
    repos = [r for r, s in HF_REPOS.items() if args.set == "all" or (args.set == "core" and s == "core")]

    home = Path.home()
    for r in repos:
        name = "models--" + r.replace("/", "--")
        clone(home / ".cache/huggingface/hub" / name, OUT / "hf/hub" / name)
    clone(home / ".cache/torch/hub/snakers4_silero-vad_master", OUT / "torch/hub/snakers4_silero-vad_master")
    if args.set != "none":
        for f in ("craft_mlt_25k.pth", "cyrillic_g2.pth"):
            clone(home / ".EasyOCR/model" / f, OUT / "easyocr/model" / f)
    # A dropped model set must not linger from an earlier build.
    for d in (OUT / "hf/hub").glob("models--*") if (OUT / "hf/hub").exists() else ():
        if d.name.replace("models--", "").replace("--", "/", 1) not in repos:
            print(f"  remove {d}")
            shutil.rmtree(d)

    os.environ["HF_HOME"] = str(OUT / "hf")
    os.environ["TORCH_HOME"] = str(OUT / "torch")
    os.environ["EASYOCR_MODULE_PATH"] = str(OUT / "easyocr")

    import torch
    print("Silero VAD")
    torch.hub.load(repo_or_dir="snakers4/silero-vad", model="silero_vad", trust_repo=True)

    if args.set == "none":
        subprocess.run(["du", "-sh", str(OUT)])
        return

    import onnx_asr
    for engine, model in (("gigaam", "gigaam-v3-e2e-rnnt"), ("parakeet", "nemo-parakeet-tdt-0.6b-v3")):
        if engine == "parakeet" and args.set != "all":
            continue
        print(f"onnx-asr {model}")
        onnx_asr.load_model(model, quantization="int8", providers=["CPUExecutionProvider"])

    if args.set == "all":
        print("Whisper large-v3-turbo")
        from faster_whisper import WhisperModel
        WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")

    print("CLIP")
    from transformers import CLIPModel, CLIPProcessor
    CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

    print("SpeechBrain ECAPA")
    from speechbrain.inference.speaker import EncoderClassifier
    tmp = OUT / ".speechbrain-tmp"
    EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(tmp))
    shutil.rmtree(tmp, ignore_errors=True)

    print("EasyOCR")
    import easyocr
    easyocr.Reader(["ru", "en"], gpu=False, verbose=False,
                   model_storage_directory=str(OUT / "easyocr/model"),
                   user_network_directory=str(OUT / "easyocr/user_network"))

    # Lock files and the xet/download scratch space are not needed at runtime.
    for junk in [*(OUT / "hf").rglob("*.lock"), OUT / "hf/xet", OUT / "hf/hub/.locks"]:
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)
        elif junk.exists():
            junk.unlink()
    subprocess.run(["du", "-sh", *map(str, sorted(OUT.glob("*")))])


if __name__ == "__main__":
    sys.exit(main())
