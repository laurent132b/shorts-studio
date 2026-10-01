"""Serveur local de review : lire un rendu image par image et poser une note par frame.

    python tools/review/serveur.py            # http://127.0.0.1:8790
    REVIEW_PORT=8791 python tools/review/serveur.py

En temps normal on ne le lance pas a la main : `assemble.py` l'ouvre des qu'un master est
ecrit, et `python tools/retours.py <slug> --ouvrir` fait de meme pour n'importe quel rendu.

Ce qu'il sert, et rien d'autre : les fichiers de `videos/<slug>/_media/masters/`. Le dossier
video doit etre EXACTEMENT un nom de `videos/`, et le rendu un nom de fichier nu present dans
`_media/masters/`. Il n'ecrit que dans `videos/<slug>/2_habillage/retours/`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS))
import paths    # noqa: E402
import retours  # noqa: E402

for _flux in (sys.stdout, sys.stderr):
    if hasattr(_flux, "reconfigure"):
        _flux.reconfigure(errors="replace")

PAGE = HERE / "review.html"
BLOC = 1024 * 1024
SANS_FENETRE = {"creationflags": 0x08000000} if os.name == "nt" else {}
_INFO_CACHE: dict[tuple, dict] = {}


def _resoudre(video: str, rendu: str | None) -> tuple[Path | None, Path | None, str]:
    """(dossier video, fichier rendu, erreur). Noms exacts, jamais de chemin."""
    if not video or video != Path(video).name:
        return None, None, "nom de video invalide"
    vd = {v.name: v for v in paths.list_videos()}.get(video)
    if vd is None:
        return None, None, f"video inconnue : {video}"
    if rendu is None:
        return vd, None, ""
    if rendu != Path(rendu).name or Path(rendu).suffix.lower() not in retours.EXT_RENDUS:
        return vd, None, "nom de rendu invalide"
    f = vd / "_media" / "masters" / rendu
    if not f.is_file():
        return vd, None, f"rendu absent de _media/masters/ : {rendu}"
    return vd, f, ""


def _info(f: Path) -> dict:
    """fps, duree, dimensions : ffprobe une fois par (chemin, taille, mtime)."""
    st = f.stat()
    cle = (str(f), st.st_size, int(st.st_mtime))
    if cle in _INFO_CACHE:
        return _INFO_CACHE[cle]
    out = {"fps": None, "duree_s": None, "largeur": None, "hauteur": None}
    try:
        r = subprocess.run(
            [paths.ffprobe(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate,avg_frame_rate,width,height:format=duration",
             "-of", "json", str(f)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            stdin=subprocess.DEVNULL, **SANS_FENETRE)
        d = json.loads(r.stdout or "{}")
        s = (d.get("streams") or [{}])[0]
        for cle_fps in ("avg_frame_rate", "r_frame_rate"):
            brut = s.get(cle_fps) or ""
            if "/" in brut:
                a, b = brut.split("/")
                if float(b):
                    out["fps"] = round(float(a) / float(b), 3)
                    break
        out["largeur"], out["hauteur"] = s.get("width"), s.get("height")
        dur = (d.get("format") or {}).get("duration")
        out["duree_s"] = round(float(dur), 3) if dur else None
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        out["erreur"] = f"ffprobe : {type(e).__name__}"
    _INFO_CACHE[cle] = out
    return out


def _videos() -> dict:
    liste = []
    for vd in paths.list_videos():
        rs = retours.rendus(vd)
        if rs:
            liste.append({"dossier": vd.name, "serie": "Videos", "rendus": rs,
                          "retours": retours.resume(vd)})
    return {"videos": liste}


class Handler(BaseHTTPRequestHandler):
    server_version = "Review/1.0"

    def log_message(self, *a):
        pass

    def _bytes(self, corps: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    def _json(self, obj, code: int = 200):
        self._bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", code)

    def _erreur(self, code: int, msg: str):
        self._json({"erreur": msg}, code)

    def _corps_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _media(self, f: Path):
        """Sert un rendu avec les requetes Range : c'est ce qui permet de scrubber."""
        taille = f.stat().st_size
        ctype = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}.get(
            f.suffix.lower(), "application/octet-stream")
        debut, fin = 0, taille - 1
        plage = self.headers.get("Range")
        if plage:
            m = plage.strip().lower().replace("bytes=", "").split("-", 1)
            try:
                debut = int(m[0]) if m[0] else max(0, taille - int(m[1]))
                fin = int(m[1]) if len(m) > 1 and m[1] and m[0] else taille - 1
            except ValueError:
                debut, fin = 0, taille - 1
            if debut >= taille or debut > fin:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{taille}")
                self.end_headers()
                return
            fin = min(fin, taille - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {debut}-{fin}/{taille}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fin - debut + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        reste = fin - debut + 1
        try:
            with f.open("rb") as fh:
                fh.seek(debut)
                while reste > 0:
                    bloc = fh.read(min(BLOC, reste))
                    if not bloc:
                        break
                    self.wfile.write(bloc)
                    reste -= len(bloc)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            route = u.path
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if route in ("/", "/index.html", "/review"):
                self._bytes(PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if route == "/api/identite":
                self._json({"app": retours.APP, "racine": str(paths.ROOT)})
                return
            if route == "/api/videos":
                self._json(_videos())
                return
            if route == "/api/info":
                vd, f, err = _resoudre(q.get("video", ""), q.get("rendu"))
                if f is None:
                    self._erreur(404, err or "rendu manquant")
                    return
                st = f.stat()
                self._json({**_info(f), "taille": st.st_size, "mtime": int(st.st_mtime)})
                return
            if route == "/api/retours":
                vd, _, err = _resoudre(q.get("video", ""), None)
                if vd is None:
                    self._erreur(404, err)
                    return
                self._json({"video": vd.name, "lots": retours.lire_retours(vd)})
                return
            if route.startswith("/media/"):
                bouts = [urllib.parse.unquote(x) for x in route[len("/media/"):].split("/")]
                if len(bouts) != 2:
                    self._erreur(404, "attendu /media/<video>/<rendu>")
                    return
                vd, f, err = _resoudre(bouts[0], bouts[1])
                if f is None:
                    self._erreur(404, err)
                    return
                self._media(f)
                return
            self._erreur(404, f"route inconnue : {route}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._erreur(500, f"{type(e).__name__}: {e}")
            except OSError:
                pass

    def do_POST(self):
        try:
            route = urllib.parse.urlparse(self.path).path
            corps = self._corps_json()
            if route == "/api/arreter":
                # `python tools/retours.py --arreter` : on repond, PUIS on s'arrete
                self._json({"ok": True})
                import threading
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route == "/api/retours":
                vd, f, err = _resoudre(str(corps.get("video", "")), corps.get("rendu"))
                if vd is None:
                    self._erreur(404, err)
                    return
                meta = {}
                if f is not None:
                    st = f.stat()
                    meta = {"rendu_taille": st.st_size, "rendu_mtime": int(st.st_mtime)}
                for cle in ("fps", "duree_s"):
                    if corps.get(cle) is not None:
                        meta[cle] = corps[cle]
                lot = retours.ecrire_retours(vd, str(corps.get("rendu")), corps.get("notes"), meta)
                print(f"[review] {vd.name}  {lot['rendu']}  {len(lot['notes'])} retour(s)")
                self._json({"ok": True, "lot": lot})
                return
            if route == "/api/retours/traite":
                vd, _, err = _resoudre(str(corps.get("video", "")), None)
                if vd is None:
                    self._erreur(404, err)
                    return
                lot = retours.marquer_traite(vd, str(corps.get("rendu", "")),
                                             bool(corps.get("traite")))
                if lot is None:
                    self._erreur(404, "aucun lot pour ce rendu")
                    return
                self._json({"ok": True, "lot": lot})
                return
            self._erreur(404, f"route inconnue : {route}")
        except Exception as e:  # noqa: BLE001
            try:
                self._erreur(500, f"{type(e).__name__}: {e}")
            except OSError:
                pass


def main() -> int:
    port = int(os.environ.get("REVIEW_PORT", retours.PORT_DEFAUT))
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[review] port {port} indisponible ({e}). Un serveur tourne peut-etre deja : "
              f"http://127.0.0.1:{port}  -  sinon REVIEW_PORT=<autre>.")
        return 2
    print(f"[review] http://127.0.0.1:{port}   (Ctrl+C pour arreter)")
    print("[review] retours ecrits dans videos/<slug>/2_habillage/retours/  -  lus par tools/retours.py")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[review] arret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
