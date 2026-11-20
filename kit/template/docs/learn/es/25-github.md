# GitHub bien explicado

GitHub es un sitio en Internet donde guardas copias de tus carpetas. Hokma lo usa para respaldar tu trabajo y para instalar plugins y skills. No necesitas saber programar.

## Qué es GitHub y qué es un repositorio

Un **repositorio** es una carpeta con memoria. Guarda tus archivos y también cada versión que fuiste haciendo. Puede ser **privado** (solo tú lo ves) o **público** (cualquiera lo ve).

**Git** es la herramienta que lleva esa memoria en tu computador. **GitHub** es el sitio donde queda una copia en línea. No son lo mismo: Git es la herramienta y GitHub es uno de los lugares donde guardas el resultado.

Para tus estrategias y tus notas usa siempre un repositorio **privado**.

## Crear la cuenta

1. Entra a github.com y pulsa **Sign up**.
2. Escribe un correo al que tengas acceso, una contraseña y un nombre de usuario.
3. Confirma el correo con el código que te llega.

Debe verse: tu página de inicio de GitHub con tu usuario arriba a la derecha.

GitHub define sus planes y límites, y pueden cambiar. Hokma no promete precios.

## Crear un repositorio privado

1. En GitHub, pulsa el signo **+** de arriba y elige **New repository**.
2. Escribe un nombre, por ejemplo `mi-fondo-respaldo`.
3. Marca **Private**.
4. Pulsa **Create repository**.

Debe verse: la página del repositorio, vacía, con una dirección como `https://github.com/tu-usuario/mi-fondo-respaldo`.

## Conectar GitHub en Hokma

La conexión está en Ajustes, **Respaldo y restaurar**. No está en la pantalla **Conexiones**: esa es para datos de mercado y Gmail.

1. Abre Ajustes y elige **Respaldo y restaurar**.
2. En "1. Conecta tu GitHub", pulsa **Conectar GitHub**.
3. Hokma muestra un código. Pulsa **Abrir GitHub**.
4. En GitHub escribe el código y autoriza a Hokma. Mientras tanto Hokma dice "Esperando tu autorización".
5. Pulsa **Elegir repo en GitHub**. Marca "Only select repositories" y escoge tu repositorio privado.
6. Vuelve a Hokma. Si la lista no se actualiza, pulsa **Actualizar lista**.

Debe verse: "Conectado como" con tu usuario y "Repositorio:" con el nombre elegido.

Hokma solo ve el repositorio que elegiste. El resto de tu GitHub queda fuera. Para cambiarlo, pulsa **Cambiar repo**. Para cortar la conexión, pulsa **Desconectar**.

## Para qué lo usa Hokma

- **Respaldo.** Hokma guarda copias cifradas de tus proyectos en tu repositorio privado. Los pasos completos están en el tema de Respaldo, Papelera y puntos de restauración.
- **Plugins.** Puedes instalar un plugin desde su repositorio.
- **Skills.** Puedes instalar una skill desde su repositorio.

Los plugins y las skills por enlace no necesitan que conectes tu cuenta. Solo el respaldo la necesita.

## Instalar un plugin pegando la dirección

1. Abre Ajustes y elige **Extensiones**.
2. En la pestaña **Plugins**, pulsa **Agregar por enlace**.
3. En **Repositorio de GitHub** escribe `dueño/repositorio` o pega el enlace, por ejemplo `https://github.com/dueño/repositorio`.
4. Pulsa **Agregar**.
5. Si el repositorio trae un solo plugin, se instala y se activa de una. Si trae varios, elige cuáles.

Debe verse: "quedó instalado y activo", o el marketplace agregado con sus plugins. Lee siempre lo que el plugin activa antes de confirmar.

## Instalar una skill pegando la dirección

1. En **Extensiones**, pulsa **Agregar skill por enlace**.
2. En **Enlace de GitHub de la skill** pega la dirección del repositorio, o la de la carpeta de una skill dentro de él.
3. Si ya tienes una skill con ese nombre y quieres cambiarla, marca la casilla de reemplazar. La anterior va a la Papelera de Hokma.
4. Pulsa **Instalar skill**.

Debe verse: "Skill instalada" con el nombre. Queda disponible de inmediato. Si el enlace era un plugin, Hokma te lo avisa y lo deja en tu lista de extensiones.

## Qué es un commit y qué es un push

- **Commit.** Es una foto de tus archivos en un momento, con un mensaje corto. Por ejemplo "Primera versión". Se guarda en tu computador.
- **Push.** Es enviar esos commits a GitHub. Así la copia en línea se pone al día.

Con el respaldo de Hokma no haces ninguno de los dos a mano. Hokma lo hace por ti. Cuando arrastras un archivo a un proyecto, Hokma hace el commit solo. Si más adelante quieres subir un proyecto tú mismo, hazlo desde la terminal integrada, con tu repositorio privado vacío.

## Si algo falla

- **"El código venció. Conecta de nuevo."** Pulsa **Conectar GitHub** otra vez y escribe el código nuevo.
- **"GitHub rechazó la autorización."** Cancelaste o negaste en GitHub. Si fue un error, conecta de nuevo.
- **"Sin conexión con GitHub. Tu cuenta sigue conectada."** Revisa tu Internet y pulsa **Reintentar**.
- **"Todavía no hay repos."** Pulsa **Elegir repo en GitHub**, marca "Only select repositories" y escoge tu repositorio. Si no tienes uno, pulsa **Crear un repo privado nuevo** y vuelve.
- **"Ese repositorio no sirve para el respaldo".** Debe ser privado y estar entre los que le diste a Hokma. Usa **Cambiar repo**.
- **"Hokma ya no puede entrar a tu repositorio de respaldo".** Lo borraste o le quitaste el permiso. Pulsa **Cambiar repo** y elige uno.
- **"Este computador no tiene un lugar seguro para guardar el acceso a GitHub".** Tu equipo no tiene almacén de credenciales. No se guardó nada.
- **Ves "Vuelve a conectar GitHub y elige tu repo".** Tu conexión es de una versión anterior y daba acceso a toda tu cuenta. Conecta de nuevo. Puedes pulsar **Quitar el acceso anterior en GitHub**.
- **Pegaste un enlace y dice que el formato no sirve.** Escribe `dueño/repositorio` o `https://github.com/dueño/repositorio`.
- **"No se pudo instalar" con un motivo.** Lee el motivo. Si no hay Internet, la instalación no avanza. Pulsa **Reintentar**.
