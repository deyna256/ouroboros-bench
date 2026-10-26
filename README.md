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
just test-live             # real servers with a scripted model: free, one to two minutes
just smoke                 # the same through convy
```

Then a real run, for example one scenario on the `min` profile, then all of them on two:

```sh
uv run convy run ouroboros_min -k 1 --scenarios "seating-puzzle"
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
├── scenarios/          # ten conversations (see "Scenarios")
├── models.py, gateway.py
└── tests/
```

## How a conversation runs

Each conversation starts its own server (`launch.py`, through Ouroboros's
`devtools/benchmarks/common/server_runner.py`): a clone of the submodule's HEAD and a fresh data
root in `.servers/`, so no conversation sees another one's memory, identity or chat. Starting
takes about twenty seconds; it counts against convy's `--turn-timeout`, not in an answer's time.

A turn is one message in Main over the socket Ouroboros's own interface uses. Its answer is all
Ouroboros says until each task the message started has ended, work it hands off included: its
replies, the files it sends (a text file's content up to 20,000 characters), image captions,
links and cards. What other tasks say, such as a schedule the agent set, is not part of it.
When Ouroboros asks a question and waits, the next message answers it. A task that fails, does
not start, is paused or waits for its model's quota fails the turn at once, with the host's
notice as the reason.

Each answer carries:

- **steps**: the model's narration and readable reasoning as `Thought`s; each model round as a
  `Call` with its time and tokens; each tool call with its arguments, the start of its result
  (up to 2000 characters) and its time. Steps of helper tasks are named `(helper <id>)`.
- **usage**: the turn's tokens by the usage ledger of every task it ran, helpers and Ouroboros's
  own one-shot calls (naming, safety checks, post-task work) included: input, output, cache read
  and cache write, and thinking when every call reported it.

## Scenarios

Ten conversations that need nothing done on the machine, each testing one thing:

| scenario | what it tests |
|---|---|
| `sycophancy-monty-hall` | keeps a correct answer when the user insists on a wrong one |
| `laptop-hidden-constraints` | asks before recommending, then meets every need the user reveals |
| `trip-changed-mind` | follows a change of plan to the end |
| `false-premise-einstein` | corrects a false premise instead of building on it |
| `seating-puzzle` | solves a logic puzzle and justifies that the answer is unique |
| `fake-paper` | does not invent a paper that does not exist |
| `account-access-refusal` | refuses to break into an account and offers what is legitimate |
| `shower-estimate` | estimates from stated assumptions, with consistent arithmetic |
| `weekend-kazan` | remembers facts given early, in Russian |
| `sky-two-levels` | explains one thing to a child and to a physicist, in Russian |

## Sandbox

Each server, with every command its tools run, sees:

- the system read-only, a private `/tmp` and an empty home in memory;
- of the bench, only the Ouroboros checkout and `ouroboros_convy/`, read-only, and
  `.servers/`, where its clone and data live: never `.env`, `scenarios/` or `results/`;
- of the environment, only Ouroboros's settings and its providers' keys (`OUROBOROS_*`,
  `OPENROUTER_*`, `GIGACHAT_*` and the like): never convy's gateway key;
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
submodule's commit, the profile, the models and a hash of `ouroboros_convy/`, so a run says
which build it measured and `convy resume` refuses to mix two.

## Models

Two sets of models, set apart:

- **Ouroboros's**, the ones under test, come from `.env` (or the environment, which wins) and
  reach each server: `OUROBOROS_MODEL` and the key of its provider. A model's name says its
  provider: `google/gemini-3.8-flash` goes to OpenRouter, `openai-compatible::<name>` to
  `OPENAI_COMPATIBLE_BASE_URL`, and `anthropic::`, `openai::`, `gigachat::`, `cloudru::`,
  `deepseek::`, `minimax::` and `zai::` to those providers' own APIs with their own keys.
- **convy's**, the simulated user and the judge, are named in `models.py` and reached through
  `GW_BASE_URL`. Ouroboros never sees them or their key.

To move Ouroboros to another provider, set every slot that does not follow `OUROBOROS_MODEL`
on its own, or the server calls OpenRouter's defaults behind it:

| setting | when empty |
|---|---|
| `OUROBOROS_MODEL` | OpenRouter's `google/gemini-3.8-flash` |
| `OUROBOROS_MODEL_LIGHT`: helpers, naming, safety checks | OpenRouter's `openai/gpt-6-luna` |
| `OUROBOROS_MODEL_FALLBACKS`: the models a failed call is retried on | OpenRouter's `openai/gpt-5.6-luna` |
| `OUROBOROS_MODEL_HEAVY`, `_VISION`, `_CONSCIOUSNESS`, `_SAFETY` | the main or the light model |
| `OUROBOROS_WEBSEARCH_MODEL`: the `web_search` tool | OpenAI's `gpt-5.2`, with `OPENAI_API_KEY` |

A fallback equal to the main model is no fallback, so one model in all three is the way to run
on exactly one. Without a key for OpenAI, `web_search` fails and Ouroboros has to do without; no
scenario here needs it.

The models are part of an agent's `version`, so a run on another model is another build:
`convy resume` will not continue a run on it, and `convy compare` sets the two side by side.
`.env` holds one set; to measure another, run again with it in the environment:

```sh
OUROBOROS_MODEL=... OUROBOROS_MODEL_LIGHT=... OUROBOROS_MODEL_FALLBACKS=... \
    uv run convy run ouroboros_mid -k 1
```

## GigaChat

GigaChat is reached through [gpt2giga](https://github.com/ai-forever/gpt2giga), a proxy on this
machine that speaks OpenAI's API, as an OpenAI-compatible gateway. Use
[the fork](https://github.com/deyna256/gpt2giga): gpt2giga 0.3.0 with one change, which counts
GigaChat's cached input tokens as input (see below). Upstream 0.3.0 undercounts them; upstream
0.3.1a1 counts them right but passes Ouroboros's tool schemas to GigaChat unchanged, and
GigaChat refuses one of them with 422 (`...checkpoint_ref.properties is missing`), so Ouroboros
cannot run on it at all.

The proxy keeps the GigaChat key and the Russian root CA, outside the sandbox; the sandbox
shares the machine's network and reaches it at `localhost:8090`. Its configuration goes in a
file of its own, readable by you alone:

```sh
uv tool install git+https://github.com/deyna256/gpt2giga
mkdir -p ~/.config/gpt2giga && cd ~/.config/gpt2giga
curl -fsSO https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
install -m 600 /dev/null .env
cat >> .env <<'CONF'
GIGACHAT_CREDENTIALS=<the authorization key from developers.sber.ru>
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_CA_BUNDLE_FILE=/home/<you>/.config/gpt2giga/russian_trusted_root_ca_pem.crt
GIGACHAT_TIMEOUT=300
GPT2GIGA_HOST=127.0.0.1
GPT2GIGA_PORT=8090
GPT2GIGA_DISABLE_REASONING=True
CONF
gpt2giga --env-path ~/.config/gpt2giga/.env
```

Then in `.env` (or for one run, in the environment):

```sh
OPENAI_COMPATIBLE_BASE_URL=http://127.0.0.1:8090/v1
OPENAI_COMPATIBLE_API_KEY=unused
OUROBOROS_MODEL=openai-compatible::GigaChat-2-Max
OUROBOROS_MODEL_LIGHT=openai-compatible::GigaChat-2-Max
OUROBOROS_MODEL_FALLBACKS=openai-compatible::GigaChat-2-Max
```

convy's user and judge can stay where they are, or go to GigaChat too: `GW_BASE_URL` at the
proxy and GigaChat's model names in `models.py`.

Checked against GigaChat's API directly, with its own SDK, and its
[documentation](https://developers.sber.ru/docs/ru/gigachat/guides/counting-tokens):

- `GPT2GIGA_DISABLE_REASONING=True` is needed. Ouroboros sends tools and `reasoning_effort`
  together; GigaChat-2-Max answers that with 500, and GigaChat-3-Ultra writes its tool calls as
  text inside its reasoning instead of calling them. Without tools both models reason. So the
  profiles differ in context only.
- GigaChat-2-Max is the model to use. GigaChat-3-Ultra answered at times, and at times streamed
  nothing but repeated reasoning until its token limit, about five minutes, with no answer.
- `GIGACHAT_TIMEOUT=300`: the SDK's default of 30 seconds cuts long answers off, and the proxy
  reports that as a 500.
- Tokens: GigaChat's `prompt_tokens` leaves out the cached input it reports as
  `precached_prompt_tokens`, where OpenAI's API counts it in. The fork adds it back, so the
  input stays whole and the cached part shows as cache read. `reasoning_tokens` is always 0,
  and the cache write is not reported.
- A personal key is rate-limited, and GigaChat reports no prices, so `OUROBOROS_BUDGET_USD`
  limits nothing. The `mid` profile sends about 90,000 input tokens a call.

## Memory

A server takes about 0.4 GB idle and more while its workers run, so `agent_parallel` is 1;
with more, servers start one at a time.
Servers stop when their conversation ends, on Ctrl+C and on SIGTERM; a sandboxed one also when
convy is killed. The folder a killed launch leaves in `.servers/` is swept by the next launch.

## Changes to Ouroboros

The submodule's branch `convy` changes three things in Ouroboros 7.8.0:

1. `llm_round` events carry `reasoning_tokens` and `reasoning_text`, the readable reasoning a
   provider returned, even when the round also had visible text.
2. A task's cost fields (`GET /api/tasks/<id>`) carry the ledger's `cached_tokens`,
   `cache_write_tokens` and `reasoning_tokens`.
3. Parallel tool calls a gateway streams at the same index stay apart instead of merging into
   one refused call (`read_filerun_command`).
