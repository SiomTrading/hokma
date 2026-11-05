# Extensiones

Las extensiones le dan capacidades nuevas a tus agentes: plugins de un marketplace y servidores MCP. Tú decides qué
está activo.

## Ver lo instalado

Abre Ajustes y elige **Extensiones**. Cada plugin muestra su marketplace y un interruptor. Los servidores MCP se
listan con el programa que ejecutan.

## Encender o apagar un plugin

Pulsa su interruptor. Hokma muestra qué se va a activar y sus riesgos, y te pide confirmar. Tu confirmación queda
guardada como una aprobación y cambia tu archivo de extensiones.

## Solicitudes de los agentes

Un agente puede pedir un plugin o un servidor MCP con `hokma ext request`. La solicitud aparece arriba en la sección
Extensiones con Aprobar y Rechazar. Nada cambia hasta que apruebes. Un servidor MCP se ejecuta fuera del aislamiento
con tus propios permisos: lee su comando y sus argumentos antes de aprobar.

## Marketplace de Anthropic

Pulsa "Marketplace de Anthropic" para ver su lista de plugins, leída de su repositorio oficial. Agrégalo a tu lista y
enciende los plugins que quieras. Cada uno pide tu confirmación.

## Agregar un marketplace por enlace

Pulsa "Agregar por enlace" y escribe una dirección de GitHub como `dueño/repositorio`. El marketplace se agrega a tu
lista. Sus plugins quedan apagados hasta que los enciendas.

## Desde la terminal

- `hokma ext list` muestra lo que tienes.
- `hokma ext requests` lista las solicitudes pendientes.
- `hokma ext approve <id>` y `hokma ext reject <id>` resuelven una solicitud.
