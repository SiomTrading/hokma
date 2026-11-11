# Extensiones

Las extensiones le dan capacidades nuevas a tus agentes: plugins de un marketplace y servidores MCP. Tú decides qué
está activo.

## Ver lo instalado

Abre Ajustes y elige **Extensiones**. Cada plugin muestra su marketplace y un interruptor. Los servidores MCP se
listan con el programa que ejecutan.

## Encender o apagar un plugin

Pulsa su interruptor. Hokma muestra qué se va a activar y sus riesgos, y te pide confirmar. Tu confirmación queda
guardada como una aprobación y cambia tu archivo de extensiones.

## Los agentes instalan por ti

Un agente puede instalar y quitar plugins, skills y servidores MCP desde el chat (`claude plugin install`,
`claude mcp add`, o un archivo de skill en tu carpeta de trabajo). Hokma ya tiene Node, Bun, npm, npx y uv en el PATH
del agente y de los hooks de los plugins, así que un plugin que necesita alguno arranca. Un servidor MCP se ejecuta
con tus propios permisos: pregúntale al agente qué instaló. `hokma ext request` sigue existiendo: la solicitud espera
arriba en la sección Extensiones con Aprobar y Rechazar.

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
