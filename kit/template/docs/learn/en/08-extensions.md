# Extensions

Extensions add abilities to your agents: plugins from a marketplace and MCP servers. You decide what is active.

## See what is installed

Open Settings and choose **Extensions**. Each plugin shows its marketplace and a switch. MCP servers are listed
with the program they run.

## Turn a plugin on or off

Press its switch. Hokma shows what will be activated and its risks, and asks you to confirm. Your confirmation is
saved as an approval and changes your extensions file.

## Agents install for you

An agent can install and remove plugins, skills and MCP servers from the chat (`claude plugin install`,
`claude mcp add`, or a skill file in your working folder). Hokma already has Node, Bun, npm, npx and uv on the PATH
of the agent and of the plugins' hooks, so a plugin that needs one of them starts. An MCP server runs with your own
rights, so ask the agent what it installed. `hokma ext request` still exists: the request waits at the top of the
Extensions section with Approve and Reject.

## Add a marketplace

Press "Add by link" and write a GitHub address as `owner/repo`. The marketplace is added to your list. Its plugins
stay off until you turn them on.

## From the terminal

- `hokma ext list` shows what you have.
- `hokma ext requests` lists pending requests.
- `hokma ext approve <id>` and `hokma ext reject <id>` decide a request.
