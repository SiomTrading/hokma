# Solución de problemas

Empieza por aquí cuando algo no abre o no conecta. Cada caso es corto.

## Hokma no abre después de reinstalar

1. Pulsa Ctrl, Shift y Esc para abrir el **Administrador de tareas**.
2. Busca **Hokma.exe**, selecciónalo y pulsa **Finalizar tarea**. Hazlo con cada copia que veas.
3. Abre el instalador otra vez.
4. Cuando pregunte qué hacer, elige **Reinstalar desde cero**.
5. Si el instalador dice que un archivo está en uso, no elijas **Ignorar**. Cierra el proceso que lo usa y reintenta.

Debe verse: "Hokma quedó instalado" y el botón **Abrir Hokma ahora**. La reinstalación guarda tu fondo, tus plugins y tu base de datos y los devuelve al final. Tendrás que iniciar sesión en Claude de nuevo.

## El instalador no avanza

1. Cierra el instalador.
2. Haz clic derecho en el instalador y elige **Ejecutar como administrador**.
3. Instala en una carpeta que no esté en OneDrive: OneDrive borra archivos que Hokma necesita.

Si se repite, manda a soporte el archivo que el instalador nombra en el mensaje final.

## Codex no conecta

- **"Tu cuenta no tiene Codex habilitado".** Tu administrador de ChatGPT debe habilitar Codex. Después conecta de nuevo en Ajustes, **Motores**.
- **"No permite entrar con código de dispositivo".** Pídele al administrador que lo active, o conecta con una clave de API.
- **No se encuentra Codex en el equipo.** Aparece "No encuentro {cli} en este equipo". Vuelve a conectar desde **Motores**.

## Mac y la terminal integrada

Si la terminal no abre, Hokma dice "No se pudo iniciar la terminal" o "Esta computadora no tiene el componente de terminal necesario". Cierra Hokma con **Salir de Hokma**, ábrelo de nuevo y pulsa **Nueva terminal**. Si se repite, manda tu `engine.log` (abajo): ahí queda el motivo exacto. El instalador `Instalar-Hokma.cmd` es solo para Windows.

## Dónde está engine.log y cómo mandarlo

`engine.log` guarda el detalle real de cada falla, sin tus claves.

1. Abre Ajustes y elige **Novedades**. La pantalla de actualización muestra la ruta del log si algo falla.
2. Si no la ves, busca la carpeta `logs` dentro de la carpeta de datos de Hokma:
   - Windows: `%LOCALAPPDATA%\Hokma\logs\engine.log`
   - Mac: `~/Library/Application Support/Hokma/logs/engine.log`
3. Copia el archivo a tu escritorio.
4. Envíalo a soporte con una frase de lo que hacías cuando falló.

## Otras revisiones

- Abre Ajustes, **Estado del sistema**. Ahí está `hokma doctor` en pantalla: arregla lo que marque y pulsa **Revisar de nuevo**.
- **El chat no responde.** Revisa que tengas un motor **Conectado** en Ajustes, **Motores**.
- **Reinstalar solo cuando el doctor o el instalador lo piden.** En los demás casos solo cuesta tiempo. Antes de reinstalar, haz un respaldo o copia tu carpeta de trabajo.
