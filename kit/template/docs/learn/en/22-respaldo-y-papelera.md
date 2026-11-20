# Backup, Trash and restore points

Hokma never deletes your files. You have three safety nets: the Trash, the restore points and the backup on GitHub.

## The Trash

What an agent wanted to delete goes to the Trash. Nothing leaves it unless you decide.

1. In Settings, open **Trash**.
2. Find the item. You see its original path and its reason.
3. Press **Restore**.

You should see: "Restored to" with the path. To really delete an item, press **Delete for good**. **Empty trash** deletes everything for good and cannot be undone.

## Restore points

In the "Do everything without asking" mode, Hokma saves a snapshot of the project folder before each turn. Restoring never deletes anything that exists now. The folder needs a Git repository: if not, Hokma says there are no restore points. I did not find the exact name of the menu that opens them: look for "Restore points" inside your project.

## Backup on GitHub

1. In Settings, open **Backup and restore**.
2. In "1. Connect your GitHub", press **Connect GitHub**. Type on GitHub the code Hokma shows.
3. Choose a private repository. Hokma will only see that one.
4. In "2. Recovery key", press **Create my key**. Copy it or save it in a file, in a safe place. It is shown only once and nobody can recover it for you.
5. Type the last 4 characters and press **I saved it**.
6. In "3. Backup", press **Back up now**.

You should see: the state **Up to date** and "Last backup". Backups are encrypted and repeat by themselves. What you keep on your disk outside Hokma is your own responsibility.

## Restore on a new computer

1. In **Backup and restore**, connect GitHub.
2. Choose a backup in **Available backups**.
3. Type your **Recovery key**.
4. Press **Restore**. Hokma applies the backup when it restarts.
5. Connect again the engine, Telegram, the folder and the projects Hokma lists.

## If something fails

- **"The code expired".** Press **Connect GitHub** again.
- **"Those 4 characters are not the end of your key".** Check that you copied the last 4 correctly.
- **Without the key nothing is restored.** Save it before you need it.
- **"The backup could not be applied on restart".** Your previous data is untouched.
