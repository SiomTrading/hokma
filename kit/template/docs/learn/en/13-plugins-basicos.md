# Basic plugins: claude-mem, caveman and watch

A plugin gives your agents a new capability. You install them from **Extensions**. Hokma shows what it will turn on and asks you to confirm before installing. Plugins only work with the Claude Code engine.

## What each one does

- **claude-mem.** Memory between conversations: it saves what happened and reminds the agent in the next one. It needs a helper process that starts by itself with your first conversation.
- **caveman.** Makes agents answer short, with no filler. You spend fewer output tokens. Hokma gives no saving figure: measure it with the "Tokens this week" bar in Settings, Models and usage, one day before and one day after.
- **watch.** Lets your agents watch videos. You give it a link (YouTube and others) or a video file: the agent downloads it, pulls frames and the transcript, and answers whatever you ask about the video. If it does not show up in the search, add it by link with `bradautomates/claude-video`.

## Install a plugin

1. Open Settings with the gear in the left menu or with Ctrl and comma.
2. Choose **Extensions** and open the **Plugins** tab.
3. In "Anthropic marketplace", type the name in **Search a plugin**.
4. Press **Install** on the plugin you want.
5. Read what gets activated and press **Install** in the confirmation box.
6. Wait. The installation can take up to 5 minutes.

You should see: the plugin in the **Installed** list, with its switch on.

## If it does not show in the search

1. In **Extensions**, press **Add by link**.
2. Type `owner/repository` or paste the plugin's GitHub link.
3. If the link has one plugin, it installs at once. If it has several, choose which ones.

You should see: the added marketplace and its plugins in the list.

## Use them

An installed plugin works on its own: there is no new button. Start a new conversation so your agents use it. To turn it off without deleting it, use its switch. To remove it, uninstall it from the same list.

## If something fails

- **"Could not install {name}" with a reason.** Read the reason and press **Retry**. With no Internet the installation does not move.
- **The plugin says "Claude Code only".** Your active engine is another one. Switch to Claude in Settings, **Engines**.
- **claude-mem says "Check".** The files are there, but its helper process does not answer. Close and open Hokma, and start a new conversation.
- **It does not show in the Anthropic marketplace.** Use **Add by link**.
