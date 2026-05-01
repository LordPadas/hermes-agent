# Hermes Agent — Development Guide

## Dev environment

```bash
source .venv/bin/activate   # or venv/ ; scripts/run_tests.sh probes .venv → venv → $HOME/.hermes/hermes-agent/venv
uv pip install -e ".[all,dev]"
```

## Load-bearing entrypoints

| File | Role |
|------|------|
| `run_agent.py` | `AIAgent` — ~60-param init, core conversation loop, tool dispatch |
| `model_tools.py` | Tool orchestration, `discover_builtin_tools()`, `handle_function_call()` |
| `toolsets.py` | Toolset definitions, `_HERMES_CORE_TOOLS`, platform-specific toolsets |
| `cli.py` | `HermesCLI` — prompt_toolkit REPL, slash-command dispatch to `COMMAND_REGISTRY` |
| `hermes_state.py` | `SessionDB` — SQLite + FTS5 session store |
| `hermes_constants.py` | `get_hermes_home()` / `display_hermes_home()` — **use these, never `Path.home()/".hermes"`** |
| `gateway/run.py` | `GatewayRunner` — all messaging platforms, `_create_adapter()`, `_is_user_authorized()` |
| `gateway/platforms/base.py` | `BasePlatformAdapter` ABC + `MessageEvent`, `SendResult` |
| `hermes_cli/commands.py` | `COMMAND_REGISTRY` — single source of truth for all slash commands (CLI, gateway, Telegram, Slack, autocomplete all derive from it) |
| `tools/registry.py` | `registry.register()` — all tools self-register; `discover_builtin_tools()` auto-imports `tools/*.py` |

## File dependency chain

```
tools/registry.py  (no deps)
       ↑
tools/*.py  (each calls registry.register() at import)
       ↑
model_tools.py  (imports registry, triggers discovery)
       ↑
run_agent.py, cli.py, batch_runner.py
```

## Adding a tool

1. Create `tools/your_tool.py` with `registry.register(name=..., toolset=..., schema=..., handler=...)`
2. Add toolset to `toolsets.py` (either `_HERMES_CORE_TOOLS` for all platforms, or a named toolset)
3. That's it — **no manual import list**. `discover_builtin_tools()` globs `tools/*.py` and imports any file with `registry.register()` at module level. Schema descriptions using file paths must call `display_hermes_home()` for profile awareness.

## Adding a slash command

1. Add `CommandDef("mycommand", "desc", "Session", aliases=("mc",), args_hint="[arg]")` to `COMMAND_REGISTRY` in `hermes_cli/commands.py`
2. Add handler in `cli.py` `process_command()` → `elif canonical == "mycommand":`
3. If gateway-available, add handler in `gateway/run.py`
4. **Adding an alias** requires ONLY the `aliases` tuple — dispatch, help, Telegram menu, autocomplete all update automatically.

## Adding a gateway platform (plugin path — zero core changes)

Create `plugins/platforms/<name>/` with `plugin.yaml` + `adapter.py`. Adapter subclasses `BasePlatformAdapter`, must implement: `connect()`, `disconnect()`, `send()`, `get_chat_info()`. Entry point `register(ctx)` calls `ctx.register_platform(...)`. Reference: `plugins/platforms/irc/`. Platform enum auto-discovers via `_missing_()`.

## Configuration

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` |
| `load_config()` | CLI subcommands, `hermes tools`, `hermes setup` | `hermes_cli/config.py` |
| Direct YAML | Gateway runtime | `gateway/run.py` + `gateway/config.py` |

If a new key works in CLI but not gateway (or vice versa), you used the wrong loader.

**Secrets** (API keys, tokens, passwords) → `.env` + `OPTIONAL_ENV_VARS` in `hermes_cli/config.py`.  
**All other settings** (timeouts, flags, paths, feature toggles) → `config.yaml` + `DEFAULT_CONFIG`.  
`TERMINAL_CWD` / `MESSAGING_CWD` in `.env` are **deprecated** — canonical is `terminal.cwd` in config.yaml.

## Profiles

`HERMES_HOME` is set by `_apply_profile_override()` before any imports. All `get_hermes_home()` calls automatically scope to the active profile.

**Rules:**
- Use `get_hermes_home()` for all paths — **never `Path.home() / ".hermes"`** (breaks profiles)
- Use `display_hermes_home()` for user-facing messages
- Tests mocking `Path.home()` must also set `HERMES_HOME`
- Gateway adapters must `acquire_scoped_lock()` in `connect()` / `release_scoped_lock()` in `disconnect()`
- `_get_profiles_root()` is HOME-anchored (not HERMES_HOME), so `hermes -p coder profile list` sees all profiles

## Prompt caching — do not break

Cache-breaking forces dramatically higher costs. **NEVER**:
- Alter past context mid-conversation
- Change toolsets mid-conversation  
- Reload memories or rebuild system prompts mid-conversation

Slash commands that mutate system-prompt state must be **cache-aware**: defer invalidation to next session, with `--now` flag for immediate. See `/skills install --now` for the canonical pattern.

## TUI (`hermes --tui`)

Process model: Node (Ink/React) ←stdio JSON-RPC→ Python (`tui_gateway`). TypeScript owns the screen; Python owns sessions, tools, and model calls.

Dev: `cd ui-tui && npm run dev` (watch mode), `npm test` (vitest), `npm run type-check`.

**Do not re-implement the transcript/composer in React for the dashboard.** The dashboard embeds real `hermes --tui` via `hermes_cli/pty_bridge.py` + PTY WebSocket. Structured React UI around the TUI (sidebars, inspectors) is OK.

## Testing

**ALWAYS use `scripts/run_tests.sh`** — not bare `pytest`. It enforces CI parity: unsets all credential env vars, TZ=UTC, LANG=C.UTF-8, `-n 4` workers (matches CI). Bare `pytest -n auto` on a 16+ core machine surfaces flakes CI never sees.

```bash
scripts/run_tests.sh                                  # full suite
scripts/run_tests.sh tests/gateway/                   # directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # single test
```

If you must run pytest directly: `python -m pytest tests/ -q -n 4`. Ignores `tests/integration/` and `tests/e2e/`.

**Do not write change-detector tests** — tests that snapshot model names, config version numbers, or enumeration counts. Write invariants instead (e.g., "every model in catalog has a context-length entry", not `len(MODELS) == 8`).

## Known pitfalls

- **Never hardcode `~/.hermes`** — use `get_hermes_home()` / `display_hermes_home()`. Source of 5 bugs.
- **No new `simple_term_menu` usage** — use `hermes_cli/curses_ui.py` instead (ghost-duplication rendering bugs in tmux).
- **No `\033[K`** in spinner/display code — leaks under `prompt_toolkit`'s `patch_stdout`. Use space-padding.
- **`_last_resolved_tool_names`** is process-global in `model_tools.py`. `delegate_tool._run_single_child()` saves/restores it around subagents — reads may be stale during child runs.
- **No hardcoded cross-tool references in schema descriptions** — tools from other toolsets may be unavailable. Add dynamically in `get_tool_definitions()`.
- **Gateway TWO message guards**: (1) base adapter queues when session active, (2) runner intercepts `/stop`, `/new`, `/approve`, `/deny`. New approval/control commands must bypass BOTH.
- **Squash merges from stale branches** silently revert recent fixes. Verify with `git diff HEAD~1..HEAD` after merge.
- **Don't wire dead code without E2E** — validate the real resolution chain with actual imports against a temp `HERMES_HOME`.
- **Tests never write to real `~/.hermes/`** — `tests/conftest.py` `_hermetic_environment` autouse fixture redirects `HERMES_HOME` to a per-test tempdir and blanks all credential/behavioral env vars.

## Test fixtures (important, non-obvious)

`tests/conftest.py` runs 5 autouse fixtures per test:
1. **`_hermetic_environment`** — blanks credential env vars, sets `HERMES_HOME` to tmp, TZ=UTC, PYTHONHASHSEED=0, disables AWS IMDS
2. **`_reset_module_state`** — clears `tools.approval._session_approved`, `interrupt._interrupted_threads`, gateway `session_context` ContextVars, terminal environments, file_tools read tracker, and tool registry caches
3. **`_reset_tool_registry_caches`** — calls `invalidate_check_fn_cache()` + `_clear_tool_defs_cache()`
4. **`_ensure_current_event_loop`** — provides event loop for sync tests calling `asyncio.get_event_loop()`
5. **`_enforce_test_timeout`** — 30s SIGALRM (Unix only)

The `_isolate_hermes_home` fixture is a backward-compat alias for `_hermetic_environment`.

## OpenCode Desktop — Hermes commands

When OpenCode Desktop is connected to Hermes Gateway via the `hermes-gateway.ts` plugin:

```
/hermes <message>     # send a message to Hermes
/hermes:start         # open a dedicated Hermes chat session
/hermes:stop          # close Hermes mode
/hermes:status        # check WebSocket connection status
/hermes:model         # show/switch Hermes model
/hermes:skills        # list available Hermes skills
```

## Profile test pattern
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```
