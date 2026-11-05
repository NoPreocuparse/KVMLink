#!/bin/bash
# Instalador de kvmlink para macOS. Desde la Terminal:  bash instalar.command
# Crea ~/Applications/kvmlink.app y el inicio al iniciar sesión.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null; then
  echo "No se ha encontrado python3. Instálalo desde https://www.python.org/downloads/"
  read -r -p "Pulsa Enter para cerrar…"; exit 1
fi
if ! command -v clang >/dev/null; then
  echo "Faltan las herramientas de desarrollo de Apple. Instálalas con:  xcode-select --install"
  read -r -p "Pulsa Enter para cerrar…"; exit 1
fi
python3 install.py "$@"
echo
read -r -p "Pulsa Enter para cerrar…"
