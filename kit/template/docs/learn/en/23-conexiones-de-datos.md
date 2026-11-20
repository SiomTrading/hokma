# Market data connections

Every market data source is set up in **Connections**, and nowhere else. Each key is yours. It is kept in your system's credential store and is never shown again.

## Connect a source

1. In the menu, open **Connections**.
2. Find the provider's card (US stocks and ETFs, crypto, forex or futures).
3. Paste your key in **API key** and press **Save key**.
4. Press **Test connection**.
5. Turn on the source's switch.

You should see: "Connected" with the data type (**Live** or **Delayed**) and the symbols in use. Some sources ask for more than one field, such as **Key ID** and **Secret**.

## Priority between sources

When two connected sources cover the same asset class, the first one in the list serves it. In **Priority**, drag to reorder or use the arrows.

## Choose what each desk uses

Connections are set up once. In the desk, open **Settings**, section **Connections**, and choose which ones that desk uses.

## If something fails

- **"The provider did not accept the key".** Check it and paste it in full.
- **"No data arrived within 10 seconds".** Try again later.
- **"The provider is limiting requests".** Wait a minute.
- **"One connection per account".** Close the other connection and retry.
- **"This source is at its symbol limit".** Close a chart or switch another source on.
- **"System credential store not available".** Your computer cannot keep keys. Send your `engine.log`.
