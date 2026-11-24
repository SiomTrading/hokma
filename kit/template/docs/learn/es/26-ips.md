# Tu IPS por chat

El IPS (Declaración de Política de Inversión) es la página que dice cuánto del dinero del fondo puedes perder y bajo qué reglas. La escribes una vez, en el chat, y Hokma la guarda como versiones que puedes leer cuando quieras. Hokma documenta tus propias respuestas. Nunca recomienda en qué invertir.

## Créalo

1. Abre el chat y escribe: **create my IPS**.
2. Hokma pregunta un bloque a la vez: perfil, activos, deudas, gastos y reserva, ingresos, conducta, cuentas de fondeo, bolsillos, objetivos y restricciones.
3. Después de cada bloque muestra una tabla "What I wrote down" y los números que ese bloque movió. Responde **ok** o dile qué corregir.
4. Al final muestra un resumen y pregunta si lo adoptas. Responde **yes** y el IPS queda como versión 1.

Debes ver: un proyecto **IPS** con dos archivos, `IPS.md` (el texto) y `ips.json` (los datos, con los límites que leen las mesas).

Si se corta la sesión, los bloques que confirmaste quedan como borrador. La próxima vez Hokma dice "You have a draft" y continúa.

## Actualízalo

Escribe **update my IPS**. Hokma muestra qué bloques están completos y cuáles siguen en "To be defined", pregunta qué quieres cambiar, muestra los números antes y después, y guarda una versión nueva. Cada versión es un commit en el proyecto IPS.

## Qué debes saber

- Todo va en USD. Si respondes en otra moneda, Hokma te pide la tasa de cambio, su fuente y su fecha. La escribes tú; Hokma no la busca.
- Hokma nunca sugiere cifras. Una respuesta que dejas vacía queda en "To be defined", que no es lo mismo que 0.
- Los números salen siempre del método de capacidad de riesgo de Hokma, nunca de memoria. Si una cifra no cuadra, Hokma te muestra los dos números y te pide confirmar.
- Las cuentas de fondeo de una empresa de fondeo no hacen parte de tu patrimonio. Una pérdida dentro de una cuenta de fondeo no es pérdida de la casa: la casa pierde lo que pagó por la cuenta.
- Para identificar una cuenta, da solo los últimos 4 dígitos.

Este es material educativo, no asesoría de inversión. Operar implica riesgo de pérdida, incluido el capital que pones en riesgo. Hokma no promete ni garantiza rendimientos.
