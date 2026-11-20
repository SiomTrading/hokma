# Plugins básicos: claude-mem, caveman y watch

Un plugin le da una capacidad nueva a tus agentes. Se instalan desde **Extensiones**. Hokma te muestra qué va a activar y te pide confirmar antes de instalar. Los plugins solo funcionan con el motor Claude Code.

## Qué hace cada uno

- **claude-mem.** Memoria entre conversaciones: guarda lo que pasó y se lo recuerda al agente en la siguiente. Necesita un proceso de apoyo que arranca solo con tu primera conversación.
- **caveman.** Hace que los agentes respondan corto, sin relleno. Gastas menos tokens de salida. Hokma no da una cifra de ahorro: mídela con la barra "Tokens esta semana" en Ajustes, Modelos y consumo, un día antes y un día después.
- **watch.** Permite que tus agentes vean videos. Le das un enlace (YouTube y otros) o un archivo de video: el agente lo descarga, saca cuadros y la transcripción, y responde lo que le preguntes sobre el video. Si no aparece en la búsqueda, agrégalo por enlace con `bradautomates/claude-video`.

## Instalar un plugin

1. Abre Ajustes con la rueda del menú izquierdo o con Ctrl y la coma.
2. Elige **Extensiones** y abre la pestaña **Plugins**.
3. En "Marketplace de Anthropic" escribe el nombre en **Buscar un plugin**.
4. Pulsa **Instalar** en el plugin que buscas.
5. Lee lo que se activa y pulsa **Instalar** en el cuadro de confirmación.
6. Espera. La instalación puede tardar hasta 5 minutos.

Debe verse: el plugin en la lista **Instaladas**, con su interruptor encendido.

## Si no aparece en la búsqueda

1. En **Extensiones**, pulsa **Agregar por enlace**.
2. Escribe `dueño/repositorio` o pega el enlace de GitHub del plugin.
3. Si el enlace trae un solo plugin, se instala de una. Si trae varios, elige cuáles.

Debe verse: el marketplace agregado y sus plugins en la lista.

## Usarlos

Un plugin instalado actúa solo: no tienes un botón nuevo. Empieza una conversación nueva para que tus agentes lo usen. Para apagarlo sin borrarlo, usa su interruptor. Para quitarlo, desinstálalo desde la misma lista.

## Si algo falla

- **"No se pudo instalar {nombre}" con un motivo.** Lee el motivo y pulsa **Reintentar**. Si no hay Internet, la instalación no avanza.
- **El plugin dice "Solo Claude Code".** Tu motor activo es otro. Cambia a Claude en Ajustes, **Motores**.
- **claude-mem dice "Revisar".** Los archivos están, pero su proceso de apoyo no responde. Cierra y abre Hokma, y empieza una conversación nueva.
- **No aparece en el Marketplace de Anthropic.** Usa **Agregar por enlace**.
