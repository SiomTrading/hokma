# Extensions

Extensions add abilities to your agents: plugins from a marketplace and MCP servers. You decide what is active.

## See what is installed

Open Settings and choose **Extensions**. Each plugin shows its marketplace and a switch. MCP servers are listed
with the program they run.

## Turn a plugin on or off

Press its switch. Hokma shows what will be activated and its risks, and asks you to confirm. Your confirmation is
saved as an approval and changes your extensions file.

## Requests from agents

An agent can ask for a plugin or an MCP server with `hokma ext request`. The request appears at the top of the
Extensions section with Approve and Reject. Nothing changes until you approve. An MCP server runs outside the
sandbox with your own rights, so read its command and arguments before you approve.

## Add a marketplace

Press "Add by link" and write a GitHub address as `owner/repo`. The marketplace is added to your list. Its plugins
stay off until you turn them on.

## From the terminal

- `hokma ext list` shows what you have.
- `hokma ext requests` lists pending requests.
- `hokma ext approve <id>` and `hokma ext reject <id>` decide a request.
