#!/bin/bash
# Lanzador de kvmlink para macOS: doble clic aquí. Abre la interfaz gráfica.
# Deja abierta esta ventana de la Terminal: los permisos de Accesibilidad y
# Monitorización de entrada van asociados a ella.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null; then
  echo "No se ha encontrado python3. Instálalo desde https://www.python.org/downloads/"
  read -r -p "Pulsa Enter para cerrar…"; exit 1
fi
python3 -c "import Quartz, AppKit, cryptography, pyperclip" 2>/dev/null || {
  echo "Instalando dependencias…"
  # el pip que trae macOS (21.x) es demasiado antiguo y acaba compilando pyobjc desde
  # el código fuente, lo que falla; con un pip actual se descargan binarios ya hechos
  python3 -m pip install --user --upgrade pip || { read -r -p "Pulsa Enter para cerrar…"; exit 1; }
  python3 -m pip install --user --prefer-binary -r requirements.txt || { read -r -p "Pulsa Enter para cerrar…"; exit 1; }
}
echo "kvmlink en marcha. La interfaz se abre en una ventana aparte."
echo "No cierres esta Terminal mientras uses kvmlink (puedes minimizarla)."
echo
# Tras actualizarse desde el otro equipo, kvmlink sale con el código 42 y se vuelve a
# lanzar aquí mismo (así sigue colgando de esta Terminal y conserva sus permisos).
export KVMLINK_LAUNCHER=command
while true; do
  python3 kvmlink.py "$@"
  code=$?
  if [ "$code" -eq 42 ]; then
    echo; echo "kvmlink se ha actualizado: reiniciando…"; echo
    continue
  fi
  [ "$code" -ne 0 ] && read -r -p "Pulsa Enter para cerrar…"
  break
done
