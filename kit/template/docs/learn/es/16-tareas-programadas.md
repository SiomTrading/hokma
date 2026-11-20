# Rutinas y briefing premercado

Una rutina le pide a un agente que haga algo a la hora que eliges. Se llama **Tarea programada**. Corre sin ti: no puede pedirte permisos durante la corrida.

## Crear una rutina

1. En el menú, abre **Tareas programadas**.
2. Pulsa **Nueva tarea programada**.
3. Escribe el **Nombre**.
4. Elige el **Agente**.
5. Escribe la **Instrucción**: lo que el agente hará cada vez.
6. En **Frecuencia** elige **Una vez**, **Diaria**, **Semanal** o **Cada cierto tiempo**. Completa la hora, los días o el intervalo (entre 5 minutos y 31 días).
7. Revisa la **Zona horaria**.
8. Pulsa **Crear tarea**.

Debe verse: "Tarea creada. Está apagada hasta que la enciendas."

También puedes pedirle a Hokma por el chat "crea una tarea de respaldo diario en GitHub" y él la crea. Hokma te pide permiso para crearla.

## Aprobarla

1. En la lista, pulsa el interruptor de la tarea.
2. Lee quién la ejecutará y con qué modelo. Gasta de tu plan cada vez.
3. Pulsa **Sí, encender**.

Debe verse: "Tarea encendida." y la línea "Próxima:" con la fecha. Tu confirmación queda registrada. Si cambias lo que ejecuta la tarea, queda apagada y debes aprobarla de nuevo.

## Ejecutar ahora

1. En la tarea, pulsa **Ejecutar ahora**.
2. Espera. La tarea muestra "Ejecutándose ahora".

Debe verse: "Última corrida" con su estado ("terminada" o "fallida") y los tokens usados. La próxima corrida programada no cambia. Enciende la tarea una vez antes: así la apruebas.

## Briefing premercado

Es un resumen para preparar el día, no una señal. Junta el calendario económico, las noticias y tus activos en seguimiento.

1. En **Tareas programadas**, pulsa **Agregar briefing premercado**.
2. Apruébalo con **Sí, encender**.

Debe verse: el briefing en la tarjeta Hoy del Fondo, y en Telegram si lo vinculaste. Corre de lunes a viernes. Para verlo ya, pulsa **Generar ahora** en la tarjeta Hoy. Pídele a Hokma por el chat si quieres cambiar la hora.

## Si tu computador estaba apagado

- Las rutinas solo corren con Hokma abierto.
- Si estaba apagado, al encender corre una vez lo que se perdió, si no pasaron más de 24 horas (3 horas para el briefing premercado). Hokma te avisa por Telegram.
- Si pasó más tiempo, la tarea queda como omitida y no corre sola. Úsala con **Ejecutar ahora** si la quieres.
- Hokma avisa por Telegram cuando una rutina termina, falla o se omite.
- Cerrar la ventana con la X solo la oculta: las rutinas siguen. **Salir de Hokma** las detiene.
- Para que el equipo no se suspenda mientras trabaja, enciende **Mantener Hokma trabajando** en Ajustes, **Mantener trabajando**.
- Correr siempre, sin tu computador, todavía no existe.

## Si algo falla

- **"Su agente está apagado".** Enciende el agente en Ajustes, **Agentes**.
- **La tarea hizo menos de lo pedido.** Dice "terminada, N permisos negados". Una rutina no puede pedir permisos: ajusta la instrucción.
- **No llega el briefing.** Enciende la tarea Briefing premercado y pulsa **Generar ahora**. Sin Telegram vinculado, el briefing queda solo en la tarjeta Hoy.
