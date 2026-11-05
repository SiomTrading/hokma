# Projects

A project is a folder inside your working folder with its own Git history. Each idea, strategy or piece of research
gets its own project so the context never mixes.

## Create a project

In the right panel press the plus button, write a name and choose a type: IPS, wealth, strategy, research, robot or
other. Hokma creates the folder `projects/<name>/` with these subfolders:

`contexto`, `documentos`, `imagenes`, `audios`, `codigo`, `resultados`

and saves a first Git commit.

## Switch project

Click a project in the list. It becomes the active project: the chat shows it in the header and the agents work in
its folder. You can also type `/proyecto` followed by the name in the chat.

## Add files

Drag files onto the zone "Drag context files into this project" or onto the tree. Each file goes to the subfolder
that matches its type and Hokma saves a Git commit.

## Add a link

Use "Add by link" to save an `https://` address with a title. The link is written to a Markdown file inside the `contexto` folder of the project.
Hokma does not download the page.

## The project tree

"Project contents" shows folders and files with their size. Everything is inside the project folder: paths that
try to leave it are rejected.
