# ai-usage: Claude Code + Codex quota in your terminal

How much of your **Claude** and **ChatGPT Codex** subscription quota is left, and when it resets, in one command:

```text
$ python3 collector.py status
Claude
  5-hour   ██████░░░░   62% left · resets in 2h 14m
  weekly   ████████░░   80% left · resets in 3d 6h
Codex · Pro Lite
  weekly   ████░░░░░░   35% left · resets in 4d 23h
```

- **Zero dependencies.** The collector is a single Python 3.11+ file that uses only the standard library.
- **It never uses your quota.** It reads the numbers your Claude Code and Codex CLIs already report. It never starts an AI turn.
- **Private by design.** It stores only the percentages and reset times. No prompts, replies, account IDs or credentials.

## Quick start

```bash
git clone https://github.com/bookchaowalit/platform-ai-usage-api ai-usage
cd ai-usage
python3 collector.py status
```

**Codex** works right away if the `codex` CLI is installed and logged in. `status` asks its local app-server for `account/rateLimits/read` each time you run it.

**Claude** reports quota through its [status line](https://docs.anthropic.com/en/docs/claude-code/statusline), so hook the collector in once:

```bash
# for one project (writes .claude/settings.local.json, keeps your other settings)
python3 collector.py install-claude --settings-path "$PWD/.claude/settings.local.json"
```

Or for every project, add this to `~/.claude/settings.json`:

```json
{ "statusLine": { "type": "command", "command": "python3 /path/to/ai-usage/collector.py claude-statusline" } }
```

The Claude Code status line then shows your remaining quota. Each refresh also saves a snapshot that `status` can read later.
Already have a status line? Pass `--passthrough-settings ~/.claude/settings.json` and the existing one keeps running.

## Commands

| Command | What it does |
| --- | --- |
| `status` | Prints the quota table. Refreshes Codex live; use `--offline` to read stored snapshots only |
| `codex [--interval 300]` | Saves a Codex snapshot once, or keeps polling every N seconds (minimum 60) |
| `claude-statusline` | The status-line hook: reads Claude's JSON on stdin, saves a snapshot, prints a short line |
| `install-claude` | Adds the hook to a project's Claude settings without overwriting an existing status line |

Snapshots live in `~/.local/state/solo-empire/ai-usage/` (change it with `--state-dir` or `AI_USAGE_STATE_DIR`).
A snapshot older than 15 minutes is marked **stale**. A window whose reset time has passed is never shown as available quota.

## Optional: HTTP API for dashboards

`server.py` serves the same data as JSON for a web dashboard or a small device display:

```bash
pip install pyjwt[crypto]
PYTHONPATH=vendor/service-auth python3 server.py   # http://127.0.0.1:3520/api/v1/ai-usage
```

On loopback it works without auth. Beyond localhost it requires short-lived RS256 JWTs.
The response also includes low-quota and "use it before it resets" alerts, plus monthly subscription cost/ROI fields.
The field reference is in the [AI Usage v1 contract](contracts/ai-usage.v1.md). Operator notes (Thai) are in [docs/OWNER.th.md](docs/OWNER.th.md).

## Tests

```bash
PYTHONPATH=vendor/service-auth:. uv run --no-project --with pytest --with 'pyjwt[crypto]' python -m pytest tests
```

## License

Apache-2.0
