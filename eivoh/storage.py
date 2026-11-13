"""Single-event-loop SQLite state. Transactions contain no awaits."""
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
            cur = self.db.execute("INSERT OR IGNORE INTO inbound(conversation_id,channel,chat_id,user_id,message_id,text) VALUES (?,?,?,?,?,?)",
                                  (conv, message.channel, message.chat_id, message.user_id, message.message_id, message.text))
            if not cur.rowcount:
                return None
            inbound_id = cur.lastrowid
            if command(message.text) not in {"/stop", "/new", "/model"}:
                active = self.db.execute("SELECT active_task_id FROM conversation WHERE id=?", (conv,)).fetchone()[0]
                if active is None:
                    active = self._create_task(conv)
                self.db.execute("INSERT INTO turn(inbound_id,conversation_id,task_id) VALUES (?,?,?)", (inbound_id, conv, active))
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

    def claim(self, inbound_id, max_recoveries):
        with self.db:
            turn = self.turn_for(inbound_id)
            recovering = turn["status"] == "running"
            if recovering and turn["recovery_count"] >= max_recoveries:
                self.finish(turn["id"], "failed", "Restart recovery limit reached")
                return None, True
            if not recovering:
                # /new is FIFO: queued turns bind to the active task at dispatch.
                self.db.execute("UPDATE turn SET task_id=(SELECT active_task_id FROM conversation WHERE id=turn.conversation_id) WHERE id=?", (turn["id"],))
            if recovering:
                self.db.execute("UPDATE turn SET recovery_count=recovery_count+1 WHERE id=?", (turn["id"],))
            self.db.execute("UPDATE turn SET status='running' WHERE id=?", (turn["id"],))
            self.db.execute("UPDATE inbound SET state='running' WHERE id=?", (inbound_id,))
        claimed = dict(self.turn_for(inbound_id))
        # The turn's observed ID remains evidence; routing, including recovery,
        # uses the task's current session, executor and model.
        task = self.db.execute("SELECT session_id, executor, model FROM task WHERE id=?", (claimed["task_id"],)).fetchone()
        claimed["resume_id"] = task["session_id"]
        claimed["executor"] = task["executor"]
        claimed["model"] = task["model"]
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
                    self.db.execute("UPDATE task SET session_id=? WHERE id=(SELECT task_id FROM turn WHERE id=?)", (event.session_id, turn_id))

    def finish(self, turn_id, status, error=None):
        with self.db:
            self.db.execute("UPDATE turn SET status=?,error=? WHERE id=? AND status IN ('queued','running')", (status, error, turn_id))
            self.db.execute("UPDATE inbound SET state=? WHERE id=(SELECT inbound_id FROM turn WHERE id=?) AND state != 'cancelled'",
                            ("cancelled" if status == "cancelled" else "done", turn_id))

    def _create_task(self, conversation_id, executor=None, model=None):
        task_id = self.db.execute("INSERT INTO task(conversation_id,executor,model) VALUES (?,?,?)",
                                  (conversation_id, executor or self.default_executor, model)).lastrowid
        self.db.execute("UPDATE conversation SET active_task_id=? WHERE id=?", (task_id, conversation_id))
        return task_id

    def new_task(self, inbound_id, executor=None):
        with self.db:
            row = self.inbound(inbound_id)
            self.db.execute("UPDATE task SET status='closed' WHERE id=(SELECT active_task_id FROM conversation WHERE id=?)", (row["conversation_id"],))
            self._create_task(row["conversation_id"], executor)
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))

    def finish_command(self, inbound_id):
        with self.db:
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))

    def active_task(self, conversation_id):
        return self.db.execute("SELECT task.* FROM task JOIN conversation ON task.id=conversation.active_task_id WHERE conversation.id=?",
                               (conversation_id,)).fetchone()

    def set_model(self, inbound_id, executor, model):
        """Change routing and consume the FIFO command in the same transaction."""
        with self.db:
            row = self.inbound(inbound_id)
            task = self.active_task(row["conversation_id"])
            switched = task is not None and task["executor"] != executor
            if task is None:
                self._create_task(row["conversation_id"], executor, model)
            else:
                self.db.execute("UPDATE task SET executor=?,model=?,session_id=? WHERE id=?",
                                (executor, model, None if switched else task["session_id"], task["id"]))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))
        return self.active_task(row["conversation_id"]), switched

    def transcript(self, task_id, before_inbound_id, limit=30000):
        """A bounded suffix of whole earlier turns, rendered oldest first."""
        blocks, size, truncated = [], 0, False
        rows = self.db.execute("SELECT turn.id,inbound.text FROM turn JOIN inbound ON inbound.id=turn.inbound_id "
                               "WHERE turn.task_id=? AND turn.inbound_id<? AND (turn.status IN ('completed','failed') "
                               "OR (turn.status='cancelled' AND EXISTS (SELECT 1 FROM raw_event WHERE turn_id=turn.id))) "
                               "ORDER BY turn.inbound_id DESC",
                               (task_id, before_inbound_id))
        for row in rows:
            result = self.stored_result(row["id"])
            answer = result[0] if result is not None else "[No stored final answer.]"
            block = f"User:\n{row['text']}\n\nAssistant:\n{answer}\n\n"
            if size + len(block) > limit:
                truncated = True
                break
            blocks.append(block)
            size += len(block)
        notice = ""
        if truncated:
            notice = "[Transcript truncated: oldest turns omitted.]\n\n"[:limit]
            while blocks and size + len(notice) > limit:
                size -= len(blocks.pop())
        text = notice + "".join(reversed(blocks))
        return text, len(blocks), truncated

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

    def stop(self, inbound_id):
        """Durably cancel all earlier pending work; later arrivals are unaffected."""
        with self.db:
            row = self.inbound(inbound_id)
            turns = self.db.execute("SELECT id,task_id,status FROM turn WHERE conversation_id=? AND inbound_id<? AND status IN ('queued','running')",
                                    (row["conversation_id"], inbound_id)).fetchall()
            queued = self.db.execute("SELECT COUNT(*) FROM inbound WHERE conversation_id=? AND id<? AND state='queued'",
                                     (row["conversation_id"], inbound_id)).fetchone()[0]
            self.db.execute("UPDATE turn SET status='cancelled' WHERE conversation_id=? AND inbound_id<? AND status IN ('queued','running')",
                            (row["conversation_id"], inbound_id))
            self.db.execute("UPDATE inbound SET state='cancelled' WHERE conversation_id=? AND id<? AND state IN ('queued','running')",
                            (row["conversation_id"], inbound_id))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))
        return [(t["id"], t["task_id"]) for t in turns if t["status"] == "running"], queued


def command(text):
    return text.strip().split(maxsplit=1)[0].split("@", 1)[0] if text.strip() else ""
