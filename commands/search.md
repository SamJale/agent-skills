---
name: search
description: Run a SearchApi search with the searchapi skill's rules
argument-hint: what to search for, e.g. best crm software in Germany
---

Search for: $ARGUMENTS

Follow the `searchapi` skill: pick the engine from its table, get the params live before the first call to an engine, set country and language, make the fewest calls, and answer with the links you used. If nothing is connected yet, run its Connect step first.
