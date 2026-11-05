"""Actualizar kvmlink desde el otro equipo.

El equipo que se actualiza (y solo cuando el usuario lo acepta en su ventana) pide los
archivos del programa al otro ("upd_req"). El otro responde con la lista de archivos y
sus sha256 ("upd_manifest") y los envía por la cola de archivos de siempre. Al terminar:
  1. se comprueba que han llegado todos y que cada sha256 coincide;
  2. se guarda una copia de los archivos actuales en la carpeta de configuración;
  3. se sustituyen uno a uno (os.replace: atómico) conservando los permisos;
  4. si ha cambiado requirements.txt se instalan las dependencias;
  5. se avisa (on_applied) para reiniciar kvmlink.
Solo se aceptan archivos del programa (version.is_program_file) y solo dentro de su carpeta.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import version


class Updater:
    def __init__(self, core, log, config_dir, program_dir=version.PROGRAM_DIR, on_applied=None):
        self.core, self.log = core, log
        self.program_dir = program_dir
        self.config_dir = config_dir
        self.on_applied = on_applied
        self.id = f"u{int(time.time() * 1000):x}"
        self.staging = os.path.join(tempfile.gettempdir(), "kvmlink-actualizacion", self.id)
        self.manifest = None
        self.peer_build = None
        self.status = "inactivo"
        self.detail = ""
        self.done = threading.Event()

    # ------------------------------------------------------------ petición
    def start(self):
        shutil.rmtree(os.path.dirname(self.staging), ignore_errors=True)   # restos anteriores
        os.makedirs(self.staging, exist_ok=True)
        self.core.receiver.targets[self.id] = self
        self.status = "descargando"
        self.core.transfers.start(self.id, "in", "Actualización de kvmlink", 0)
        self.log("pidiendo la versión de kvmlink del otro equipo")
        self.core.send({"t": "upd_req", "id": self.id, "build": version.BUILD})

    def set_manifest(self, msg):
        files = msg.get("files") or []
        # Tipos de archivo que esta versión no conoce (los añadirá una versión más
        # nueva): se apartan sin instalarlos, en vez de anular toda la actualización.
        skipped = [f[0] for f in files if not version.is_program_file(f[0])]
        if skipped:
            self.log("la actualización trae archivos que esta versión no instala:", ", ".join(skipped))
        self.manifest = [(n, int(s), h) for n, s, h in files if version.is_program_file(n)]
        self.peer_build = msg.get("build")
        self.core.transfers.start(self.id, "in", f"Actualización de kvmlink (versión {self.peer_build})",
                                  sum(s for _, s, _ in self.manifest))

    # ------------------------------------- interfaz que usa files.Receiver
    def path_for(self, key):
        if not version.is_program_file(key):
            # se recibe aparte y se borra con el resto: nunca se instala
            discard = os.path.join(self.staging, "_descartados")
            os.makedirs(discard, exist_ok=True)
            return os.path.join(discard, f"{abs(hash(key))}.bin")
        return os.path.join(self.staging, key)

    def on_start(self, key):
        pass

    def on_data(self, key, n):
        pass

    def on_end(self, key):
        pass

    def on_done(self):
        self.status = "instalando"
        threading.Thread(target=self._apply, name="update-apply", daemon=True).start()

    def fail(self, reason):
        self.status, self.detail = "error", reason
        self.core.transfers.finish(self.id, "error", reason)
        self.log("no se pudo actualizar kvmlink:", reason)
        self.done.set()

    # ---------------------------------------------------------- instalación
    def _verify(self):
        if self.manifest is None:
            raise ValueError("no llegó la lista de archivos")
        if not any(n == "kvmlink.py" for n, _, _ in self.manifest):
            raise ValueError("la actualización no contiene kvmlink.py")
        for name, size, digest in self.manifest:
            path = os.path.join(self.staging, name)
            if not os.path.isfile(path):
                raise ValueError(f"falta {name}")
            with open(path, "rb") as f:
                data = f.read()
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"{name} ha llegado dañado")

    def _backup(self):
        dest = os.path.join(self.config_dir, "copias", f"version{version.BUILD}-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(dest, exist_ok=True)
        for name, _, _ in version.manifest(self.program_dir):
            shutil.copy2(os.path.join(self.program_dir, name), os.path.join(dest, name))
        return dest

    def _install(self):
        changed = []
        current = {n: h for n, _, h in version.manifest(self.program_dir)}
        for name, _, digest in self.manifest:
            if current.get(name) == digest:
                continue
            dest = os.path.join(self.program_dir, name)
            tmp = dest + ".kvmnew"
            shutil.copyfile(os.path.join(self.staging, name), tmp)   # misma unidad que el destino
            if os.path.exists(dest):
                shutil.copymode(dest, tmp)
            if name.endswith(".command"):
                os.chmod(tmp, 0o755)
            os.replace(tmp, dest)   # atómico: si algo lo está leyendo, sigue con la versión anterior
            changed.append(name)
        return changed

    def _install_requirements(self):
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", os.path.join(self.program_dir, "requirements.txt")]
        if sys.platform == "darwin":
            cmd[4:4] = ["--user", "--prefer-binary"]
        self.log("instalando las dependencias nuevas …")
        subprocess.run(cmd, check=True, timeout=600,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _apply(self):
        try:
            self._verify()
            backup = self._backup()
            changed = self._install()
            if "requirements.txt" in changed:
                self._install_requirements()
        except subprocess.CalledProcessError as e:
            self.fail(f"no se pudieron instalar las dependencias ({e})")
            return
        except (OSError, ValueError) as e:
            self.fail(str(e))
            return
        finally:
            shutil.rmtree(self.staging, ignore_errors=True)
        self.status = "listo"
        self.detail = f"{len(changed)} archivo(s) actualizado(s)"
        self.core.transfers.finish(self.id, "instalado", self.detail)
        self.log(f"kvmlink actualizado a la versión {self.peer_build} ({self.detail}); "
                 f"copia de la versión anterior en {backup}")
        self.done.set()
        if self.on_applied:
            self.on_applied()
