# Broker and capital

In the **Broker** tab of the Fund you manage the accounts. Each desk works with one account, and that account gives it its capital. The broker included with Hokma uses the real prices of your connections and charges commission and slippage on every trade.
## See the broker and its costs

1. In **Money desks**, open the **Broker** tab.
2. Look at **Connected brokers**: the one that says **Included with Hokma**.

You should see: the **Commission** (on the value of each trade) and the **Slippage** (against you: you buy higher, you sell lower). They are measured in basis points. 100 basis points are 1%.

To change them press **Edit costs**, adjust and press **Save**.

## Create an account

1. In the **Broker** tab, press **New account**. If you have none, press **Create your first account**.
2. Choose the **Broker**.
3. Type the **Name**.
4. Type the **Starting capital**, a whole number from 100 to 1,000,000,000.
5. Adjust **Commission (basis points)** and **Slippage (basis points)** if you like.
6. Press **Create account**.

You should see: the account in the table with its **Starting capital**, its **Balance** and **No desk**.

## Assign an account to a desk

1. On the account's row, press **Assign to desk**.
2. Choose the **Desk**.
3. Press **Assign**.

Another way: on the desk's card, press **Assign account**.

You should see: the account and its balance on the desk's card, and the desk's name in the table. An account can only be in one desk. If there are no free accounts, create one first.

## Remove an account from a desk

1. On the account's row, press **Remove from desk**.
2. Confirm with **Remove from desk**.

You should see: **No desk** on that account. The account and its balance are kept and stay free for another desk.

## Reset an account

1. On the account's row, press **Reset**.
2. Confirm with **Reset account**.

You should see: the balance equal to the starting capital. The result of that account is erased. The other accounts do not change.

## The Fund's capital

The Fund's **Capital** is the sum of the accounts assigned to desks. An account with no desk does not count. Keep capital apart by strategy: a bad desk does not eat another desk's capital.

## If something fails

- **"No broker is connected".** Restart Hokma to get the Hokma broker back.
- **"There are no free accounts".** Create a new account in **Broker**.
- **"Every desk already has an account".** Create a desk or remove an account from another.
- **Connecting Schwab.** Connect it in Connections: its accounts appear in this tab with their number masked and without **Reset**, and you assign them to your desks the same way. Its card has the switch **Agents can trade in this account**, off until you turn it on. With it on, the desk agents send real-money orders to your Schwab account and you can lose money.
