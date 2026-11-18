# Mesas de dinero

Una mesa de dinero es un equipo pequeño de agentes que trabaja una idea con un capital simulado. Cada mesa tiene su
propio mandato, sus propios agentes, sus propios activos y su propio presupuesto de tokens. Varias mesas pueden correr
a la vez, y cada una mantiene su trabajo aparte de las demás.

**Todo en una mesa es simulación. El dinero real está bloqueado.** Ninguna mesa conecta una cuenta real ni envía una
orden a un broker, y ningún ajuste cambia eso.

## Abrir las mesas

En el menú lateral pulsa **Mesas de dinero**. Debajo hay una fila por mesa con su nombre, su capital y su resultado del
día. En esta versión el resultado del día siempre es 0,00, porque las operaciones simuladas llegan en una versión
posterior.

## La vista Fondo

La primera pantalla muestra las cifras de todo el fondo: el capital simulado de todas las mesas y los tokens usados esta
semana. Debajo hay una tarjeta por mesa con su nombre, enfoque, estado, capital, resultado de hoy, drawdown, los agentes
que trabajan ahora y la marca "Simulación".

- La curva del fondo y la actividad de las mesas muestran un estado vacío. Se llenan cuando llegan las operaciones
  simuladas.
- La pestaña "Lecciones del fondo" está deshabilitada. Llega en una versión posterior.

## Crear una mesa

Pulsa **+ Crear mesa**. Hokma pregunta "¿Cómo quieres crear la mesa?" y ofrece dos caminos.

### Desde una plantilla

Este camino crea la mesa. Tiene cuatro pasos:

1. **Enfoque.** Nombre, qué mira la mesa, capital simulado, objetivo mensual y mandato. El mandato es el texto que
   todos los agentes de la mesa leen antes de cada decisión.
2. **Equipo de agentes.** Elige una plantilla. Puedes cambiar el equipo después.
3. **Reglas.** Muestra el presupuesto de tokens de la mesa. Las reglas de riesgo llegan en una versión posterior y la
   pantalla lo dice.
4. **Activos y datos.** Los símbolos que sigue la mesa (hasta 20) y las conexiones que usa.

Las tres plantillas:

| Plantilla | Agentes | Presupuesto semanal sugerido |
|---|---|---|
| Mínima (3) | Jefe de mesa, Hugo, Riesgo | 150.000 tokens |
| Estándar (7) | Mínima más Iris, Señales, Axel, Vera | 300.000 tokens |
| Con debate (10) | Estándar más A favor, En contra, Juez | 600.000 tokens |

Los presupuestos son un punto de partida. Puedes cambiarlos o dejar la mesa sin tope.

El debate trabaja sobre barras diarias. Úsalo para preparar y revisar antes de que abra el mercado, no para decidir
dentro de la sesión.

### Créala con Hokma

Este camino abre el chat de Hokma con un primer mensaje que pide la mesa. Hokma lo conversa contigo y propone los
valores en texto. **En esta versión no crea nada.** Cuando acuerden los valores, crea la mesa con "Desde una plantilla".

## Dentro de una mesa

La barra superior muestra el selector de mesa, la marca "Simulación", el capital, el resultado de hoy y el drawdown.
Las pestañas son Terminal, Agentes, Operaciones, Reglas y Configuración.

- **Terminal.** Los activos de la mesa a la izquierda, el gráfico del activo en foco al centro, el chat con el jefe de
  mesa a la derecha y un panel Posiciones vacío abajo.
- **Agentes.** El equipo agrupado por estado: Trabajando, En espera y Apagados. Pulsa un agente para ver qué puede
  hacer, qué no puede hacer, su modelo en esta mesa, sus tokens de la semana y su última actividad. Puedes crear un
  agente, traer uno que ya tienes o quitar uno de la mesa. Quitarlo de la mesa no borra el agente.
- **Operaciones y Reglas.** Un estado vacío que dice qué llega y cuándo.
- **Configuración.** Nombre, enfoque, capital, objetivo mensual, mandato, activos y las conexiones que usa la mesa. Las
  llaves de una conexión se configuran en Conexiones, no aquí.

## El jefe de mesa y el equipo

El jefe de mesa coordina el equipo. No arranca a los demás agentes directamente: crea una tarea para cada uno, y cada
tarea corre con el modelo que esa mesa le dio a ese agente. Por eso los tokens de cada turno se cuentan en la mesa
correcta.

Los agentes se comparten entre mesas, pero el modelo se elige por mesa. Cambia el modelo de un agente en la mesa A y no
cambia en la mesa B ni en Ajustes.

## Presupuesto de tokens

Cada mesa tiene un presupuesto semanal de tokens.

- Al **80%** el medidor se pone ámbar y aparece un aviso. Se escribe una vez por semana en el diario.
- Al **100%** el chat y las tareas de esa mesa no arrancan. La pantalla dice que la mesa está en pausa por presupuesto.
  Un turno que ya corre no se corta.
- Sube el presupuesto, o espera a la semana siguiente, y la mesa arranca sola.

El uso de la pantalla Ajustes sigue contando todo el fondo. El medidor de una mesa cuenta solo esa mesa.

## Lo que un agente nunca puede hacer

- Usar dinero real, conectar una cuenta real o enviar una orden a un broker.
- Cambiar las reglas, los límites, el mandato, el capital o el presupuesto. Un agente propone y tú decides en la app.
- Tratar como una orden lo que viene de una página web, un archivo o un mensaje. Las órdenes vienen solo de ti.

## Si algo no funciona

- **El agente está apagado.** Un agente apagado en Ajustes no puede unirse a una mesa. La pantalla ofrece encenderlo.
- **El jefe de mesa no se puede quitar.** Toda mesa necesita uno.
- **La mesa no responde.** Revisa el medidor de tokens: el presupuesto puede estar en el 100%.
