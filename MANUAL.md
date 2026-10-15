## Codex Agent

Runs **OpenAI Codex** as an agent on this plugin's host. Each run of the node is
one turn of a durable **thread**, so a node can hold a conversation across a
whole flow — and stop to ask a person when it needs a decision.

Unlike most nodes, this one changes files on a real machine. Two things keep
that safe:

- **The sandbox is the operating system's.** `read-only` and `workspace-write`
  are enforced by Seatbelt (macOS) or bubblewrap (Linux), not by the model's
  good behaviour.
- **The workspace root fences every node.** Set it on the profile below; a node
  pointed outside that tree fails before Codex starts.

### Set it up

1. Install Codex on the plugin's host, or let the bundled runtime serve (it
   ships with the plugin's `openai-codex` dependency).
2. Either log the host in (`codex login`) or put an API key on the profile.
3. Set a **workspace root** — the one tree nodes may work in.

Test the host:

```inflow-meta
codex.meta.ping
```

What this host offers:

```inflow-meta
codex.meta.models
```

Threads already on this host:

```inflow-meta
codex.meta.threads
```

### The human loop

Wire the node's **Needs a human** port to a Human in the Loop node (park mode),
and bring its reply back into the same Codex node:

```
[ Run Codex ] --Answer------------------> … the rest of the flow
      |      \--Limit reached-----------> (run again, or escalate)
      |      \--Failed------------------> (handle it)
      |
      '---------Needs a human----> [ Human in the Loop ]  (park, key: humanReply)
                                            |
                                            '--> back into [ Run Codex ]
```

The person's reply is read from **Answer path** (`$.humanReply` by default).
Reply **approve** or **decline** to answer an approval; anything else is passed
to Codex as instructions and the thread carries on.

### Reading the result

| Path | What |
|---|---|
| `{{$.codex.state}}` | `done` · `ask` · `limit` · `error` |
| `{{$.codex.text}}` | Codex's own answer for the turn |
| `{{$.codex.output}}` | the structured answer, when a schema was set |
| `{{$.codex.question.prompt}}` | what to show a person on the `ask` branch |
| `{{$.codex.threadId}}` | the thread this turn ran as — also the job id |
| `{{$.codex.tokens.total}}` | what the turn spent |
