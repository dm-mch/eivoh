"""Single-event-loop SQLite state. Transactions contain no awaits."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .channels.base import Inbound
from .executors.base import Record


# Migration 1 is the initial, unreleased schema; no legacy layout is supported.
MIGRATIONS = (
    (
        """CREATE TABLE conversation (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, chat_id TEXT NOT NULL,
                active_task_id INTEGER REFERENCES task, UNIQUE(channel, chat_id));""",
        """CREATE TABLE task (
                id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversation,
                executor TEXT NOT NULL DEFAULT 'claude', session_id TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);""",
        """CREATE TABLE inbound (
                id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversation,
                channel TEXT NOT NULL, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                message_id TEXT NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','done','cancelled')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel, chat_id, message_id));""",
        """CREATE TABLE turn (
                task_id INTEGER NOT NULL REFERENCES task,
                id INTEGER PRIMARY KEY, inbound_id INTEGER NOT NULL UNIQUE REFERENCES inbound,
                conversation_id INTEGER NOT NULL REFERENCES conversation,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','running','completed','failed','cancelled')),
                session_id TEXT, recovery_count INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);""",
        """CREATE TABLE raw_event (
                id INTEGER PRIMARY KEY, turn_id INTEGER NOT NULL REFERENCES turn,
                sequence INTEGER NOT NULL, stream TEXT NOT NULL, raw_line TEXT,
                payload TEXT, kinds TEXT NOT NULL, events TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(turn_id, sequence));""",
        """CREATE TRIGGER raw_event_no_update BEFORE UPDATE ON raw_event
                BEGIN SELECT RAISE(ABORT, 'raw events are append-only'); END;""",
        """CREATE TRIGGER raw_event_no_delete BEFORE DELETE ON raw_event
                BEGIN SELECT RAISE(ABORT, 'raw events are append-only'); END;""",
    ),
    ("ALTER TABLE task ADD COLUMN model TEXT",),
    (
        # Keep task.session_id for the old layout, but never use/update it again.
        # The previous release still rejects user_version=3; this is not a downgrade.
        "ALTER TABLE task ADD COLUMN title TEXT",
        "ALTER TABLE task ADD COLUMN last_active TEXT",
        "ALTER TABLE turn ADD COLUMN executor TEXT",
        "ALTER TABLE turn ADD COLUMN model TEXT",
        "ALTER TABLE inbound ADD COLUMN reply_to TEXT",
        """CREATE TABLE task_session (
                task_id INTEGER NOT NULL REFERENCES task, executor TEXT NOT NULL,
                session_id TEXT NOT NULL, synced_inbound_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(task_id, executor))""",
        """CREATE TABLE outbound (
                channel TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
                task_id INTEGER NOT NULL REFERENCES task, turn_id INTEGER REFERENCES turn,
                PRIMARY KEY(channel, chat_id, message_id))""",
        """UPDATE turn SET executor=COALESCE(
                (SELECT CASE json_extract(payload,'$.executor')
                    WHEN 'ClaudeExecutor' THEN 'claude' WHEN 'CodexExecutor' THEN 'codex' END
                 FROM raw_event WHERE turn_id=turn.id AND stream='meta'
                    AND json_extract(payload,'$.executor') IS NOT NULL ORDER BY sequence DESC LIMIT 1),
                (SELECT executor FROM task WHERE id=turn.task_id AND session_id=turn.session_id)),
                model=(SELECT COALESCE(json_extract(payload,'$.requested_model'),json_extract(payload,'$.model')) FROM raw_event
                       WHERE turn_id=turn.id AND stream='meta'
                         AND json_extract(payload,'$.executor') IS NOT NULL ORDER BY sequence DESC LIMIT 1)""",
        # One session per executor the task has used: the newest observed id, synced
        # up to that executor's newest finished turn.
        """INSERT INTO task_session SELECT task_id,executor,
                (SELECT session_id FROM turn AS t WHERE t.task_id=turn.task_id AND t.executor=turn.executor
                    AND t.session_id IS NOT NULL ORDER BY t.inbound_id DESC LIMIT 1),
                COALESCE(MAX(CASE WHEN status IN ('completed','failed','cancelled') THEN inbound_id END),0)
                FROM turn WHERE executor IS NOT NULL AND session_id IS NOT NULL GROUP BY task_id,executor""",
        """UPDATE task SET last_active=MAX(created_at,
                COALESCE((SELECT MAX(created_at) FROM turn WHERE task_id=task.id),created_at),
                COALESCE((SELECT MAX(raw_event.created_at) FROM raw_event JOIN turn ON turn.id=turn_id
                          WHERE turn.task_id=task.id),created_at))""",
        "CREATE INDEX turn_task_queue ON turn(task_id,status,inbound_id)",
        "CREATE INDEX task_recent ON task(conversation_id,status,last_active DESC,id DESC)",
    ),
)


class Storage:
    def __init__(self, path: Path, default_executor="claude"):
        self.default_executor = default_executor
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        # FULL: acknowledging an inbound message must survive power loss too.
        # The low-volume owner-only V1 workload favours durability over throughput.
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise ValueError(f"Unsupported schema version {version}")
            for index in range(version, len(MIGRATIONS)):
                for statement in MIGRATIONS[index]:
                    self.db.execute(statement)
                if index == 2:
                    for row in self.db.execute("SELECT task.id,inbound.text FROM task JOIN turn ON turn.id="
                                               "(SELECT id FROM turn WHERE task_id=task.id ORDER BY inbound_id LIMIT 1) "
                                               "JOIN inbound ON inbound.id=turn.inbound_id"):
                        self.db.execute("UPDATE task SET title=? WHERE id=?", (default_title(row['text']), row['id']))
                self.db.execute(f"PRAGMA user_version={index + 1}")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def journal(self, message: Inbound):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO conversation(channel,chat_id) VALUES (?,?)", (message.channel, message.chat_id))
            conv = self.db.execute("SELECT id FROM conversation WHERE channel=? AND chat_id=?", (message.channel, message.chat_id)).fetchone()[0]
            cur = self.db.execute("INSERT OR IGNORE INTO inbound(conversation_id,channel,chat_id,user_id,message_id,text,reply_to) VALUES (?,?,?,?,?,?,?)",
                                  (conv, message.channel, message.chat_id, message.user_id, message.message_id, message.text, message.reply_to))
            if not cur.rowcount:
                return None
            inbound_id = cur.lastrowid
            # Unknown slash text (a path, an executor's own command) is an ordinary
            # message. A button press is always a command; the gateway rejects unknown ones.
            if not (message.button or command(message.text) in COMMANDS):
                target = self.reply_task(conv, message.reply_to) or self.active_task(conv)
                task_id = target['id'] if target else self._create_task(conv)
                title = default_title(message.text)
                self.db.execute("UPDATE task SET status='active',title=COALESCE(title,?),last_active=? WHERE id=?",
                                (title, now(), task_id))
                self.db.execute("INSERT INTO turn(inbound_id,conversation_id,task_id) VALUES (?,?,?)", (inbound_id, conv, task_id))
        return self.inbound(inbound_id)

    def inbound(self, inbound_id):
        return self.db.execute("SELECT * FROM inbound WHERE id=?", (inbound_id,)).fetchone()

    def pending(self, conversation_id=None):
        sql = "SELECT * FROM inbound WHERE state IN ('queued','running')"
        args = ()
        if conversation_id is not None:
            sql += " AND conversation_id=?"
            args = (conversation_id,)
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def turn_for(self, inbound_id):
        return self.db.execute("SELECT * FROM turn WHERE inbound_id=?", (inbound_id,)).fetchone()

    def pending_task(self, task_id):
        return self.db.execute("SELECT inbound.* FROM inbound JOIN turn ON turn.inbound_id=inbound.id "
                               "WHERE turn.task_id=? AND turn.status IN ('queued','running') ORDER BY inbound.id",
                               (task_id,)).fetchall()

    def claim(self, inbound_id, max_recoveries):
        with self.db:
            turn = self.turn_for(inbound_id)
            recovering = turn["status"] == "running"
            if recovering and turn["recovery_count"] >= max_recoveries:
                self.finish(turn["id"], "failed", "Restart recovery limit reached")
                return None, True
            # Binding is immutable. NULL executor denotes an unclaimed/legacy turn;
            # NULL model with a recorded executor means that executor's default.
            if turn['executor'] is None:
                task = self.task(turn['conversation_id'], turn['task_id'])
                self.db.execute("UPDATE turn SET executor=?,model=? WHERE id=?",
                                (task['executor'], task['model'], turn['id']))
            if recovering:
                self.db.execute("UPDATE turn SET recovery_count=recovery_count+1 WHERE id=?", (turn["id"],))
            self.db.execute("UPDATE turn SET status='running' WHERE id=?", (turn["id"],))
            self.db.execute("UPDATE inbound SET state='running' WHERE id=?", (inbound_id,))
        claimed = dict(self.turn_for(inbound_id))
        session = self.session(claimed['task_id'], claimed['executor'])
        claimed['resume_id'] = session['session_id'] if session else None
        return claimed, recovering

    def append(self, turn_id: int, record: Record, *, allow_result=True):
        # Shutdown errors/results remain in raw_line/payload/kinds for audit,
        # but cannot finalize an interrupted turn when normalized events replay.
        with self.db:
            seq = self.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM raw_event WHERE turn_id=?", (turn_id,)).fetchone()[0]
            self.db.execute("INSERT INTO raw_event(turn_id,sequence,stream,raw_line,payload,kinds,events) VALUES (?,?,?,?,?,?,?)",
                            (turn_id, seq, record.stream, record.raw_line, json.dumps(record.payload), json.dumps([e.kind for e in record.events]), json.dumps([{"kind": e.kind, "data": e.data} for e in record.events
                                        if allow_result or e.kind not in {"error", "turn_completed"}])))
            for event in record.events:
                if event.session_id:
                    self.db.execute("UPDATE turn SET session_id=? WHERE id=?", (event.session_id, turn_id))
                    self.db.execute("""INSERT INTO task_session(task_id,executor,session_id)
                        SELECT task_id,executor,? FROM turn WHERE id=? AND executor IS NOT NULL
                        ON CONFLICT(task_id,executor) DO UPDATE SET session_id=excluded.session_id""",
                        (event.session_id, turn_id))
            turn = self.db.execute("SELECT * FROM turn WHERE id=?", (turn_id,)).fetchone()
            if turn['status'] in {'completed', 'failed', 'cancelled'}:
                self._sync(turn)  # /stop may observe the session only while draining.

    def _sync(self, turn):
        # Only a turn that observed the session has shown it the request; a failed
        # launch must not hide turns the session has still not seen.
        if turn['executor'] and turn['session_id']:
            self.db.execute("UPDATE task_session SET synced_inbound_id=MAX(synced_inbound_id,?) WHERE task_id=? AND executor=?",
                            (turn['inbound_id'], turn['task_id'], turn['executor']))

    def finish(self, turn_id, status, error=None):
        with self.db:
            turn = self.db.execute("SELECT * FROM turn WHERE id=?", (turn_id,)).fetchone()
            changed = self.db.execute("UPDATE turn SET status=?,error=? WHERE id=? AND status IN ('queued','running')", (status, error, turn_id)).rowcount
            if changed:
                self._sync(turn)
                self.db.execute("UPDATE task SET last_active=? WHERE id=?", (now(), turn['task_id']))
            self.db.execute("UPDATE inbound SET state=? WHERE id=(SELECT inbound_id FROM turn WHERE id=?) AND state != 'cancelled'",
                            ("cancelled" if status == "cancelled" else "done", turn_id))

    def _create_task(self, conversation_id, executor=None, model=None, title=None):
        task_id = self.db.execute("INSERT INTO task(conversation_id,executor,model,title,last_active) VALUES (?,?,?,?,?)",
                                  (conversation_id, executor or self.default_executor, model, title, now())).lastrowid
        self.db.execute("UPDATE conversation SET active_task_id=? WHERE id=?", (task_id, conversation_id))
        return task_id

    def new_task(self, inbound_id, executor=None, title=None):
        with self.db:
            row = self.inbound(inbound_id)
            task_id = self._create_task(row['conversation_id'], executor, title=title)
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))
        return self.task(row['conversation_id'], task_id)

    def finish_command(self, inbound_id):
        with self.db:
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))

    def active_task(self, conversation_id):
        return self.db.execute("SELECT task.* FROM task JOIN conversation ON task.id=conversation.active_task_id WHERE conversation.id=?",
                               (conversation_id,)).fetchone()

    def task(self, conversation_id, task_id):
        return self.db.execute("SELECT * FROM task WHERE conversation_id=? AND id=?", (conversation_id, task_id)).fetchone()

    def tasks(self, conversation_id, include_closed=False):
        return self.db.execute("SELECT * FROM task WHERE conversation_id=? " +
                               ("" if include_closed else "AND status='active' ") +
                               "ORDER BY last_active DESC,id DESC", (conversation_id,)).fetchall()

    def reply_task(self, conversation_id, message_id):
        if message_id is None:
            return None
        row = self.db.execute("SELECT turn.task_id FROM inbound JOIN turn ON turn.inbound_id=inbound.id "
                              "WHERE inbound.conversation_id=? AND inbound.message_id=? UNION ALL "
                              "SELECT outbound.task_id FROM outbound JOIN conversation USING(channel,chat_id) "
                              "WHERE conversation.id=? AND outbound.message_id=? LIMIT 1",
                              (conversation_id, message_id, conversation_id, message_id)).fetchone()
        return self.task(conversation_id, row[0]) if row else None

    def last_exchange(self, task_id):
        """Newest user text and newest stored answer of a task, for reminders."""
        turns = self.db.execute("SELECT turn.id,inbound.text FROM turn JOIN inbound ON inbound.id=turn.inbound_id "
                                "WHERE task_id=? ORDER BY inbound.id DESC", (task_id,)).fetchall()
        for turn in turns:
            result = self.stored_result(turn['id'])
            if result is not None:
                return turns[0]['text'], result[0]
        return (turns[0]['text'] if turns else None), None

    def remember_outbound(self, channel, chat_id, message_ids, task_id, turn_id=None):
        """Map bot messages to their task, and answers to their turn, for replies."""
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO outbound VALUES (?,?,?,?,?)",
                                [(channel, chat_id, mid, task_id, turn_id) for mid in message_ids])

    def switch_task(self, inbound_id, task_id):
        with self.db:
            row = self.inbound(inbound_id)
            self.db.execute("UPDATE task SET status='active',last_active=? WHERE id=? AND conversation_id=?",
                            (now(), task_id, row['conversation_id']))
            self.db.execute("UPDATE conversation SET active_task_id=? WHERE id=?", (task_id, row['conversation_id']))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))

    def title_task(self, inbound_id, task_id, title):
        with self.db:
            self.db.execute("UPDATE task SET title=? WHERE id=?", (title, task_id))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))

    def set_model(self, inbound_id, executor, model, task_id=None):
        with self.db:
            row = self.inbound(inbound_id)
            task = self.task(row['conversation_id'], task_id) if task_id is not None else self.active_task(row['conversation_id'])
            switched = task is not None and task['executor'] != executor
            if task is None:
                task_id = self._create_task(row['conversation_id'], executor, model)
            else:
                task_id = task['id']
                self.db.execute("UPDATE task SET executor=?,model=? WHERE id=?", (executor, model, task_id))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))
        return self.task(row['conversation_id'], task_id), switched

    def session(self, task_id, executor):
        return self.db.execute("SELECT * FROM task_session WHERE task_id=? AND executor=?", (task_id, executor)).fetchone()

    def missed_transcript(self, task_id, executor, before_inbound_id):
        session = self.session(task_id, executor)
        return self.transcript(task_id, before_inbound_id, after_inbound_id=session['synced_inbound_id'] if session else 0)

    def transcript(self, task_id, before_inbound_id, limit=30000, after_inbound_id=0):
        """Recent whole turns, or excerpts of the newest oversized turn."""
        limit = max(0, limit)
        blocks, size, truncated, newest = [], 0, False, None
        rows = self.db.execute("SELECT turn.id,inbound.text FROM turn JOIN inbound ON inbound.id=turn.inbound_id "
                               "WHERE turn.task_id=? AND turn.inbound_id<? AND turn.inbound_id>? AND (turn.status IN ('completed','failed') "
                               "OR (turn.status='cancelled' AND EXISTS (SELECT 1 FROM raw_event WHERE turn_id=turn.id))) "
                               "ORDER BY turn.inbound_id DESC",
                               (task_id, before_inbound_id, after_inbound_id))
        for row in rows:
            result = self.stored_result(row["id"])
            answer = result[0] if result is not None else "[No stored final answer.]"
            if newest is None:
                newest = (row['text'], answer)
            block = f"User:\n{row['text']}\n\nAssistant:\n{answer}\n\n"
            if size + len(block) > limit:
                truncated = True
                break
            blocks.append(block)
            size += len(block)
        notice = ""
        if truncated:
            notice = "[Transcript truncated: older turns or text omitted.]\n\n"[:limit]
            while blocks and size + len(notice) > limit:
                size -= len(blocks.pop())
            if not blocks and newest is not None:
                block = excerpt_turn(*newest, limit - len(notice))
                if block:
                    blocks.append(block)
        text = notice + "".join(reversed(blocks))
        return text, len(blocks), truncated

    def prepare_prompt(self, turn, request):
        """Journal the original assembled input before launch; reuse on recovery."""
        for row in self.db.execute("SELECT payload FROM raw_event WHERE turn_id=? AND stream='meta' ORDER BY sequence",
                                   (turn['id'],)):
            payload = json.loads(row[0])
            if isinstance(payload, dict) and payload.get('type') == 'turn_prompt':
                return payload['prompt']
        prompt = request
        transcript, _, _ = self.missed_transcript(turn['task_id'], turn['executor'], turn['inbound_id'])
        if transcript:
            # A resumed session is told why it has not seen these turns; otherwise
            # a careful model treats them as pasted text of unknown origin.
            header = ("Since your last turn in this conversation, the same user continued this task with another "
                      "model. Eivoh, the gateway running you, recorded those turns; they are part of this "
                      "conversation's history:" if self.session(turn['task_id'], turn['executor'])
                      else "Earlier conversation in this task (transcript from Eivoh):")
            prompt = f"{header}\n\n{transcript}End of transcript.\n\nCurrent request:\n{request}"
        self.append(turn['id'], Record(None, {'type': 'turn_prompt', 'prompt': prompt}, stream='meta'))
        return prompt

    def stored_result(self, turn_id):
        """Replay normalized events after a crash, without invoking the executor."""
        text, error, completed, deltas = "", None, False, False
        for row in self.db.execute("SELECT events FROM raw_event WHERE turn_id=? ORDER BY sequence", (turn_id,)):
            for event in json.loads(row[0]):
                kind, data = event["kind"], event["data"]
                if kind == "session_started":
                    # Each resumed attempt has its own output and errors.
                    text, error, completed, deltas = "", None, False, False
                elif kind == "text_delta":
                    text += data["text"]
                    deltas = True
                elif kind == "message":
                    if not deltas:
                        text += data["text"]
                    deltas = False
                elif kind == "error":
                    error = error or str(data.get("message", "Executor error"))
                elif kind == "turn_completed":
                    completed = True
                    text = data.get("text") or text
                    if data["status"] != "completed" and error is None:
                        error = "Executor reported failure"
        return (text, error) if completed else None

    def stop(self, inbound_id, task_id=None, close=False):
        """Cancel before killing; only earlier turns in the requested scope."""
        with self.db:
            row = self.inbound(inbound_id)
            turns = self.db.execute("SELECT * FROM turn WHERE conversation_id=? AND inbound_id<? "
                                    "AND status IN ('queued','running')" + (" AND task_id=?" if task_id is not None else ""),
                                    (row['conversation_id'], inbound_id) + ((task_id,) if task_id is not None else ())).fetchall()
            for turn in turns:
                self.db.execute("UPDATE turn SET status='cancelled' WHERE id=?", (turn['id'],))
                self.db.execute("UPDATE inbound SET state='cancelled' WHERE id=?", (turn['inbound_id'],))
                self._sync(turn)
                self.db.execute("UPDATE task SET last_active=? WHERE id=?", (now(), turn['task_id']))
            if close:
                self.db.execute("UPDATE task SET status='closed' WHERE id=?", (task_id,))
                switched = self.db.execute("""UPDATE conversation SET active_task_id=(SELECT id FROM task
                    WHERE conversation_id=? AND status='active' ORDER BY last_active DESC,id DESC LIMIT 1)
                    WHERE id=? AND active_task_id=?""", (row['conversation_id'], row['conversation_id'], task_id)).rowcount
                if switched:
                    self.db.execute("UPDATE task SET last_active=? WHERE id=(SELECT active_task_id FROM conversation WHERE id=?)",
                                    (now(), row['conversation_id']))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))
        return [(t['id'], t['task_id']) for t in turns if t['status'] == 'running'], sum(t['status'] == 'queued' for t in turns)


COMMANDS = frozenset({'/new', '/tasks', '/task', '/model', '/stop', '/close', '/title'})


def now():
    return datetime.now(timezone.utc).isoformat(sep=' ', timespec='microseconds')


def default_title(text):
    if text.startswith('[Voice message, speech-to-text]\n'):
        text = text.split('\n', 1)[1]
    text = ' '.join(text.split())
    return text if len(text) <= 40 else text[:39] + '…'


def command(text):
    return text.strip().split(maxsplit=1)[0].split("@", 1)[0] if text.strip() else ""


def excerpt_turn(user, assistant, limit):
    """Keep both roles and each text's beginning/end, including marker costs."""
    marker = "\n[Text truncated]\n"
    overhead = len("User:\n\n\nAssistant:\n\n\n")
    budget = limit - overhead
    minimum = [min(len(text), len(marker) + 2) for text in (user, assistant)]
    if budget < sum(minimum):
        return ""  # Too small to show both roles and honest excerpts.
    user_budget = min(len(user), max(minimum[0], budget // 2))
    assistant_budget = min(len(assistant), budget - user_budget)
    if assistant_budget < minimum[1]:
        user_budget -= minimum[1] - assistant_budget
        assistant_budget = minimum[1]
    user_budget = min(len(user), budget - assistant_budget)

    def trim(text, available):
        if len(text) <= available:
            return text
        keep = available - len(marker)
        head = (keep + 1) // 2
        return text[:head] + marker + text[-(keep - head):]

    return f"User:\n{trim(user, user_budget)}\n\nAssistant:\n{trim(assistant, assistant_budget)}\n\n"
