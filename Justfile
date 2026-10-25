# Fetch Ouroboros and build both environments: Ouroboros's own and convy's.
setup:
    git submodule update --init
    cd ouroboros && uvx "uv==$(sed -n 's/^required-version = "==\(.*\)"/\1/p' pyproject.toml)" sync --locked
    uv sync

# The tests that need no server.
test:
    uv run pytest

# The tests with real Ouroboros servers and a scripted model: free, about a minute.
test-live:
    uv run pytest -m live

# Check the integration through convy with the scripted model: free, no keys needed.
smoke:
    GW_BASE_URL="${GW_BASE_URL:-http://127.0.0.1:9/v1}" GW_API_KEY="${GW_API_KEY:-unused}" uv run convy run ouroboros_stub --smoke

# Play every scenario against the three profiles, k attempts each (paid: real models).
bench k="3":
    uv run convy run ouroboros_min ouroboros_mid ouroboros_max -k {{k}}
