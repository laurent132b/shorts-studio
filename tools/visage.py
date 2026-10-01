"""visage.py -- le poids du detecteur de visage YuNet, et son telechargement.

    python tools/visage.py telecharger     # pose models/face_detection_yunet_2023mar.onnx
    python tools/visage.py verifier        # 0 present et intact, 1 absent ou corrompu

YuNet (232 ko, licence Apache-2.0, opencv_zoo) tourne dans OpenCV, sans GPU. Il sert a
`punch.py reperes` : savoir ou sont les yeux pour ancrer le recadrage. Le poids n'est pas
versionne (models/ est ignore par git) : il se telecharge une fois.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

NOM = "face_detection_yunet_2023mar.onnx"
URL_MODELE = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
              "face_detection_yunet/" + NOM)
SHA_MODELE = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


def modele_yunet() -> Path:
    """`YUNET_MODEL` s'il est defini, sinon `models/<nom>`."""
    if os.environ.get("YUNET_MODEL"):
        return Path(os.environ["YUNET_MODEL"])
    return paths.MODELS / NOM


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def est_intact(p: Path | None = None) -> bool:
    p = p or modele_yunet()
    return p.is_file() and sha256(p) == SHA_MODELE


def telecharger() -> int:
    cible = modele_yunet()
    if est_intact(cible):
        print(f"OK  deja present : {cible}")
        return 0
    cible.parent.mkdir(parents=True, exist_ok=True)
    tmp = cible.with_suffix(".part")
    print(f"telechargement de {NOM} (232 ko)...")
    try:
        urllib.request.urlretrieve(URL_MODELE, tmp)
    except OSError as e:
        print(f"ECHEC  {e}\n  a la main : curl -sSL -o \"{cible}\" {URL_MODELE}")
        return 1
    if sha256(tmp) != SHA_MODELE:
        tmp.unlink(missing_ok=True)
        print("ECHEC  le fichier telecharge n'a pas l'empreinte attendue : supprime.")
        return 1
    os.replace(tmp, cible)
    print(f"OK  {cible}")
    return 0


def main(argv: list[str]) -> int:
    verbe = argv[1] if len(argv) > 1 else "verifier"
    if verbe == "telecharger":
        return telecharger()
    if verbe == "verifier":
        p = modele_yunet()
        if est_intact(p):
            print(f"OK  {p}")
            return 0
        print(f"ABSENT ou corrompu : {p}\n  python tools/visage.py telecharger")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
