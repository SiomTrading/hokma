# Proyectos

Un proyecto es una carpeta dentro de tu carpeta de trabajo, con su propio historial de Git. Cada idea, estrategia o
investigación tiene su proyecto para que el contexto no se mezcle.

## Crear un proyecto

En el panel derecho pulsa el botón más, escribe un nombre y elige un tipo: IPS, patrimonio, estrategia,
investigación, robot u otro. Hokma crea la carpeta `projects/<nombre>/` con estas subcarpetas:

`contexto`, `documentos`, `imagenes`, `audios`, `codigo`, `resultados`

y guarda un primer commit de Git.

## Cambiar de proyecto

Haz clic en un proyecto de la lista. Pasa a ser el proyecto activo: el chat lo muestra en la cabecera y los agentes
trabajan en su carpeta. También puedes escribir `/proyecto` y el nombre en el chat.

## Agregar archivos

Arrastra archivos a la zona "Arrastra archivos de contexto a este proyecto" o al árbol. Cada archivo va a la
subcarpeta que corresponde a su tipo y Hokma guarda un commit de Git.

## Agregar un enlace

Usa "Agregar por enlace" para guardar una dirección `https://` con un título. El enlace se escribe en
un archivo Markdown dentro de la carpeta `contexto` del proyecto. Hokma no descarga la página.

## El árbol del proyecto

"Contenido del proyecto" muestra carpetas y archivos con su tamaño. Todo está dentro de la carpeta del proyecto: las
rutas que intentan salir de ella se rechazan.
