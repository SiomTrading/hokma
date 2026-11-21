# Money desks

A money desk is a small team of agents that works on one idea with its capital. Each desk has its own
mandate, its own agents, its own assets and its own token budget. Several desks can run side by side, and each one
keeps its work apart from the others.

A desk's agents can send orders with real money only through Hokma's order tools, and only when the broker of the desk's account has orders turned on in the Broker tab. The account of each desk is assigned in the Broker tab.

## Open the desks

In the side menu press **Money desks**. Below it there is one row per desk with its name, its capital and its result
of the day. In this version the result of the day is always 0.00, because trades arrive in a later version.

## The Fund view

The first screen shows the numbers of the whole fund: the capital of all desks and the tokens used this
week. Below there is one card per desk with its name, focus, state, capital, result of today, drawdown, the agents
working now.

- The fund curve and the activity of the desks show an empty state. They fill in when trades arrive.
- The "Fund lessons" tab is disabled. It arrives in a later version.

## Create a desk

Press **+ Create desk**. Hokma asks "How do you want to create the desk?" and offers two ways.

### From a template

This way creates the desk. It has four steps:

1. **Focus.** Name, what the desk looks at, capital, monthly objective and mandate. The mandate is the text
   every agent of the desk reads before each decision.
2. **Team of agents.** Choose a template. You can change the team later.
3. **Rules.** Shows the token budget of the desk. The risk rules arrive in a later version and the screen says so.
4. **Assets and data.** The symbols the desk follows (up to 20) and the connections it uses.

The three templates:

| Template | Agents | Suggested weekly budget |
|---|---|---|
| Minimal (3) | Desk chief, Hugo, Risk | 150,000 tokens |
| Standard (7) | Minimal plus Iris, Signals, Axel, Vera | 300,000 tokens |
| With debate (10) | Standard plus Bull case, Bear case, Judge | 600,000 tokens |

The budgets are a starting point. You can change them or leave the desk without a limit.

The debate works on daily bars. Use it to prepare and review before the market opens, not to decide inside the
session.

### Create it with Hokma

This way opens the Hokma chat with a first message that asks for the desk. Hokma talks it over with you and proposes the
values. When you agree, Hokma asks for your permission in the chat, showing what it will create, and creates the desk
only after you approve. You can also ask it later to change a desk, and it asks for permission again. If you work in the "Do everything without asking" mode, Hokma creates or changes desks from the chat without asking for permission.

## Inside a desk

The top bar shows the desk selector, the capital, the result of today and the drawdown. The tabs
are Terminal, Agents, Operations, Rules and Settings.

- **Terminal.** The assets of the desk on the left, the chart of the asset in focus in the center, the chat with the
  desk chief on the right and an empty Positions panel below.
- **Agents.** The team grouped by state: Working, Waiting and Off. Press an agent to see what it can do, what it cannot
  do, its model on this desk, its tokens of the week and its latest activity. You can create an agent, bring one you
  already have or remove one from the desk. Removing it from the desk does not delete the agent.
- **Operations and Rules.** An empty state that says what arrives and when.
- **Settings.** Name, focus, capital, monthly objective, mandate, assets and the connections the desk uses. The keys of a
  connection are configured in Connections, not here.

## The desk chief and the team

The desk chief coordinates the team. It does not start the other agents directly: it creates a task for each one, and
each task runs with the model that desk gave that agent. For that reason the tokens of every turn are counted on the
right desk.

Agents are shared between desks, but the model is chosen per desk. Change the model of an agent on desk A and it does not
change on desk B or in Settings.

## Token budget

Each desk has a weekly budget of tokens.

- At **80%** the meter turns amber and a notice appears. It is written once per week in the journal.
- At **100%** the chat and the tasks of that desk do not start. The screen says the desk is paused by budget. A turn that
  is already running is not cut.
- Raise the budget, or wait for the next week, and the desk starts again by itself.

The usage of the Settings screen keeps counting the whole fund. The meter of a desk counts only that desk.

## What an agent can never do

- Send an order to a broker in any way other than Hokma's order tools, or ask for your keys or your account number. Only you turn on the orders of a broker.
- Change the rules, the limits, the mandate, the capital or the budget. An agent proposes and you decide in the app.
- Treat what comes from a web page, a file or a message as an order. Orders come only from you.

## If something does not work

- **The agent is off.** An agent turned off in Settings cannot join a desk. The screen offers to turn it on.
- **The desk chief cannot be removed.** Every desk needs one.
- **The desk does not answer.** Check the token meter: the budget may be at 100%.
