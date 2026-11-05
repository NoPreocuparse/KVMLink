"""Versión de kvmlink y huella de los archivos del programa (para actualizarse entre equipos).

BUILD debe subir con cada versión que se publique: es lo que decide cuál de los dos
equipos tiene la más nueva. La huella (code_hash) detecta además cualquier diferencia en
los archivos aunque el número sea el mismo.
"""
import hashlib
import os

# Versión para las personas (ventana, Aplicaciones instaladas) y la que se compara con
# las releases de GitHub: al publicar una release vX.Y, NAME debe ser "X.Y" en ese código.
NAME = "1.0"

BUILD = 9  # 2: programa instalable (bandeja / barra de menús, inicio automático, app del Mac)
#             3: actualizar desde la versión 1 sin que rechace los instaladores
#             4: volver a abrir kvmlink.app abre su ventana; el instalador cierra la copia en marcha
#             5: reinstalar la app del Mac ya no invalida sus permisos (solo si cambia el lanzador)
#             6: el icono de la barra de menús del Mac aparece (fallaba la clase del menú)
#             7: tras actualizarse, pone al día la instalación sola (sin volver a instalar)
#             8: versión 1.0, primera publicada
#             9: avisa de las versiones nuevas publicadas en GitHub y se actualiza desde ahí

PROGRAM_DIR = os.path.dirname(os.path.abspath(__file__))
_EXTRA = {"kvmlink.bat", "kvmlink.command", "instalar.bat", "instalar.command",
          "requirements.txt", "README.md"}


def is_program_file(name):
    """¿Es un archivo del programa que se puede actualizar? Solo nombres simples (sin
    carpetas) y solo estos tipos: así una actualización no puede escribir otra cosa."""
    if not name or name != os.path.basename(name) or name.startswith(".") or name.startswith("test_"):
        # las pruebas son solo para desarrollar: no van en los zip publicados, así que
        # tampoco cuentan para comparar versiones ni se copian entre equipos
        return False
    return name in _EXTRA or name.endswith(".py") or name == "ui.html"


def manifest(folder=PROGRAM_DIR):
    """[(nombre, tamaño, sha256)] de los archivos del programa, ordenado."""
    out = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if is_program_file(name) and os.path.isfile(path):
            with open(path, "rb") as f:
                data = f.read()
            out.append((name, len(data), hashlib.sha256(data).hexdigest()))
    return out


def code_hash(man=None):
    h = hashlib.sha256()
    for name, size, digest in (man if man is not None else manifest()):
        h.update(f"{name}\0{size}\0{digest}\n".encode())
    return h.hexdigest()[:16]
