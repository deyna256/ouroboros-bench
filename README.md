# ouroboros-bench

Conversation tests of [Ouroboros](https://github.com/razzant/ouroboros) with
[convy](https://github.com/deyna256/convy): a simulated user talks to Ouroboros in its Main chat,
a judge checks each scenario's claims, and convy reports the pass rate, the tokens, the answer
times and every step Ouroboros took: what it thought, each model round and each tool call.

Ouroboros's tools run real commands, so each server runs in a
[bubblewrap](https://github.com/containers/bubblewrap) sandbox (see "Sandbox"): no Docker, no root.

## Setup

```sh
just setup                 # the Ouroboros submodule and both environments
cp .env.example .env       # convy's models, then Ouroboros's models and key
just test                  # tests that need no server
just test-live             # real servers with a scripted model: free, about a minute
just smoke                 # the same through convy
```

Then a real run, for example two scenarios on the `min` profile:

```sh
uv run convy run ouroboros_min -k 1 --scenarios "clarify-backup"
uv run convy run ouroboros_min ouroboros_max -k 3      # or: just bench 3
uv run convy compare <run-of-min> <run-of-max>
```

## Layout

```text
ouroboros-bench/
├── ouroboros/          # Ouroboros, a submodule on the branch `convy` (see "Changes to Ouroboros")
├── ouroboros_convy/    # the integration
│   ├── agent.py        # Ouroboros: the convy agent; Chat, Turn: one dialogue and one answer
│   ├── trace.py        # a turn's steps and tokens, from the task's event stream and ledger
│   ├── server.py       # Server: one isolated Ouroboros server per conversation
│   └── launch.py       # runs in Ouroboros's own venv: starts and stops that server
├── agents/             # ouroboros_min, _mid, _max and _stub
├── scenarios/          # convy's template scenarios
├── models.py, gateway.py
└── tests/
```

## How a conversation runs

Each conversation starts its own server (`launch.py`, through Ouroboros's
`devtools/benchmarks/common/server_runner.py`): a clone of the submodule's HEAD and a fresh data
root in `.servers/`, so no conversation sees another one's memory, identity or chat. Starting
takes about twenty seconds; it counts against convy's `--turn-timeout`, not in an answer's time.

A turn is one message in Main over the socket Ouroboros's own interface uses. Its answer is every
reply Ouroboros writes until each task the message started has ended, work it hands off included.
When Ouroboros asks a question and waits, the next message answers it.

Each answer carries:

- **steps**: the model's narration and readable reasoning as `Thought`s; each model round as a
  `Call` with its time and tokens; each tool call with its arguments, the start of its result
  (up to 2000 characters) and its time. Steps of helper tasks are named `(helper <id>)`.
- **usage**: the turn's tokens by the usage ledger of every task it ran, helpers and Ouroboros's
  own one-shot calls (naming, safety checks, post-task work) included: input, output, cache read
  and cache write, and thinking when every call reported it.

## Sandbox

Each server, with every command its tools run, sees:

- the system read-only, a private `/tmp` and an empty home in memory;
- of the bench, only the Ouroboros checkout and `ouroboros_convy/`, read-only, and
  `.servers/`, where its clone and data live: never `.env`, `scenarios/` or `results/`;
- its own process namespace, which dies with the conversation, or with convy if convy is
  killed, so no server outlives its conversation.

The network stays open for the model's API, so a scenario that asks for `curl | bash` can
still download and run a script; inside the sandbox it can only write to its own server's folder
and read what the server itself holds, such as its model key. `Server(..., sandbox=False)` runs
without the sandbox, on Linux without bubblewrap for example: then Ouroboros acts on this machine
with your user's rights.

## Profiles

| agent | context (`OUROBOROS_CONTEXT_MODE`) | effort (`OUROBOROS_EFFORT_TASK`) |
|---|---|---|
| `ouroboros_min` | `nano` | `low` |
| `ouroboros_mid` | `low` | `medium` |
| `ouroboros_max` | `max` | `high` |

All of them run in the `light` runtime mode, so Ouroboros cannot rewrite itself during a run,
with three workers per server. The models come from `.env`. An agent's `version` is the
submodule's commit and the profile, so a run says which build it measured.

## Memory

A server takes about 0.4 GB idle and more while its workers run, so `agent_parallel` is 1.
Servers stop when their conversation ends, on Ctrl+C and on SIGTERM; a sandboxed one also when
convy is killed. The folder a killed launch leaves in `.servers/` is swept by the next launch.

## Changes to Ouroboros

The submodule's branch `convy` adds two commits over Ouroboros 7.8.0 so convy sees more:

1. `llm_round` events carry `reasoning_tokens` and `reasoning_text`, the readable reasoning a
   provider returned, even when the round also had visible text.
2. A task's cost fields (`GET /api/tasks/<id>`) carry the ledger's `cached_tokens`,
   `cache_write_tokens` and `reasoning_tokens`.
