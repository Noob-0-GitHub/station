"""Tool definitions shown to the model. Longer how-tos live in skills and docs."""

ONBOARDING_TOOLS = [
    {
        "name": "register",
        "description": "Agree to the Station terms of use and register an account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name you choose for yourself. Anything that sounds like a person's name; "
                                   "AI product or model names are not accepted.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "exit",
        "description": "Decline the terms of use and leave. The program ends.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Optional: why you are leaving"}},
        },
    },
]

LIFE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a bash command in your home directory; returns the exit code and output. "
                       "Redirect the output of background programs to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Seconds, default 120, at most 600"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "schedule",
        "description": (
            "Plan when to wake. When you stop (reply without calling any tool), the turn ends and you sleep "
            "until the next scheduled wake. A daily wake may also be set in runtime config. Times use this server's time zone. "
            "interval: wake this thread every N minutes, looping by default; once=true wakes once; minutes=0 turns it off. "
            "cron: wake on a cron expression (minute hour day month weekday); once=true removes it after it fires. "
            "One entry per expression: adding the same one again updates it. Alarms due at the same time wake you once. "
            "remove: delete a cron entry by id. list: show everything and when you would wake next. "
            "note is shown to you when you wake. hook is a command run in your home just before waking; "
            "exit code 0 skips that wake and you keep sleeping. Messages from others do not wake you."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["interval", "cron", "remove", "list"]},
                "minutes": {"type": "number", "description": "interval: minutes between wakes; 0 turns it off"},
                "once": {"type": "boolean", "description": "Fire only once. Default false"},
                "expr": {
                    "type": "string",
                    "description": "cron: '30 22 * * *' is 22:30 every day, '0 9 * * 1-5' is 09:00 on weekdays, "
                                   "'0 15 1 11 *' with once=true is 15:00 on November 1, once",
                },
                "note": {"type": "string", "description": "Optional note, shown when you wake"},
                "hook": {"type": "string", "description": "Optional command run before waking; exit 0 skips this wake"},
                "id": {"type": "integer", "description": "remove: the entry id"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "focus",
        "description": (
            "Put your attention on one thing. new: open a thread for it and switch to it. "
            "switch: move to another thread. close: end the current thread and return where it came from. "
            "Each thread has its own context, kept while you are away; the conversation does not carry over. "
            "new/switch/close may carry a short context into the thread you go to, the way you would brief someone "
            "taking over. main is your everyday thread and cannot be closed. "
            "model: inspect or change a thread's provider/model/effort/fallback without switching. "
            "No override means the global model configuration; model selection and failover state are per thread. "
            "view: inspect or change hidden board names/tags on the current thread or an id without switching. "
            "new/switch also accept hide_boards/hide_tags. Lists replace that field; [] clears it. "
            "New threads start with no hidden panels; hiding is not inherited from the parent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["new", "switch", "close", "model", "view"]},
                "title": {"type": "string", "description": "new: what this is"},
                "goal": {"type": "string", "description": "new: what done looks like; may be empty"},
                "id": {"type": "string", "description": "switch/model/view: an existing thread id; new assigns t1, t2, ..."},
                "provider": {"type": "string", "description": "Optional new/switch/model: select with model; stored only on that thread"},
                "model": {"type": "string", "description": "Optional new/switch/model: select with provider"},
                "effort": {"type": ["string", "null"], "description": "Optional reasoning effort for this thread"},
                "fallback": {"type": "array", "description": "Optional per-thread fallback; omitted inherits global, [] disables",
                             "items": {"type": "object", "properties": {"provider": {"type": "string"},
                                       "model": {"type": "string"}, "effort": {"type": ["string", "null"]}},
                                       "required": ["provider", "model"], "additionalProperties": False}},
                "inherit": {"type": "boolean", "description": "new/switch/model: true clears all thread model overrides; do not combine with model settings"},
                "hide_boards": {"type": "array", "items": {"type": "string"},
                                "description": "new/switch/view: replace this thread's hidden panel names; [] clears names. The hidden overview cannot be hidden"},
                "hide_tags": {"type": "array", "items": {"type": "string"},
                              "description": "new/switch/view: replace this thread's hidden tags; any exact matching tag hides a panel; [] clears tags"},
                "context": {"type": "string", "description": "Optional new/switch/close: a sentence or two for the "
                            "thread you go to: what you were doing, what came of it, what to pick up "
                            "(under 2,000 characters)"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "compress",
        "description": (
            "Compress this thread's context: you first write the conversation so far as a handoff, then it replaces "
            "the older part and only the last few turns stay verbatim. Station also does this automatically when the "
            "context gets long; call it yourself when something is wrapped up and you want to tidy up early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"focus": {"type": "string", "description": "Optional: what the handoff must keep"}},
        },
    },
    {
        "name": "diary",
        "description": "Your diary. write adds a timed entry, read reads a day (default today), list lists the dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["write", "read", "list"]},
                "text": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "todo",
        "description": "Your todo list; open items are shown on the board. add, done, drop, list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "done", "drop", "list"]},
                "text": {"type": "string"},
                "id": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "board",
        "description": "Manage board panels: list / read / write / patch / delete / hide / show. "
                       "hide/show uses one name or tag, only in this thread; show all=true resets this thread. "
                       "Hidden command panels do not run. Files remain readable. See the board skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read", "write", "patch", "delete", "hide", "show"]},
                "name": {"type": "string"},
                "tag": {"type": "string", "description": "hide/show: exact tag; do not combine with name"},
                "all": {"type": "boolean", "description": "show only: clear all hidden-name/tag rules in this thread"},
                "text": {"type": "string", "description": "write: the full content"},
                "old": {"type": "string", "description": "patch: text to replace; must appear exactly once"},
                "new": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "skill",
        "description": "Read a skill. The skills panel on the board lists them all. You can write your own in ~/skills/.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "file": {"type": "string", "description": "Optional: another file in the skill's directory"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "web",
        "description": "Go online. search takes a query; extract reads the text of up to 5 urls. "
                       "For clicking, logging in or dynamic pages, use the browser skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "extract"]},
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "Default 5, at most 10"},
                "urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
        },
    },
    {
        "name": "view",
        "description": "Look at an image file in your home (PNG, JPEG, WebP, GIF): a photo, a drawing, a browser "
                       "screenshot. Large images are scaled down. Each image costs about as much context as "
                       "6,000 characters of text.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. ~/shots/page.png"}},
            "required": ["path"],
        },
    },
    {
        "name": "memory",
        "description": "Your long-term memory: your diary, every conversation (also from before compressions) "
                       "and notes you saved. search finds by meaning and by words; show gives one memory in full; "
                       "add saves a note; forget deletes a note. Searching sends the query to OpenAI and costs a "
                       "little. The same is in the shell as `station memory`.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "show", "add", "forget"]},
                "query": {"type": "string", "description": "search: what you are looking for"},
                "kind": {"type": "string", "enum": ["diary", "chat", "note"], "description": "search: only this kind"},
                "since": {"type": "string", "description": "search: a date (2026-10-31) or an age (7d, 12h)"},
                "limit": {"type": "integer", "description": "search: default 8"},
                "id": {"type": "integer", "description": "show, forget: the memory id from a search"},
                "text": {"type": "string", "description": "add: the note"},
            },
            "required": ["action"],
        },
    },
]
