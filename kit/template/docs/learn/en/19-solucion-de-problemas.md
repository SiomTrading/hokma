# Troubleshooting

Start here when something does not open or does not connect. Each case is short.

## Hokma does not open after reinstalling

1. Press Ctrl, Shift and Esc to open **Task Manager**.
2. Find **Hokma.exe**, select it and press **End task**. Do it with every copy you see.
3. Open the installer again.
4. When it asks what to do, choose **Reinstall from scratch**.
5. If the installer says a file is in use, do not choose **Ignore**. Close the process using it and retry.

You should see: "Hokma is installed" and the button **Open Hokma now**. The reinstall saves your fund, your plugins and your database and returns them at the end. You will have to sign in to Claude again.

## The installer does not move

1. Close the installer.
2. Right-click the installer and choose **Run as administrator**.
3. Install in a folder that is not in OneDrive: OneDrive deletes files Hokma needs.

If it repeats, send support the file the installer names in its final message.

## Codex does not connect

- **"Your account does not have Codex enabled".** Your ChatGPT admin must enable Codex. Then connect again in Settings, **Engines**.
- **"Does not allow signing in with a device code".** Ask the admin to turn it on, or connect with an API key.
- **Codex is not found on the computer.** You see "I cannot find {cli} on this computer". Connect again from **Engines**.

## Mac and the built-in terminal

If the terminal does not open, Hokma says "The terminal could not start" or "This computer is missing the terminal component". Close Hokma with **Shut down Hokma**, open it again and press **New terminal**. If it repeats, send your `engine.log` (below): the exact reason is written there. The `Instalar-Hokma.cmd` installer is for Windows only.

## Where engine.log is and how to send it

`engine.log` keeps the real detail of each failure, without your keys.

1. Open Settings and choose **What is new**. The update screen shows the path of the log if something fails.
2. If you do not see it, look for the `logs` folder inside Hokma's data folder:
   - Windows: `%LOCALAPPDATA%\Hokma\logs\engine.log`
   - Mac: `~/Library/Application Support/Hokma/logs/engine.log`
3. Copy the file to your desktop.
4. Send it to support with a sentence about what you were doing when it failed.

## Other checks

- Open Settings, **System status**. `hokma doctor` is there on screen: fix what it marks and press **Check again**.
- **The chat does not answer.** Check that you have an engine **Connected** in Settings, **Engines**.
- **Reinstall only when the doctor or the installer asks.** Otherwise it only costs time. Before reinstalling, make a backup or copy your working folder.
