# kvmlink

Un solo ratón y un solo teclado para un Mac y un PC Windows en la misma red Wi-Fi.
Lleva el cursor al borde de la pantalla que da al otro ordenador y pasarás a controlarlo;
vuelve por ese mismo borde para recuperar el control. Funciona en los dos sentidos: puedes
usar los periféricos del Mac o los del PC.

- Teclas por **posición física**: con la misma distribución (p.ej. Español ISO) en los dos,
  `ñ`, `@`, `€`, acentos, etc. salen igual que con un teclado físico.
- **Cmd ⇄ Ctrl** se intercambian automáticamente (Cmd+C del Mac llega como Ctrl+C al PC y al revés).
- **Portapapeles de texto** compartido: se copia al cruzar de un equipo a otro.
- **Archivos** entre equipos: arrastrándolos de una pantalla a otra, o con copiar y pegar.
- **Disposición de pantallas** libre, como en la configuración de pantalla de Windows.
- Tráfico **cifrado y autenticado** (ChaCha20-Poly1305) con una contraseña compartida.
- **Descubrimiento automático** en la LAN y reconexión automática.
- Atajo de emergencia mientras controlas el otro equipo: **Ctrl+Alt+Shift+Q**.
  Si se cae la Wi-Fi o el otro equipo, recuperas el control local automáticamente en ≤ 5 s.

## Instalación

Copia esta carpeta a los dos equipos (en el Mac, a tu carpeta personal, por ejemplo a
*Documentos*). Los archivos se quedan en esa carpeta: el instalador solo crea los accesos
directos, la app y el inicio automático, y las actualizaciones desde el otro equipo
sustituyen los archivos de ahí.

### Windows

1. Instala Python 3 desde [python.org](https://www.python.org/downloads/).
2. Doble clic en **`instalar.bat`**. Crea los accesos directos de *kvmlink* en el menú Inicio
   y en el escritorio, hace que se abra solo al iniciar sesión, lo añade a *Configuración →
   Aplicaciones instaladas* (desde ahí se desinstala) y lo abre.

kvmlink vive en la **bandeja, junto al reloj**: clic para abrir su ventana, clic derecho para
salir. Si el icono queda escondido, arrástralo desde la flecha **^** a la barra.

### macOS

1. Abre la Terminal (Cmd+Espacio, escribe "Terminal") y ejecuta, una a una:
   ```bash
   cd ~/Documents/kvmlink
   ```
   ```bash
   bash instalar.command
   ```
   Crea **`kvmlink.app`** en tu carpeta *Aplicaciones* (compila un pequeño lanzador con las
   herramientas de desarrollo de Apple; si faltan, pide instalarlas con
   `xcode-select --install`), hace que se abra solo al iniciar sesión y la abre.
2. macOS pedirá permisos **para kvmlink**: en *Ajustes del Sistema → Privacidad y
   seguridad*, actívalo en **Accesibilidad** y en **Monitorización de entrada**. Mientras
   falten, la ventana de kvmlink lo avisa con un botón que abre ese ajuste; en cuanto los
   concedes empieza a funcionar solo. Ya no hace falta la Terminal ni darle permisos a ella
   (puedes quitárselos).

kvmlink vive en la **barra de menús** (arriba a la derecha), sin icono en el Dock. Si con
muchos iconos queda escondido tras la muesca de la pantalla, ábrelo otra vez desde
*Aplicaciones*, Spotlight o Launchpad: estando en marcha, eso abre su ventana (y desde ella
puedes salir).

Para desinstalar: `instalar.bat --uninstall` en Windows (o desde *Aplicaciones instaladas*),
`bash instalar.command --uninstall` en el Mac. No borra la carpeta ni tu configuración.

Si la instalación de dependencias falla con errores de `pyobjc` en rojo, el pip de macOS es
demasiado antiguo. Actualízalo e instala a mano:
```bash
python3 -m pip install --user --upgrade pip
```
```bash
python3 -m pip install --user --prefer-binary cryptography pyperclip pyobjc-framework-Quartz pyobjc-framework-Cocoa
```

## Uso

### Con la interfaz (lo más fácil)

Abre **kvmlink** desde el menú Inicio / el escritorio (Windows) o desde *Aplicaciones* /
su icono en la barra de menús (Mac). Sin instalar, también se puede abrir con
`kvmlink.bat` o `kvmlink.command` (en el Mac, entonces los permisos son de la Terminal).

Se abre la ventana de kvmlink (en Edge o Chrome en modo aplicación; si no hay ninguno, en
el navegador). En ella:

1. **Conexión:** en un equipo elige *Este equipo espera* y en el otro *Este equipo se conecta*
   (deja la IP vacía para buscarlo solo, o escribe la IP que muestra el otro). Pon la misma
   contraseña en los dos y pulsa **Guardar y conectar**.
2. **Disposición de pantallas:** arrastra las pantallas del otro equipo (en azul) para
   colocarlas como están en tu mesa, igual que en *Configuración → Pantalla* de Windows. Al
   soltarlas se pegan al monitor más cercano. El ratón pasa de un equipo a otro solo por el
   tramo de borde en el que las pantallas se tocan, y a la misma altura física. Basta con
   colocarlas en uno de los dos equipos: el otro se actualiza solo.
3. **Opciones:** velocidad del ratón, Cmd ⇄ Ctrl, portapapeles y scroll; se guardan al momento.
4. **Archivos:** arrastra archivos o carpetas desde el Explorador o el Finder hasta el borde
   que da al otro equipo, sin soltar el botón, y suéltalos allí. Se **copian** (el original no
   se toca) en la carpeta de destino del otro equipo (por defecto *Descargas*, se cambia en la
   sección *Archivos*), que se abre sola al terminar. El progreso se ve en los dos equipos y el
   ratón sigue respondiendo mientras se copia. Si vuelves con el botón aún pulsado, no se envía
   nada y el arrastre sigue en tu equipo. Los dos equipos necesitan esta versión.
   Funciona con el ratón de cualquiera de los dos: también puedes, desde el Mac, manejar el PC,
   arrastrar archivos en su Explorador y volver al Mac sin soltar (y al revés).
5. **Copiar y pegar archivos en los dos sentidos:** copia archivos o carpetas en el Finder
   (Cmd+C) o en el Explorador (Ctrl+C) y pégalos en el otro equipo (Ctrl+V / Cmd+V, menú o
   clic derecho → Pegar). Al copiar solo viajan los nombres; el contenido se transfiere **en
   el momento de pegar**. Lo último que copies, en cualquiera de los dos equipos, es lo que se
   pega.
   - En el PC se ve la ventana de progreso del propio Explorador mientras llega.
   - En el Mac el Finder espera a que lleguen todos los archivos antes de copiarlos (el
     progreso se ve en la sección *Archivos* de kvmlink). Con archivos muy grandes es más
     cómodo arrastrarlos.

6. **Actualizaciones:** si los dos equipos tienen versiones distintas de kvmlink, la ventana
   del que tiene la anterior muestra **Actualizar este equipo**. Al pulsarlo, recibe la
   versión del otro por la conexión cifrada, comprueba cada archivo, guarda una copia de la
   versión anterior (en la carpeta de configuración, `copias`) y kvmlink se reinicia solo; la
   ventana se reconecta sin hacer nada. Solo se instala si lo aceptas en ese equipo, y solo
   se aceptan los archivos del programa. Si la versión nueva cambia algo de la instalación
   (accesos directos, la app del Mac), kvmlink lo pone al día solo al reiniciarse: no hace
   falta volver a ejecutar el instalador. Si en el Mac cambia la app en sí, se relanza con
   la nueva y macOS vuelve a pedir sus permisos (es lo único que no se puede automatizar).

**Cerrar la ventana no detiene kvmlink**: sigue funcionando en segundo plano, con su icono en
la bandeja / barra de menús; desde él vuelves a abrir la ventana o sales. La opción **Abrir
kvmlink al iniciar sesión** está en *Opciones*.
Mientras el otro equipo no se haya conectado nunca, sus pantallas se muestran con un tamaño
aproximado y en semitransparente.

La configuración se guarda en la carpeta del usuario, no junto al programa, así que puedes
copiar versiones nuevas de la carpeta sin perderla:

- Windows: `%APPDATA%\kvmlink\config.json` (y el registro en `kvmlink.log`)
- Mac: `~/Library/Application Support/kvmlink/config.json`

La contraseña no se guarda, solo una clave derivada de ella.

### Desde la terminal

Sin interfaz, con argumentos. Ejemplo con el Mac a la izquierda y el PC a la derecha.
`--edge` indica en qué lado de *esta* pantalla está el *otro* equipo (en cada máquina se pone
el lado opuesto); el otro equipo se coloca centrado en ese lado.

En el PC (espera conexiones; la primera vez acepta el aviso del Firewall para **redes privadas**):
```bash
python kvmlink.py --listen --edge left
```

En el Mac (encuentra al PC solo):
```bash
python3 kvmlink.py --connect auto --edge right
```

Ambos piden la misma contraseña (mín. 6 caracteres). También puedes pasarla con
`--password` o con la variable de entorno `KVMLINK_PASSWORD`. Si el descubrimiento
automático no funciona en tu red (algunos routers bloquean el broadcast), usa la IP:
`--connect 192.168.1.50`.

### Opciones

| Opción | Para qué |
|---|---|
| `--speed 1.5` | El ratón va lento/rápido en el otro equipo (p.ej. pantalla 4K en el PC). Se aplica en el lado que controla. |
| `--invert-scroll` | El scroll recibido va al revés (por el "desplazamiento natural" del Mac). Se aplica en el lado que recibe. |
| `--iso-swap` | (Mac) Las teclas `º` y `<` salen intercambiadas. |
| `--no-swap-cmd-ctrl` | Mantener Cmd y Ctrl sin intercambiar. |
| `--no-clipboard` | No compartir el portapapeles. |
| `--port 24800` | Puerto TCP (el descubrimiento usa UDP 24801). |

## Limitaciones conocidas

- Las pantallas de cada equipo se tratan como un bloque: en la disposición se mueven
  juntas las del otro equipo, no una a una. Las del PC están en píxeles y las del Mac en
  puntos, así que en el dibujo pueden no tener exactamente su tamaño físico relativo.
- Las actualizaciones automáticas funcionan a partir de la versión 1 (la primera con esta
  función): esa hay que copiarla a mano a los dos equipos. Con versiones anteriores, kvmlink
  avisa en la ventana y las funciones nuevas no están disponibles.
- En el Mac, el reinicio tras actualizarse lo hace `kvmlink.command`; si arrancaste kvmlink
  de otra forma, ciérralo y vuelve a abrirlo tras actualizar.
- Con un botón pulsado solo se cruza si lo que arrastras son archivos; arrastrar una ventana
  o una selección de texto no pasa al otro equipo. Los archivos arrastrados se guardan en la
  carpeta de destino, no en la carpeta o aplicación concreta sobre la que sueltas.
- En Windows, para controlar ventanas que se ejecutan como administrador (p.ej. el
  Administrador de tareas), lanza kvmlink también como administrador.
  Ctrl+Alt+Supr nunca se puede reenviar.
- En el Mac, los campos de contraseña activan "Secure Input", que bloquea la captura de
  teclado de cualquier programa mientras están enfocados.

## Pruebas

`python test_core.py` levanta dos instancias simuladas conectadas por loopback (con red y
cifrado reales) sin tocar tu ratón real. Comprueba el cruce de bordes y el reenvío de eventos,
el cambio Cmd/Ctrl, la disposición de pantallas y su sincronización, el atajo de emergencia,
la recuperación si se cae la conexión, arrastrar archivos en los dos sentidos, copiar y pegar
archivos en los dos sentidos y que los nombres que llegan no puedan salirse de la carpeta de
destino. `python test_failsafe_win.py` prueba en Windows que el cursor se recupera si kvmlink
se cierra o falla (lo oculta un par de segundos).

## Archivos

- `kvmlink.py` — lógica principal, máquina de estados, conexión y modo terminal.
- `gui.py` + `ui.html` — interfaz: servidor local en 127.0.0.1 y la página que se abre.
- `layout.py` — geometría de la disposición de pantallas (cruce de bordes, ajuste al soltar).
- `files.py` — envío y recepción de archivos por la conexión cifrada.
- `dragdrop_win.py` — ventana invisible que detecta en Windows qué archivos se arrastran.
- `clipboard_win.py` — "archivos virtuales" en el portapapeles de Windows para pegar archivos
  copiados en el otro equipo sin transferirlos hasta pegar.
- `version.py` — número de versión (`BUILD`, hay que subirlo con cada versión nueva) y huella
  de los archivos del programa.
- `update.py` — pedir, comprobar e instalar la versión del otro equipo.
- `install.py` (+ `instalar.bat` / `instalar.command`) — instalar como programa: accesos
  directos, `kvmlink.app`, inicio automático y desinstalación.
- `tray_win.py` — icono en la bandeja de Windows (el de la barra de menús del Mac está en
  `platform_mac.py`).
- `icon.py` — el icono de kvmlink, dibujado por código (`.ico` y `.icns` se generan al instalar).
- `net.py` — canal TCP cifrado y descubrimiento por broadcast.
- `keymap.py` — tabla de teclas físicas Windows ⇄ macOS.
- `platform_win.py` — hooks de bajo nivel y `SendInput` (Windows).
- `platform_mac.py` — `CGEventTap` y `CGEventPost` (macOS).
