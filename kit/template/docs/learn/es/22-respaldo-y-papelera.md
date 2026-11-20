# Respaldo, Papelera y puntos de restauración

Hokma nunca borra tus archivos. Tienes tres redes de seguridad: la Papelera, los puntos de restauración y el respaldo en GitHub.

## La Papelera

Lo que un agente quiso borrar llega a la Papelera. Nada sale de ahí si no lo decides tú.

1. En Ajustes, abre **Papelera**.
2. Busca el elemento. Verás su ruta original y su motivo.
3. Pulsa **Restaurar**.

Debe verse: "Restaurado en" con la ruta. Para borrar un elemento de verdad, pulsa **Borrar definitivamente**. **Vaciar papelera** borra todo para siempre y no se puede deshacer.

## Puntos de restauración

En el modo "Hacer todo sin preguntar", Hokma guarda una foto de la carpeta del proyecto antes de cada turno. Restaurar nunca borra nada de lo que existe ahora. Necesitas que la carpeta tenga un repositorio Git: si no, Hokma dice que no hay puntos de restauración. No encontré el nombre exacto del menú donde se abren: busca "Puntos de restauración" dentro de tu proyecto.

## Respaldo en GitHub

1. En Ajustes, abre **Respaldo y restaurar**.
2. En "1. Conecta tu GitHub", pulsa **Conectar GitHub**. Escribe en GitHub el código que Hokma muestra.
3. Elige un repositorio privado. Hokma solo verá ese.
4. En "2. Clave de recuperación", pulsa **Crear mi clave**. Cópiala o guárdala en un archivo, en un lugar seguro. Se muestra una sola vez y nadie puede recuperarla por ti.
5. Escribe los últimos 4 caracteres y pulsa **Ya la guardé**.
6. En "3. Respaldo", pulsa **Respaldar ahora**.

Debe verse: el estado **Al día** y "Último respaldo". Los respaldos van cifrados y se repiten solos. Lo que tengas en tu disco fuera de Hokma es responsabilidad tuya.

## Restaurar en un equipo nuevo

1. En **Respaldo y restaurar**, conecta GitHub.
2. Elige un respaldo en **Respaldos disponibles**.
3. Escribe tu **Clave de recuperación**.
4. Pulsa **Restaurar**. Hokma aplica el respaldo cuando se reinicia.
5. Vuelve a conectar tu motor, Telegram, la carpeta y los proyectos que Hokma indique.

## Si algo falla

- **"El código venció".** Pulsa **Conectar GitHub** de nuevo.
- **"Esos 4 caracteres no son los de tu clave".** Revisa que copiaste bien los últimos 4.
- **Sin la clave no se restaura.** Guárdala antes de necesitarla.
- **"El respaldo no se pudo aplicar al reiniciar".** Tus datos de antes siguen intactos.
