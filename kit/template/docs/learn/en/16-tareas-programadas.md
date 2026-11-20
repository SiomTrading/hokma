# Routines and the premarket briefing

A routine asks an agent to do something at the time you choose. It is called a **Scheduled task**. It runs without you: it cannot ask you for permissions during the run.

## Create a routine

1. In the menu, open **Scheduled tasks**.
2. Press **New scheduled task**.
3. Type the **Name**.
4. Choose the **Agent**.
5. Type the **Instruction**: what the agent will do each time.
6. In **Frequency** choose **Once**, **Daily**, **Weekly** or **Every so often**. Fill in the time, the days or the interval (between 5 minutes and 31 days).
7. Check the **Time zone**.
8. Press **Create task**.

You should see: "Task created. It stays off until you turn it on."

You can also ask Hokma in the chat "create a daily backup task on GitHub" and it creates it. Hokma asks your permission to create it.

## Approve it

1. In the list, press the task's switch.
2. Read who will run it and with which model. It spends from your plan each time.
3. Press **Yes, turn on**.

You should see: "Task turned on." and the "Next:" line with the date. Your confirmation is recorded. If you change what the task runs, it turns off and you must approve it again.

## Run now

1. On the task, press **Run now**.
2. Wait. The task shows "Running now".

You should see: "Last run" with its status ("finished" or "failed") and the tokens used. The next scheduled run does not change. Turn the task on once first: that approves it.

## Premarket briefing

It is a summary to prepare your day, not a signal. It puts together the economic calendar, the news and your watched assets.

1. In **Scheduled tasks**, press **Add pre-market briefing**.
2. Approve it with **Yes, turn on**.

You should see: the briefing on the Today card of the Fund, and in Telegram if you linked it. It runs Monday to Friday. To see it now, press **Generate now** on the Today card. Ask Hokma in the chat if you want to change the time.

## If your computer was off

- Routines run only while Hokma is open.
- If it was off, what you missed runs once when you turn it on, if no more than 24 hours passed (3 hours for the premarket briefing). Hokma tells you on Telegram.
- If more time passed, the task is marked as skipped and does not run on its own. Use **Run now** if you want it.
- Hokma tells you on Telegram when a routine finishes, fails or is skipped.
- Closing the window with the X only hides it: routines keep running. **Shut down Hokma** stops them.
- To keep the computer from suspending while it works, turn on **Keep Hokma working** in Settings, **Keep working**.
- Running always, without your computer, does not exist yet.

## If something fails

- **"Its agent is off".** Turn the agent on in Settings, **Agents**.
- **The task did less than you asked.** It says "finished, N permissions denied". A routine cannot ask for permissions: adjust the instruction.
- **The briefing does not arrive.** Turn on the Premarket briefing task and press **Generate now**. Without Telegram linked, the briefing stays only on the Today card.
