# Conectar un motor de IA

El motor es el programa de IA con el que conversa Hokma. Puedes conectar Claude, ChatGPT (Codex) o Gemini, y cambiar cuando quieras. Sin motor conectado, el chat no responde. El resto de Hokma sí funciona.

## Conectar Claude (recomendado)

1. Abre Ajustes con la rueda del menú izquierdo o con Ctrl y la coma.
2. Elige **Motores**.
3. En la tarjeta de Claude pulsa **Conectar Claude**.
4. Inicia sesión con tu cuenta de Claude en la ventana que se abre.
5. Vuelve a Hokma y pulsa **Probar**.

Debe verse: el estado **Conectado** y el mensaje "Claude responde" con los segundos que tardó.

## Conectar ChatGPT (Codex)

1. En Ajustes, **Motores**, busca la tarjeta **ChatGPT (Codex)**.
2. Pulsa **Entrar con mi cuenta de ChatGPT**.
3. Hokma muestra un enlace y un **Código de dispositivo**.
4. Abre el enlace en tu navegador e ingresa el código.
5. Inicia sesión con tu cuenta de ChatGPT y autoriza.
6. Vuelve a Hokma y pulsa **Probar**.

Debe verse: **Conectado** en la tarjeta.

## Conectar Gemini

1. En Ajustes, **Motores**, busca la tarjeta **Gemini**.
2. Pulsa **Conectar Gemini**.
3. Inicia sesión con tu cuenta de Google y autoriza.
4. Pulsa **Probar**.

Debe verse: **Conectado**. El nivel gratuito de Google tiene cupo limitado.

## Conectar con una clave de API

Si tienes una clave de otro proveedor, usa la tarjeta **Clave de API de cualquier proveedor**: elige el **Proveedor**, pega la **Clave** y pulsa **Conectar con mi clave**. Para un servicio compatible con OpenAI, completa también la **URL base**. La clave nunca vuelve a mostrarse.

## Elegir el motor activo

1. En Ajustes, **Motores**, ubica el motor que quieres usar.
2. Pulsa **Usar este motor**.

Debe verse: la marca **Activo** en ese motor. Puedes tener varios conectados y cambiar cuando quieras con **Conectar otro motor** y **Usar este motor**.

## Elegir el nivel y el modelo

Cada motor tiene tres niveles: **Rápido** (tareas simples), **Equilibrado** (el trabajo diario) y **Potente** (lo difícil). Un nivel más profundo es más lento y gasta más de tu plan.

1. En el chat, abre el menú **Nivel** y elige uno.
2. Para escoger el modelo exacto de cada nivel, pulsa **Modelos** en la tarjeta del motor.
3. Deja **Automático** para que Hokma use el más reciente, o elige uno de la lista.
4. Pulsa **Guardar**.

Debe verse: el aviso "Nivel: Equilibrado" (o el que elegiste) en el chat.

## Si algo falla

- **Sesión vencida o No instalado (Claude).** Pulsa **Conectar Claude** otra vez. Si dice "No se pudo comprobar", pulsa **Volver a comprobar**: no significa que la sesión haya vencido.
- **"Tu cuenta no tiene Codex habilitado".** Tu administrador de ChatGPT debe habilitar Codex. Después conecta de nuevo.
- **"Tu espacio no permite entrar con código de dispositivo".** Pídele al administrador que lo active en los permisos del espacio, o conecta con una clave de API.
- **El proveedor no atiende desde tu ubicación.** Pulsa **Probar de nuevo** más tarde o **Conectar otro motor**.
- **Rechazó la sesión o la clave.** Revisa que la clave esté completa y vigente, y vuelve a conectar en Ajustes, Motores.
- **Se acabó la cuota.** Prueba otro nivel u otro motor.
- **El modelo que elegiste ya no existe.** Elige otro en **Modelos** o déjalo en **Automático**.
- **Los plugins no funcionan con ChatGPT ni Gemini.** Los plugins solo funcionan con Claude Code.
- **Nada de lo anterior sirve.** Mira el tema "Solución de problemas" para mandar tu `engine.log`.
