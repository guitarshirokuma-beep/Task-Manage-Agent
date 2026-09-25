# taskagent

A minimal MCP server that manages a personal to-do list.

It exposes five tools — add, list, complete, update, delete — over stdio, so an
MCP host such as Claude Desktop can read and change your tasks in conversation.
Tasks are stored in a single JSON file. There is no database, no server process
to keep running and no network access.

```
MCP host (e.g. Claude Desktop)
        │  stdio
        ▼
   taskagent  ──►  ~/.taskagent/tasks.json
```

## Requirements

- Python 3.11+ (developed on 3.12)
- [uv](https://docs.astral.sh/uv/) (optional, but the lockfile is for uv)

## Setup

```bash
git clone https://github.com/guitarshirokuma-beep/Task-Manage-Agent.git
cd Task-Manage-Agent
uv sync
uv run taskagent          # starts the server on stdio; Ctrl-C to stop
```

With pip instead of uv:

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows;  source .venv/bin/activate on macOS/Linux
pip install -e .
taskagent
```

Running it directly like this is only a smoke test — an MCP server speaks a
protocol on stdin/stdout and is meant to be launched by a host.

### Register it with Claude Desktop

Add the following to `claude_desktop_config.json`
(Windows: `%APPDATA%\Claude\`, macOS: `~/Library/Application Support/Claude/`),
replacing the path with where you cloned the repository, then restart the app.

```json
{
  "mcpServers": {
    "taskagent": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/Task-Manage-Agent", "run", "taskagent"]
    }
  }
}
```

### Where the data lives

`~/.taskagent/tasks.json` by default. Set `TASKAGENT_DATA_PATH` to use another
location — for example a folder synced across machines:

```json
"env": { "TASKAGENT_DATA_PATH": "/absolute/path/to/tasks.json" }
```

The file is plain JSON and safe to read or edit by hand. It is never written
inside the repository, and `tasks.json` is in `.gitignore`.

### Tests

```bash
uv run pytest
```

## Tools

| Tool | Arguments | What it does |
| :--- | :--- | :--- |
| `add_task` | `title` (required)<br>`due` `YYYY-MM-DD`<br>`priority` `low`/`normal`/`high` (default `normal`)<br>`tags` list of strings | Adds one task and returns its generated id |
| `list_tasks` | `status` `open`/`done`/`all` (default `open`)<br>`tag`<br>`due_before` `YYYY-MM-DD` | Returns matching tasks, sorted by due date then priority. The first line is today's date |
| `complete_task` | `id` (required) | Marks a task as done |
| `update_task` | `id` (required)<br>`title`, `due`, `priority`, `tags`, `clear_due` | Changes only the fields that are passed |
| `delete_task` | `id` (required) | Removes a task permanently |

Task ids are random 8-character strings, so a caller cannot guess one. Every
tool that takes an `id` says in its description to call `list_tasks` first.

## Design notes

**A JSON file, not a database.** A personal to-do list is small and has a single
writer. A file keeps the whole state readable and hand-editable, and removes a
dependency that would have to be installed before anything works.

**Relative dates are the caller's job.** The server accepts only `YYYY-MM-DD`
and rejects "tomorrow". Interpreting a relative date needs to know the current
date, the user's timezone and what "next Friday" means to them — all of which
the calling model has and the server does not. Keeping that out of the server
leaves it with one unambiguous contract. To make the conversion easy,
`list_tasks` prints today's date on its first line, and a rejected date says so
in the error.

**Problems the caller can fix are returned as text, not raised.** An unknown id
answers with `No task with id '...' was found. Nothing was changed. Call
list_tasks ... then try again.` A tag that matches nothing lists the tags that
do exist. A failed call gives a model nothing to work with; a sentence
explaining what was wrong lets it correct itself on the next call. Real faults
— an unreadable or corrupt data file — are still raised.

**Tool descriptions are written for a reader who has to choose between them.**
Each one says when to use it *and* when not to: `complete_task` points to
`delete_task` and the other way round, `add_task` says to add one task per call.
`ToolAnnotations` marks which tools only read and which destroy data.

**Three layers, so the logic can be tested without MCP.** `models.py` is data
and validation, `store.py` is CRUD over a `Backend` protocol, and `server.py`
only translates between MCP and the store. All but one of the tests run against
`TaskStore` directly. Swapping `JsonFileBackend` for another backend — cloud
storage, a database — touches neither the tool definitions nor the task logic.

**Writes are atomic, with a retry for Windows.** Each save writes a temporary
file in the same folder, fsyncs it and then replaces the target, so an
interrupted write cannot leave a half-written `tasks.json`. On Windows the
replace step retries a few times, because antivirus and file-sync clients hold
the target open for a moment and `os.replace` fails with `PermissionError`.

**Every operation re-reads the file.** Nothing is cached in memory, so edits
made by hand or by a sync client while the server is running are picked up.

**`clear_due` is a separate flag.** `due=None` already means "leave it alone",
so removing a deadline needs its own argument rather than an overloaded `None`.

## Not done yet

- **Concurrency**: two processes writing at the same time is last-write-wins.
  Each write is atomic, but there is no locking.
- **No search by title.** Filtering is by status, a single exact tag, and a due
  date cutoff.
- **No pagination.** `list_tasks` returns everything that matches.
- **No recurring tasks, subtasks, reminders or notifications.**
- **No migration path.** The data file is `"version": 1`; a future change of
  schema would be refused rather than upgraded.
- **stdio only**, single local user. No transport over HTTP and no auth.
- Developed and tested on Windows. It should run on macOS and Linux, but that
  has not been verified.

## License

MIT
