# Telegram: your channel from your phone

With Telegram you talk to Hokma from your phone and receive its notices. You create your own bot, connect it and, if you like, use it in a group with topics.

## Create the bot with BotFather

1. In Telegram, search for **@BotFather** and open it.
2. Type `/newbot`.
3. Choose a name for your bot.
4. Choose a username that ends in `bot`.
5. Copy the token BotFather gives you.

You should see: a message with the token. It is a key: whoever has it controls the bot. Do not share it.

## Connect the bot to Hokma

1. Open Settings and choose **Telegram**.
2. Paste the token in **Bot token**.
3. Press **Connect**. The state changes to **Waiting for the code**.
4. In Telegram, send any message to your bot.
5. The bot replies with a code. Type it in **Code** in Hokma.
6. Press **Approve**.

You should see: the state **Linked** and your name's chat.

## Group with topics

Link your private chat first. Then:

1. In Telegram, create a group and turn on **Topics** in its settings.
2. Add your bot to the group as an administrator, with the permission to manage topics.
3. Type `/vincular` in the group. The bot replies with a code.
4. In Hokma, in Settings, **Telegram**, section **Group with topics**, type the code in **Group code**.
5. Press **Approve**.

You should see: **Group linked** and **Avisos topic ok**. Each topic in the group is a separate conversation with Hokma. You can link only one group, and it answers only your account.

## Notices

The **Avisos** topic receives what Hokma tells you: routines finished, failed or skipped, and permissions with buttons to approve or deny. In the "Do everything without asking" mode, permissions do not arrive in Telegram.

## If something fails

- **"That is not a Telegram bot token".** Copy the full token from BotFather, with no spaces.
- **"This computer has no safe place to keep the token".** Your computer has no credential store. Nothing was saved.
- **"Permission missing" appears.** The group does not have Topics on, or the bot is not an administrator with the permission to manage topics. Fix it in Telegram, press **Unlink group**, type `/vincular` again and approve. You can also press **Retry the Avisos topic**.
- **"No connection to Telegram".** Your chat is still linked. Hokma tries again by itself, or press **Reconnect**.
- **Messages waiting.** You see "N messages are waiting to reach Telegram". They arrive when the connection returns.
- **You want to start over.** Press **Unlink**: Hokma forgets the chat, the group and the token. Also revoke the bot in BotFather.
