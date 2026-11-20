# Telegram: tu canal desde el celular

Con Telegram hablas con Hokma desde el celular y recibes sus avisos. Creas tu propio bot, lo conectas y, si quieres, lo usas en un grupo con temas.

## Crear el bot con BotFather

1. En Telegram busca **@BotFather** y ábrelo.
2. Escribe `/newbot`.
3. Elige un nombre para tu bot.
4. Elige un usuario que termine en `bot`.
5. Copia el token que te entrega BotFather.

Debe verse: un mensaje con el token. Es una clave: quien lo tiene controla el bot. No lo compartas.

## Conectar el bot a Hokma

1. Abre Ajustes y elige **Telegram**.
2. Pega el token en **Token del bot**.
3. Pulsa **Conectar**. El estado pasa a **Esperando el código**.
4. En Telegram, escríbele cualquier mensaje a tu bot.
5. El bot responde con un código. Escríbelo en **Código** en Hokma.
6. Pulsa **Aprobar**.

Debe verse: el estado **Vinculado** y "Chat de" con tu nombre.

## Grupo con temas

Primero vincula tu chat privado. Después:

1. En Telegram crea un grupo y activa **Temas** en su configuración.
2. Agrega tu bot al grupo como administrador, con el permiso de gestionar temas.
3. Escribe `/vincular` en el grupo. El bot responde con un código.
4. En Hokma, en Ajustes, **Telegram**, sección **Grupo con temas**, escribe el código en **Código del grupo**.
5. Pulsa **Aprobar**.

Debe verse: **Grupo vinculado** y **Tema Avisos ok**. Cada tema del grupo es una conversación aparte con Hokma. Solo se puede vincular un grupo, y solo responde a tu cuenta.

## Avisos

El tema **Avisos** recibe lo que Hokma te avisa: rutinas terminadas, fallidas u omitidas, y los permisos con botones para aprobar o negar. Si estás en el modo "Hacer todo sin preguntar", los permisos no llegan a Telegram.

## Si algo falla

- **"Eso no es un token de bot de Telegram".** Copia el token completo desde BotFather, sin espacios.
- **"Este computador no tiene un lugar seguro para guardar el token".** Tu equipo no tiene almacén de credenciales. No se guardó nada.
- **Aparece "Falta permiso".** El grupo no tiene Temas activados o el bot no es administrador con permiso de gestionar temas. Corrígelo en Telegram, pulsa **Desvincular grupo**, escribe `/vincular` otra vez y aprueba. También puedes pulsar **Reintentar el tema Avisos**.
- **"Sin conexión con Telegram".** Tu chat sigue vinculado. Hokma lo intenta solo, o pulsa **Reconectar**.
- **Mensajes en espera.** Aparece "N mensajes esperan para llegar a Telegram". Llegan cuando vuelva la conexión.
- **Quieres empezar de cero.** Pulsa **Desvincular**: Hokma olvida el chat, el grupo y el token. Revoca también el bot en BotFather.
