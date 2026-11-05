"""Buscar e instalar versiones nuevas de kvmlink publicadas en GitHub.

Al abrirse (y luego cada 12 h) kvmlink consulta la última versión publicada en las
releases del repositorio. Si es más nueva que la propia, la ventana lo avisa; solo si el
usuario pulsa "Actualizar":
  1. se descarga el zip de esa versión;
  2. se comprueba su sha256 con el que publica GitHub;
  3. se extraen solo los archivos del programa (version.is_program_file) de la carpeta
     kvmlink/ del zip, nada con subcarpetas ni rutas raras;
  4. se instala igual que una actualización desde el otro equipo (update.Updater):
     comprobación, copia de la versión anterior, sustitución y reinicio.

En el Mac se usa el curl del sistema: el python3 de macOS a veces no tiene los
certificados para HTTPS y curl sí (usa los del llavero).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile

import update
import version

REPO = "NoPreocuparse/KVMLink"
API = "https://api.github.com"
CHECK_EVERY = 12 * 3600
USER_AGENT = f"kvmlink/{version.NAME}"


def parse_version(text):
    """'v1.2' / 'kvmlink 1.2.3' -> (1, 2) / (1, 2, 3), para comparar versiones."""
    nums = tuple(int(n) for n in re.findall(r"\d+", text or ""))
    return nums or (0,)


def _fetch(url, dest=None, accept="application/vnd.github+json", progress=None, timeout=60):
    """Descarga url. Sin dest devuelve los bytes; con dest los escribe ahí."""
    if sys.platform == "darwin":
        cmd = ["curl", "-fsSL", "--max-time", str(timeout * 10), "-A", USER_AGENT, "-H", f"Accept: {accept}", url]
        if dest:
            subprocess.run(cmd + ["-o", dest], check=True, capture_output=True)
            if progress:
                progress(os.path.getsize(dest))
            return None
        return subprocess.run(cmd, check=True, capture_output=True).stdout
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not dest:
            return resp.read()
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                if progress:
                    progress(len(chunk))
    return None


def latest_release(api=None, repo=None):
    """La última versión publicada: {version, title, notes, page, zip_url, size, sha256}."""
    data = json.loads(_fetch(f"{api or API}/repos/{repo or REPO}/releases/latest"))
    asset = next((a for a in data.get("assets", []) if a.get("name", "").endswith(".zip")), None)
    if asset is None:
        raise ValueError("la última versión publicada no tiene zip")
    digest = asset.get("digest") or ""
    return {"version": data.get("tag_name", "").lstrip("v"), "title": data.get("name") or data.get("tag_name"),
            "notes": data.get("body") or "", "page": data.get("html_url", ""),
            "zip_url": asset["browser_download_url"], "size": asset.get("size", 0),
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None}


def is_newer(release, current=version.NAME):
    return parse_version(release["version"]) > parse_version(current)


class Checker:
    """Comprueba en segundo plano si hay versión nueva (al abrir y cada CHECK_EVERY)."""

    def __init__(self, log, api=None, repo=None):
        self.log, self.api, self.repo = log, api, repo
        self.available = None      # release más nueva que la nuestra, o None
        self.error = None
        self.checked = False
        self._stop = threading.Event()

    def start(self, first_delay=5):
        threading.Thread(target=self._run, args=(first_delay,), name="github-check", daemon=True).start()

    def stop(self):
        self._stop.set()

    def check_now(self):
        try:
            rel = latest_release(self.api, self.repo)
            self.error = None
        except Exception as e:   # sin internet, GitHub caído, repositorio privado…
            self.error = str(e)
            return None
        finally:
            self.checked = True
        newer = rel if is_newer(rel) else None
        if newer and (self.available or {}).get("version") != newer["version"]:
            self.log(f"hay una versión nueva de kvmlink en GitHub: {newer['version']}")
        self.available = newer
        return newer

    def _run(self, first_delay):
        if self._stop.wait(first_delay):
            return
        while True:
            self.check_now()
            if self._stop.wait(CHECK_EVERY):
                return


class GithubUpdater(update.Updater):
    """Como update.Updater, pero los archivos vienen del zip de la release de GitHub."""

    def __init__(self, core, log, config_dir, release, program_dir=version.PROGRAM_DIR, on_applied=None):
        super().__init__(core, log, config_dir, program_dir=program_dir, on_applied=on_applied)
        self.release = release
        self.peer_build = release["version"]

    def start(self):
        shutil.rmtree(os.path.dirname(self.staging), ignore_errors=True)   # restos anteriores
        os.makedirs(self.staging, exist_ok=True)
        self.status = "descargando"
        self.core.transfers.start(self.id, "in", f"kvmlink {self.release['version']} desde GitHub",
                                  self.release.get("size") or 0)
        self.log(f"descargando kvmlink {self.release['version']} desde GitHub")
        threading.Thread(target=self._download, name="github-update", daemon=True).start()

    def _download(self):
        zip_path = self.staging + ".zip"
        try:
            _fetch(self.release["zip_url"], dest=zip_path, accept="application/octet-stream",
                   progress=lambda n: self.core.transfers.progress(self.id, n))
            if self.release.get("sha256"):
                with open(zip_path, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() != self.release["sha256"]:
                        raise ValueError("el zip descargado no coincide con el publicado en GitHub")
            self.manifest = self._extract(zip_path)
        except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as e:
            self.fail(f"no se pudo descargar la versión nueva: {e}")
            shutil.rmtree(self.staging, ignore_errors=True)
            return
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass
        self.on_done()   # instala como una actualización desde el otro equipo

    def _extract(self, zip_path):
        """Extrae solo archivos del programa que estén justo dentro de la carpeta del zip."""
        manifest = []
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                parts = info.filename.split("/")
                if info.is_dir() or len(parts) != 2 or not version.is_program_file(parts[1]):
                    continue
                data = z.read(info)
                with open(os.path.join(self.staging, parts[1]), "wb") as f:
                    f.write(data)
                manifest.append((parts[1], len(data), hashlib.sha256(data).hexdigest()))
        return manifest
