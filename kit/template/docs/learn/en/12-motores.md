# Connect an AI engine

The engine is the AI program Hokma talks to. You can connect Claude, ChatGPT (Codex) or Gemini, and switch whenever you like. With no engine connected, the chat does not answer. The rest of Hokma still works.

## Connect Claude (recommended)

1. Open Settings with the gear in the left menu or with Ctrl and comma.
2. Choose **Engines**.
3. On the Claude card, press **Connect Claude**.
4. Sign in with your Claude account in the window that opens.
5. Go back to Hokma and press **Test**.

You should see: the state **Connected** and the message "Claude responds" with the seconds it took.

## Connect ChatGPT (Codex)

1. In Settings, **Engines**, find the **ChatGPT (Codex)** card.
2. Press **Sign in with my ChatGPT account**.
3. Hokma shows a link and a **Device code**.
4. Open the link in your browser and enter the code.
5. Sign in with your ChatGPT account and allow access.
6. Go back to Hokma and press **Test**.

You should see: **Connected** on the card.

## Connect Gemini

1. In Settings, **Engines**, find the **Gemini** card.
2. Press **Connect Gemini**.
3. Sign in with your Google account and allow access.
4. Press **Test**.

You should see: **Connected**. Google's free tier has a limited quota.

## Connect with an API key

If you have a key from another provider, use the **API key from any provider** card: choose the **Provider**, paste the **Key** and press **Connect with my key**. For an OpenAI compatible service, fill in the **Base URL** too. The key is never shown again.

## Choose the active engine

1. In Settings, **Engines**, find the engine you want.
2. Press **Use this engine**.

You should see: the **Active** mark on that engine. You can keep several connected and switch with **Connect another engine** and **Use this engine**.

## Choose the level and the model

Every engine has three levels: **Fast** (simple tasks), **Balanced** (daily work) and **Deep** (hard problems). A deeper level is slower and uses more of your plan.

1. In the chat, open the **Level** menu and pick one.
2. To choose the exact model of each level, press **Models** on the engine card.
3. Leave **Automatic** so Hokma uses the newest one, or pick one from the list.
4. Press **Save**.

You should see: "Level: Balanced" (or the one you chose) in the chat.

## If something fails

- **Session expired or Not installed (Claude).** Press **Connect Claude** again. If it says "Could not check", press **Check again**: it does not mean the session expired.
- **"Your account does not have Codex enabled".** Your ChatGPT admin must enable Codex. Then connect again.
- **"Your workspace does not allow signing in with a device code".** Ask the admin to turn it on in the workspace permissions, or connect with an API key.
- **The provider does not serve your location.** Press **Try again** later or **Connect another engine**.
- **It rejected the session or the key.** Check that the key is complete and still valid, and connect again in Settings, Engines.
- **The quota is used up.** Try another level or another engine.
- **The model you chose no longer exists.** Pick another in **Models** or leave it on **Automatic**.
- **Plugins do not work with ChatGPT or Gemini.** Plugins only work with Claude Code.
- **None of the above helps.** See the "Troubleshooting" topic to send your `engine.log`.
