---
name: setup
description: Connect SearchApi to this agent (API key or MCP sign-in) and check that it works
---

Run the Connect step of the `searchapi` skill now, even if something already looks connected. If the user named the API or MCP, set up that one. If a key or a SearchApi MCP tool is already there, say which and skip to the check. Otherwise ask with buttons: I have a key, I need a key, Use MCP.

Check: run the skill's `scripts/searchapi.py credits` and report "Connected" with the credits left. On MCP only, confirm the SearchApi tools are listed (no test search needed).

$ARGUMENTS
