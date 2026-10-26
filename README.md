# ouroboros-bench

Conversation tests of [Ouroboros](https://github.com/razzant/ouroboros) with
[convy](https://github.com/deyna256/convy): a simulated user talks to Ouroboros in its Main chat,
a judge checks each scenario's claims, and convy reports the pass rate, the tokens, the answer
times and every step Ouroboros took: what it thought, each model round and each tool call.

Ouroboros's tools run real commands, so each server runs in a
[bubblewrap](https://github.com/containers/bubblewrap) sandbox (see "Sandbox"): no Docker, no root.

## Setup

Needs Linux with [uv](https://docs.astral.sh/uv/), [just](https://just.systems), git and
bubblewrap (`bwrap`); uv fetches the Pythons both environments need.

```sh
git clone https://github.com/deyna256/ouroboros-bench && cd ouroboros-bench
just setup                 # the Ouroboros submodule and both environments
cp .env.example .env       # keys and models; the tests and smoke below need none of them
just test                  # tests that need no server
just test-live             # real servers with a scripted model: free, one to two minutes
just smoke                 # the same through convy
```

Then a real run, for example one scenario on the `min` profile, then all of them on two:

```sh
uv run convy run ouroboros_min -k 1 --scenarios "seating-puzzle"
uv run convy run ouroboros_min ouroboros_max -k 3      # or: just bench 3
uv run convy compare <run-of-min> <run-of-max>       # run ids: results/<agent>/
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

- the system read-only, a private `/tmp` and an empty home in memory, with uv's Pythons;
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
with three workers per server. An agent's `version` is the submodule's commit, the profile, the
main and light models and a hash of `ouroboros_convy/`, so a run says which build it measured and
`convy resume` refuses to mix two.

## Models

- **Ouroboros's**, the ones under test, come from `.env`, or from the environment, which wins.
  A model's name says its provider: `google/gemini-3.8-flash` goes to OpenRouter,
  `openai-compatible::<name>` to `OPENAI_COMPATIBLE_BASE_URL`, `anthropic::<name>` and the like
  to that provider's API with its own key.
- **convy's**, the simulated user and the judge, are named in `models.py`
  (`openai/gpt-5.6-luna`; change it if `GW_BASE_URL` serves other names) and reached through
  `GW_BASE_URL`. Ouroboros never sees them or their key.

To move Ouroboros to another provider, set every slot that does not follow `OUROBOROS_MODEL`
on its own, or the server calls OpenRouter's defaults behind it (with a key for a single
provider and no gateway, that provider's):

| setting | when unset |
|---|---|
| `OUROBOROS_MODEL` | OpenRouter's `google/gemini-3.8-flash` |
| `OUROBOROS_MODEL_LIGHT`: helpers, naming, safety checks | OpenRouter's `openai/gpt-6-luna` |
| `OUROBOROS_MODEL_FALLBACKS`: the models a failed call is retried on | OpenRouter's `openai/gpt-5.6-luna` |
| `OUROBOROS_MODEL_VISION`, `_CONSCIOUSNESS` | the main model |
| `OUROBOROS_MODEL_SAFETY` | the light model |
| `OUROBOROS_WEBSEARCH_MODEL`: the `web_search` tool | `gpt-5.2` on OpenAI or OpenRouter, Claude on Anthropic, else DuckDuckGo |

A fallback equal to the main model is no fallback, so one model in all three runs on exactly
one. Set but blank, Light follows the main model and Fallbacks are none.

## GigaChat

GigaChat is reached through [gpt2giga](https://github.com/ai-forever/gpt2giga), a local proxy
that speaks OpenAI's API, in [the fork](https://github.com/deyna256/gpt2giga): 0.3.0 that counts
cached input tokens as input. Upstream 0.3.0 undercounts them, and 0.3.1a1 passes on a tool
schema of Ouroboros's that GigaChat refuses with 422. The proxy runs outside the sandbox and
holds the GigaChat key and the Russian root CA, which GigaChat's certificates are signed with.

**1. The key.** At [developers.sber.ru](https://developers.sber.ru/studio), in your GigaChat API
project, take the *authorization key* (a base64 string, not the Client ID or the Client
Secret) and its scope: `GIGACHAT_API_PERS` for a personal account, `GIGACHAT_API_B2B` or
`GIGACHAT_API_CORP` for a company's.

**2. Install and configure.** The configuration is a file readable by you alone:

```sh
uv tool install git+https://github.com/deyna256/gpt2giga@3cfbfb42  # gpt2giga in ~/.local/bin
mkdir -p ~/.config/gpt2giga && cd ~/.config/gpt2giga
curl -fsSO https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
install -m 600 /dev/null .env
cat > .env <<CONF
GIGACHAT_CREDENTIALS=
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_CA_BUNDLE_FILE=$HOME/.config/gpt2giga/russian_trusted_root_ca_pem.crt
GIGACHAT_TIMEOUT=300
GPT2GIGA_HOST=127.0.0.1
GPT2GIGA_PORT=8090
GPT2GIGA_DISABLE_REASONING=True
CONF
```

Then paste the key after `GIGACHAT_CREDENTIALS=` in an editor, which keeps it out of the shell's
history, and change the scope if yours differs.

**3. Run and check.** The proxy has to run while the bench does:

```sh
cd ~/.config/gpt2giga
nohup gpt2giga --env-path .env > gpt2giga.log 2>&1 & echo $! > gpt2giga.pid
curl -s http://127.0.0.1:8090/v1/models      # lists GigaChat-2-Max: the key and the CA work
kill "$(cat ~/.config/gpt2giga/gpt2giga.pid)"  # to stop it
```

**4. The bench.** In the bench's `.env`; all three slots, or Ouroboros calls OpenRouter for the
others:

```sh
OPENAI_COMPATIBLE_BASE_URL=http://127.0.0.1:8090/v1
OPENAI_COMPATIBLE_API_KEY=unused
OUROBOROS_MODEL=openai-compatible::GigaChat-2-Max
OUROBOROS_MODEL_LIGHT=openai-compatible::GigaChat-2-Max
OUROBOROS_MODEL_FALLBACKS=openai-compatible::GigaChat-2-Max
```

Then `uv run convy run ouroboros_mid -k 1 --scenarios shower-estimate` is a short real run.

**When it fails.** A client sees the proxy's `500 Internal server error`; the cause is in
`~/.config/gpt2giga/gpt2giga.log`:

| in the log or the answer | cause | fix |
|---|---|---|
| `gpt2giga: command not found` | `~/.local/bin` is not on `PATH` | `uv tool update-shell`, then a new shell |
| `Port 8090 is already in use` | a proxy already runs | stop it, or change the port in both `.env` files |
| `Can't decode 'Authorization' header` | not the authorization key, or cut short | paste the whole key again |
| `429 Too Many Requests` | GigaChat's rate limit, often after failed logins | wait a minute |
| `FileNotFoundError` | `GIGACHAT_CA_BUNDLE_FILE` names a missing file | fix the path |
| `CERTIFICATE_VERIFY_FAILED` | no CA set | set `GIGACHAT_CA_BUNDLE_FILE` |
| a 500 on every request with tools | reasoning left on | `GPT2GIGA_DISABLE_REASONING=True` |
| a 500 after 30 seconds | the SDK's default timeout | `GIGACHAT_TIMEOUT=300` |
| a 422 about `checkpoint_ref.properties` | upstream gpt2giga 0.3.1 or later | install the fork as above |
| Ouroboros: `Missing credentials` | a slot left on OpenRouter | set all three slots |

What else to know:

- Without reasoning the profiles differ in context only: with tools and `reasoning_effort`
  together GigaChat-2-Max answers 500, and GigaChat-3-Ultra writes its tool calls as text.
- GigaChat-2-Max, not GigaChat-3-Ultra: Ultra at times repeats its reasoning until its token
  limit, about five minutes, and gives no answer.
- Thinking tokens are always 0 and cache writes are not reported. GigaChat reports no prices,
  so `OUROBOROS_BUDGET_USD` limits nothing; `mid` sends about 90,000 input tokens a call.

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
