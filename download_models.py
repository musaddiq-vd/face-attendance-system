"""Downloads face-api.js and its model files into static/ (run once).  python download_models.py"""
import json
import os
import urllib.request

BASE = "https://cdn.jsdelivr.net/npm/@vladmandic/face-api@1.7.13"
HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = ["tiny_face_detector_model", "face_landmark_68_model", "face_recognition_model"]


def fetch(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print("exists  ", os.path.relpath(dest, HERE))
        return
    print("download", url)
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())


def main():
    fetch(f"{BASE}/dist/face-api.js", os.path.join(HERE, "static", "vendor", "face-api.js"))
    model_dir = os.path.join(HERE, "static", "models")
    for name in MODELS:
        manifest = os.path.join(model_dir, f"{name}-weights_manifest.json")
        fetch(f"{BASE}/model/{name}-weights_manifest.json", manifest)
        with open(manifest) as f:
            for group in json.load(f):
                for path in group["paths"]:
                    fetch(f"{BASE}/model/{path}", os.path.join(model_dir, path))
    print("Done.")


if __name__ == "__main__":
    main()
