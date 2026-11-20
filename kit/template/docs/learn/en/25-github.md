# GitHub, well explained

GitHub is a site on the Internet where you keep copies of your folders. Hokma uses it to back up your work and to install plugins and skills. You do not need to know how to code.

## What GitHub and a repository are

A **repository** is a folder with a memory. It keeps your files and every version you made. It can be **private** (only you see it) or **public** (anyone sees it).

**Git** is the tool that keeps that memory on your computer. **GitHub** is the site where an online copy lives. They are not the same: Git is the tool and GitHub is one of the places where you keep the result.

For your strategies and notes, always use a **private** repository.

## Create the account

1. Go to github.com and press **Sign up**.
2. Type an email you can access, a password and a username.
3. Confirm the email with the code you receive.

You should see: your GitHub home page with your username at the top right.

GitHub sets its own plans and limits, and they can change. Hokma does not promise prices.

## Create a private repository

1. On GitHub, press the **+** sign at the top and choose **New repository**.
2. Type a name, for example `my-fund-backup`.
3. Select **Private**.
4. Press **Create repository**.

You should see: the empty repository page, with an address like `https://github.com/your-user/my-fund-backup`.

## Connect GitHub in Hokma

The connection is in Settings, **Backup and restore**. It is not in the **Connections** screen: that one is for market data and Gmail.

1. Open Settings and choose **Backup and restore**.
2. In "1. Connect your GitHub", press **Connect GitHub**.
3. Hokma shows a code. Press **Open GitHub**.
4. On GitHub, type the code and authorize Hokma. Meanwhile Hokma says "Waiting for your authorization".
5. Press **Choose repo on GitHub**. Select "Only select repositories" and pick your private repository.
6. Go back to Hokma. If the list does not update, press **Refresh list**.

You should see: "Connected as" with your username and "Repository:" with the chosen name.

Hokma only sees the repository you chose. The rest of your GitHub stays out. To change it, press **Change repo**. To cut the connection, press **Disconnect**.

## What Hokma uses it for

- **Backup.** Hokma keeps encrypted copies of your projects in your private repository. The full steps are in the topic on Backup, Trash and restore points.
- **Plugins.** You can install a plugin from its repository.
- **Skills.** You can install a skill from its repository.

Plugins and skills by link do not need you to connect your account. Only the backup does.

## Install a plugin by pasting the address

1. Open Settings and choose **Extensions**.
2. In the **Plugins** tab, press **Add by link**.
3. In **GitHub repository** type `owner/repository` or paste the link, for example `https://github.com/owner/repository`.
4. Press **Add**.
5. If the repository has one plugin, it is installed and turned on right away. If it has several, you choose which ones.

You should see: "was installed and is on", or the marketplace added with its plugins. Always read what the plugin turns on before you confirm.

## Install a skill by pasting the address

1. In **Extensions**, press **Add skill by link**.
2. In **GitHub link of the skill**, paste the repository address, or the address of one skill folder inside it.
3. If you already have a skill with that name and want to replace it, select the replace box. The old one goes to the Hokma trash.
4. Press **Install skill**.

You should see: "Skill installed" with the name. It is ready at once. If the link was a plugin, Hokma tells you and puts it in your extensions list.

## What a commit and a push are

- **Commit.** A snapshot of your files at one moment, with a short message. For example "First version". It is saved on your computer.
- **Push.** Sending those commits to GitHub. That brings the online copy up to date.

With Hokma's backup you do neither by hand. Hokma does it for you. When you drag a file into a project, Hokma makes the commit on its own. If you later want to upload a project yourself, do it from the built-in terminal, with your empty private repository.

## If something fails

- **"The code expired. Connect again."** Press **Connect GitHub** again and type the new code.
- **"GitHub refused the authorization."** You cancelled or denied it on GitHub. If it was a mistake, connect again.
- **"No connection to GitHub. Your account is still connected."** Check your Internet and press **Retry**.
- **"No repos yet."** Press **Choose repo on GitHub**, select "Only select repositories" and pick your repository. If you do not have one, press **Create a new private repo** and come back.
- **"That repository cannot hold the backup".** It must be private and one of those you gave Hokma. Use **Change repo**.
- **"Hokma can no longer reach your backup repository".** You deleted it or removed the permission. Press **Change repo** and choose one.
- **"This computer has no secure place to keep the GitHub access".** Your computer has no credential store. Nothing was saved.
- **You see "Connect GitHub again and choose your repo".** Your connection is from an older version and gave access to your whole account. Connect again. You can press **Remove the previous access on GitHub**.
- **You pasted a link and the format is rejected.** Write `owner/repository` or `https://github.com/owner/repository`.
- **"Could not install" with a reason.** Read the reason. Without Internet the install does not move. Press **Retry**.
