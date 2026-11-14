"""Single-event-loop SQLite state. Transactions contain no awaits."""
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import uuid

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
    (
        "ALTER TABLE task ADD COLUMN title_source TEXT NOT NULL DEFAULT 'fallback'",
        "ALTER TABLE task ADD COLUMN title_attempted INTEGER NOT NULL DEFAULT 0",
        # Old titles have no provenance: even a first-message-looking title may
        # have been explicitly chosen. Preserve all existing tasks conservatively.
        "UPDATE task SET title_source='manual'",
    ),
    (
        "ALTER TABLE turn ADD COLUMN force_queue INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE steering (
            inbound_id INTEGER PRIMARY KEY REFERENCES inbound,
            parent_turn_id INTEGER NOT NULL REFERENCES turn,
            input_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN ('sending','accepted','queued','unknown')),
            detail TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX steering_parent ON steering(parent_turn_id,state)",
    ),
    (
        "ALTER TABLE inbound ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE inbound ADD COLUMN preparation TEXT NOT NULL DEFAULT 'ready'",
        "ALTER TABLE inbound ADD COLUMN prepared_text TEXT",
        "CREATE TABLE update_receipt (channel TEXT NOT NULL, update_id INTEGER NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(channel,update_id))",
        "CREATE TABLE poll_cursor (channel TEXT PRIMARY KEY, next_id INTEGER NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE TABLE turn_output (turn_id INTEGER PRIMARY KEY REFERENCES turn, text TEXT NOT NULL, artifacts TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0)",
        """CREATE TABLE delivery (
            id INTEGER PRIMARY KEY, channel TEXT NOT NULL, chat_id TEXT NOT NULL,
            logical_id TEXT NOT NULL, part TEXT NOT NULL, task_id INTEGER REFERENCES task, turn_id INTEGER REFERENCES turn,
            payload TEXT NOT NULL, message_id TEXT, state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1, cancelled INTEGER NOT NULL DEFAULT 0,
            UNIQUE(channel,chat_id,logical_id,part))""",
        """CREATE TABLE delivery_attempt (
            id INTEGER PRIMARY KEY, delivery_id INTEGER NOT NULL REFERENCES delivery,
            operation TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'sending',
            message_id TEXT, detail TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        # V5 already retained the physical ids of interrupted answers for reply
        # routing. Reuse these ids when their answer resumes after migration.
        """INSERT INTO delivery(channel,chat_id,logical_id,part,task_id,turn_id,payload,message_id,state)
            SELECT outbound.channel,outbound.chat_id,'turn:' || turn_id,
                CAST(ROW_NUMBER() OVER (PARTITION BY turn_id ORDER BY CAST(message_id AS INTEGER))-1 AS TEXT),
                outbound.task_id,turn_id,'{"kind":"text","text":""}',message_id,'sent'
            FROM outbound JOIN turn ON turn.id=outbound.turn_id
            WHERE outbound.channel='telegram' AND message_id!='' AND message_id NOT GLOB '*[^0-9]*'
                AND turn.status IN ('queued','running')""",
    ),
    (
        "CREATE INDEX inbound_message_number ON inbound(channel,chat_id,CAST(message_id AS INTEGER))",
        "CREATE INDEX delivery_message_number ON delivery(channel,chat_id,CAST(message_id AS INTEGER))",
    ),
    (
        "ALTER TABLE task ADD COLUMN effort TEXT",
        "ALTER TABLE turn ADD COLUMN effort TEXT",
    ),
    # A task's working folder, fixed at creation: executor sessions are bound to it.
    ("ALTER TABLE task ADD COLUMN workdir TEXT",),
)


class LegacyWorkdirRequired(ValueError):
    """Old tasks have no recorded folder; guessing one would break their resume."""


class Storage:
    def __init__(self, path: Path, default_executor="claude", *, task_workdir=None, legacy_workdir=None):
        """task_workdir: working folder of tasks created from now on.
        legacy_workdir: backfilled into tasks that have none (created before V3.1)."""
        self.default_executor = default_executor
        self.task_workdir = str(Path(task_workdir).resolve()) if task_workdir is not None else None
        self.state_dir = path.parent.resolve()
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
            if legacy_workdir is not None:
                self.db.execute("UPDATE task SET workdir=? WHERE workdir IS NULL", (str(Path(legacy_workdir).resolve()),))
            if task_workdir is not None:
                # Rolled back with the migrations below: a later start with the folder repairs it.
                missing = self.db.execute("SELECT COUNT(*) FROM task WHERE workdir IS NULL").fetchone()[0]
                if missing:
                    raise LegacyWorkdirRequired(f"EIVOH_WORKDIR must be set: {missing} task(s) created before "
                                                "the agent's home existed have no working folder")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def journal(self, message: Inbound):
        with self.db:
            if message.update_id is not None:
                self.receipt(message.channel, message.update_id)
            self.db.execute("INSERT OR IGNORE INTO conversation(channel,chat_id) VALUES (?,?)", (message.channel, message.chat_id))
            conv = self.db.execute("SELECT id FROM conversation WHERE channel=? AND chat_id=?", (message.channel, message.chat_id)).fetchone()[0]
            cur = self.db.execute("INSERT OR IGNORE INTO inbound(conversation_id,channel,chat_id,user_id,message_id,text,reply_to,attachments,preparation) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (conv, message.channel, message.chat_id, message.user_id, message.message_id, message.text, message.reply_to,
                                   json.dumps(message.attachments), 'pending' if message.attachments else 'ready'))
            if not cur.rowcount:
                return None
            inbound_id = cur.lastrowid
            # Unknown slash text (a path, an executor's own command) is an ordinary
            # message. A button press is always a command; the gateway rejects unknown ones.
            if message.attachments or not (message.button or command(message.text) in COMMANDS) or (command(message.text) == '/q' and len(message.text.split(maxsplit=1)) == 2):
                target = self.reply_task(conv, message.reply_to) or self.active_task(conv)
                task_id = target['id'] if target else self._create_task(conv)
                title = default_title(message.text or 'Attached file')
                self.db.execute("UPDATE task SET status='active',title=COALESCE(title,?),last_active=? WHERE id=?",
                                (title, now(), task_id))
                self.db.execute("INSERT INTO turn(inbound_id,conversation_id,task_id,force_queue) VALUES (?,?,?,?)",
                                (inbound_id, conv, task_id, not message.attachments and command(message.text) == '/q'))
        return self.inbound(inbound_id)

    def inbound(self, inbound_id):
        return self.db.execute("SELECT * FROM inbound WHERE id=?", (inbound_id,)).fetchone()

    def receipt(self, channel, update_id):
        # Caller owns the transaction; never acknowledge an uncommitted admission.
        self.db.execute("INSERT OR REPLACE INTO update_receipt VALUES (?,?,?)", (channel, update_id, now()))

    def saw_update(self, channel, update_id):
        # Telegram may choose fresh random ids after a week without updates.
        return self.db.execute("SELECT 1 FROM update_receipt WHERE channel=? AND update_id=? AND received_at>?",
            (channel, update_id, (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(sep=' '))).fetchone() is not None

    def advance_cursor(self, channel, next_id):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO poll_cursor VALUES (?,?,?)", (channel, next_id, now()))

    def cursor(self, channel):
        row = self.db.execute("SELECT * FROM poll_cursor WHERE channel=?", (channel,)).fetchone()
        if row and (datetime.now(timezone.utc) - datetime.fromisoformat(row['updated_at'])).total_seconds() < 6 * 86400:
            return row['next_id']
        return None

    def prepared(self, inbound_id, text, attachments, failed=False):
        with self.db:
            return self.db.execute("UPDATE inbound SET prepared_text=?,attachments=?,preparation=? WHERE id=? AND state='queued'",
                (text, json.dumps(attachments), 'failed' if failed else 'ready', inbound_id)).rowcount != 0

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
                               "WHERE turn.task_id=? AND turn.status IN ('queued','running') AND NOT EXISTS "
                               "(SELECT 1 FROM steering WHERE inbound_id=inbound.id AND state IN ('sending','accepted')) ORDER BY inbound.id",
                               (task_id,)).fetchall()

    def steering(self, inbound_id):
        return self.db.execute("SELECT * FROM steering WHERE inbound_id=?", (inbound_id,)).fetchone()

    def reserve_steer(self, parent_id, inbound_id):
        """Claim only the first queued input, with unchanged launch settings."""
        with self.db:
            parent = self.db.execute("SELECT * FROM turn WHERE id=?", (parent_id,)).fetchone()
            child = self.turn_for(inbound_id)
            task = self.task(parent['conversation_id'], parent['task_id'])
            if (parent['status'] != 'running' or child['status'] != 'queued' or child['force_queue'] or
                    self.inbound(inbound_id)['preparation'] != 'ready' or
                    self.steering(inbound_id) or parent['executor'] != task['executor'] or parent['model'] != task['model'] or
                    parent['effort'] != task['effort']):
                return None
            earlier = self.db.execute("SELECT 1 FROM turn WHERE task_id=? AND inbound_id<? AND id!=? "
                "AND status IN ('queued','running') AND NOT EXISTS (SELECT 1 FROM steering "
                "WHERE inbound_id=turn.inbound_id AND (state='accepted' OR (state='sending' AND parent_turn_id=?)))",
                (parent['task_id'], inbound_id, parent_id, parent_id)).fetchone()
            if earlier:
                return None
            input_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO steering(inbound_id,parent_turn_id,input_id,state) VALUES (?,?,?,'sending')",
                            (inbound_id, parent_id, input_id))
            self.db.execute("UPDATE turn SET executor=?,model=?,effort=? WHERE id=?",
                            (parent['executor'], parent['model'], parent['effort'], child['id']))
            return input_id

    def steer_outcome(self, inbound_id, status, detail=''):
        with self.db:
            # Acceptance evidence wins over a concurrent timeout/shutdown.
            changed = self.db.execute("UPDATE steering SET state=?,detail=? WHERE inbound_id=? AND state!='accepted'",
                                     (status, detail, inbound_id)).rowcount
            if changed and status in {'queued', 'unknown'}:
                self.db.execute("UPDATE turn SET executor=NULL,model=NULL,effort=NULL WHERE inbound_id=? AND status='queued'", (inbound_id,))

    def recover_steering(self):
        with self.db:
            self.db.execute("UPDATE steering SET state='unknown',detail='Service restarted before acknowledgement' WHERE state='sending'")
            self.db.execute("UPDATE turn SET executor=NULL,model=NULL,effort=NULL WHERE status='queued' AND inbound_id IN "
                            "(SELECT inbound_id FROM steering WHERE state='unknown')")

    def input_text(self, inbound_id):
        row = self.inbound(inbound_id)
        turn = self.turn_for(inbound_id)
        source = row['prepared_text'] if row['prepared_text'] is not None else row['text']
        text = source.split(maxsplit=1)[1] if turn['force_queue'] else source
        delivery = self.steering(inbound_id)
        if delivery and delivery['state'] == 'unknown':
            text = ("Eivoh could not confirm delivery of this clarification during the previous run. "
                    "It may already be in this session or have been acted on. Check existing context and work; "
                    "do not repeat completed actions. Ensure this user request is handled:\n\n" + text)
        return text

    def accepted_inputs(self, turn_id):
        return self.db.execute("SELECT inbound.id,COALESCE(inbound.prepared_text,inbound.text) AS text FROM inbound JOIN steering ON steering.inbound_id=inbound.id "
                               "WHERE parent_turn_id=? AND steering.state='accepted' ORDER BY inbound.id", (turn_id,)).fetchall()

    def recovery_inputs(self, turn_id):
        rows = self.accepted_inputs(turn_id)
        if not rows:
            return ''
        return ("\n\nThe following additional user inputs were accepted by the executor during the interrupted run. "
                "They are already part of this session; retain their instructions without repeating completed actions:\n\n" +
                '\n\n'.join(row['text'] for row in rows))

    def claim(self, inbound_id, max_recoveries):
        with self.db:
            turn = self.turn_for(inbound_id)
            recovering = turn["status"] == "running"
            if recovering and turn["recovery_count"] >= max_recoveries:
                self.finish(turn["id"], "failed", "Restart recovery limit reached")
                return None, True
            # Binding is immutable. NULL executor denotes an unclaimed/legacy turn;
            # NULL model/effort with a recorded executor mean executor/model defaults.
            if turn['executor'] is None:
                task = self.task(turn['conversation_id'], turn['task_id'])
                self.db.execute("UPDATE turn SET executor=?,model=?,effort=? WHERE id=?",
                                (task['executor'], task['model'], task['effort'], turn['id']))
            if recovering:
                self.db.execute("UPDATE turn SET recovery_count=recovery_count+1 WHERE id=?", (turn["id"],))
            self.db.execute("UPDATE turn SET status='running' WHERE id=?", (turn["id"],))
            self.db.execute("UPDATE inbound SET state='running' WHERE id=?", (inbound_id,))
        claimed = dict(self.turn_for(inbound_id))
        session = self.session(claimed['task_id'], claimed['executor'])
        claimed['resume_id'] = session['session_id'] if session else None
        claimed['workdir'] = self.db.execute("SELECT workdir FROM task WHERE id=?", (claimed['task_id'],)).fetchone()[0]
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
                if event.kind == 'input_accepted' and not event.data.get('initial'):
                    accepted = self.db.execute("UPDATE steering SET state='accepted',detail='' WHERE parent_turn_id=? AND input_id=? "
                                               "AND state IN ('sending','unknown')", (turn_id, event.data['input_id'])).rowcount
                    if accepted:
                        # A timeout may have cleared launch settings for fallback
                        # before a buffered acknowledgement reaches the journal.
                        # Restore the actual accepting run, never current /model.
                        self.db.execute("UPDATE turn SET executor=(SELECT executor FROM turn WHERE id=?),"
                            "model=(SELECT model FROM turn WHERE id=?),effort=(SELECT effort FROM turn WHERE id=?) WHERE inbound_id="
                            "(SELECT inbound_id FROM steering WHERE input_id=?) AND status IN ('queued','cancelled')",
                            (turn_id, turn_id, turn_id, event.data['input_id']))
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
            # Legacy adapters report a session only after receiving their stdin
            # prompt. App-server can open a thread while turn/start still fails.
            # Fold attempts in order: an older acceptance cannot acknowledge a
            # new attempt, and ordinary steering acknowledgements do not count.
            initial_accepted = True
            for row in self.db.execute("SELECT events FROM raw_event WHERE turn_id=? AND "
                "(kinds LIKE '%\"session_started\"%' OR kinds LIKE '%\"input_accepted\"%') ORDER BY sequence", (turn['id'],)):
                for event in json.loads(row[0]):
                    if event['kind'] == 'session_started':
                        initial_accepted = event['data'].get('input_accepted', True)
                    elif event['kind'] == 'input_accepted' and event['data'].get('initial'):
                        initial_accepted = True
            if not initial_accepted:
                return
            watermark = max([turn['inbound_id'], *(row['id'] for row in self.accepted_inputs(turn['id']))])
            for row in self.db.execute("SELECT payload FROM raw_event WHERE turn_id=? AND stream='meta'", (turn['id'],)):
                payload = json.loads(row[0])
                if isinstance(payload, dict) and payload.get('type') == 'turn_prompt':
                    watermark = max(watermark, payload.get('context_through', 0))
                    break
            blocked = self.db.execute("SELECT MIN(inbound_id) FROM turn WHERE task_id=? AND inbound_id<=? "
                "AND id!=? AND status IN ('queued','running') AND NOT EXISTS "
                "(SELECT 1 FROM steering WHERE inbound_id=turn.inbound_id AND state='accepted')",
                (turn['task_id'], watermark, turn['id'])).fetchone()[0]
            if blocked is not None:
                watermark = min(watermark, blocked - 1)
            self.db.execute("UPDATE task_session SET synced_inbound_id=MAX(synced_inbound_id,?) WHERE task_id=? AND executor=?",
                            (watermark, turn['task_id'], turn['executor']))

    def finish(self, turn_id, status, error=None):
        with self.db:
            turn = self.db.execute("SELECT * FROM turn WHERE id=?", (turn_id,)).fetchone()
            changed = self.db.execute("UPDATE turn SET status=?,error=? WHERE id=? AND status IN ('queued','running')", (status, error, turn_id)).rowcount
            if changed:
                children = self.db.execute("SELECT turn.id FROM turn JOIN steering ON steering.inbound_id=turn.inbound_id "
                                          "WHERE parent_turn_id=? AND steering.state='accepted'", (turn_id,)).fetchall()
                for child in children:
                    self.db.execute("UPDATE turn SET status=?,session_id=?,error=? WHERE id=? AND status='queued'",
                                    (status, turn['session_id'], error, child['id']))
                    self.db.execute("UPDATE inbound SET state=? WHERE id=(SELECT inbound_id FROM turn WHERE id=?) AND state!='cancelled'",
                                    ('cancelled' if status == 'cancelled' else 'done', child['id']))
                self._sync(turn)
                self.db.execute("UPDATE task SET last_active=? WHERE id=?", (now(), turn['task_id']))
            self.db.execute("UPDATE inbound SET state=? WHERE id=(SELECT inbound_id FROM turn WHERE id=?) AND state != 'cancelled'",
                            ("cancelled" if status == "cancelled" else "done", turn_id))

    def _create_task(self, conversation_id, executor=None, model=None, title=None, effort=None):
        task_id = self.db.execute("INSERT INTO task(conversation_id,executor,model,title,last_active,title_source,effort,workdir) VALUES (?,?,?,?,?,?,?,?)",
                                  (conversation_id, executor or self.default_executor, model, title, now(),
                                   'manual' if title is not None else 'fallback', effort, self.task_workdir)).lastrowid
        self.db.execute("UPDATE conversation SET active_task_id=? WHERE id=?", (task_id, conversation_id))
        return task_id

    def new_task(self, inbound_id, executor=None, title=None):
        with self.db:
            row = self.inbound(inbound_id)
            active = self.active_task(row['conversation_id'])
            inherit = active is not None and (executor is None or executor == active['executor'])
            task_id = self._create_task(row['conversation_id'],
                active['executor'] if inherit else executor, active['model'] if inherit else None,
                title, active['effort'] if inherit else None)
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
                output = self.output(turn['id'])
                return turns[0]['text'], output['text'] if output is not None else result[0]
        return (turns[0]['text'] if turns else None), None

    def remember_outbound(self, channel, chat_id, message_ids, task_id, turn_id=None):
        """Map bot messages to their task, and answers to their turn, for replies."""
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO outbound VALUES (?,?,?,?,?)",
                                [(channel, chat_id, mid, task_id, turn_id) for mid in message_ids])

    def output(self, turn_id):
        return self.db.execute("SELECT * FROM turn_output WHERE turn_id=?", (turn_id,)).fetchone()

    def save_output(self, turn_id, text, artifacts):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO turn_output(turn_id,text,artifacts) VALUES (?,?,?)",
                            (turn_id, text, json.dumps(artifacts)))
        return self.output(turn_id)

    def output_delivered(self, turn_id):
        with self.db:
            self.db.execute("UPDATE turn_output SET delivered=1 WHERE turn_id=?", (turn_id,))

    def pending_outputs(self):
        return self.db.execute("SELECT inbound.*,turn.id AS output_turn,turn.task_id FROM turn_output JOIN turn ON turn.id=turn_id "
            "JOIN inbound ON inbound.id=turn.inbound_id WHERE delivered=0 AND turn_output.cancelled=0 AND turn.status IN ('completed','failed') ORDER BY turn_id").fetchall()

    def prepare_delivery(self, channel, chat, logical, part, payload, task_id=None, turn_id=None, *, commit=True):
        encoded = json.dumps(payload, sort_keys=True)
        with self.db if commit else nullcontext():
            self.db.execute("INSERT OR IGNORE INTO delivery(channel,chat_id,logical_id,part,payload,task_id,turn_id) VALUES (?,?,?,?,?,?,?)",
                            (channel, chat, logical, part, encoded, task_id, turn_id))
            row = self.db.execute("SELECT * FROM delivery WHERE channel=? AND chat_id=? AND logical_id=? AND part=?",
                                  (channel, chat, logical, part)).fetchone()
            if row['payload'] != encoded:
                # A lost initial send never becomes a new send just because the
                # streamed text changed. A known id can be safely edited instead.
                state = row['state'] if row['state'] == 'sending' or row['state'] == 'unknown' and not row['message_id'] else 'pending'
                deleted = json.loads(row['payload']).get('kind') == 'delete' and row['state'] == 'sent'
                self.db.execute("UPDATE delivery SET payload=?,state=?,detail='',revision=revision+1,message_id=CASE WHEN ? THEN NULL ELSE message_id END WHERE id=?",
                                (encoded, state, deleted, row['id']))
        return self.delivery(row['id'])

    def delivery(self, delivery_id):
        return self.db.execute("SELECT * FROM delivery WHERE id=?", (delivery_id,)).fetchone()

    def delivery_parts(self, channel, chat, logical):
        return self.db.execute("SELECT * FROM delivery WHERE channel=? AND chat_id=? AND logical_id=? ORDER BY id",
                               (channel, chat, logical)).fetchall()

    def delivery_displaced(self, channel, chat, logical):
        family = [logical]
        preview = False
        if logical.startswith('turn:') and logical.split(':')[1].isdecimal():
            progress = ':'.join(logical.split(':')[:2])
            if logical in {progress, progress + ':final'}:
                family = [progress, progress + ':final']
                preview = logical == progress
        placeholders = ','.join('?' for _ in family)
        # +0 prevents SQLite's MAX shortcut from walking the chat-wide numeric
        # index; the threshold must use the small logical-id subset instead.
        return bool(self.db.execute(f"""SELECT 1 FROM (
                SELECT message_id FROM inbound WHERE channel=? AND chat_id=?
                UNION ALL
                SELECT message_id FROM delivery WHERE channel=? AND chat_id=?
                    AND logical_id NOT IN ({placeholders})
                    AND NOT (? AND EXISTS (SELECT 1 FROM turn WHERE turn.id=delivery.turn_id
                        AND turn.status='running' AND delivery.logical_id='turn:' || turn.id
                        AND json_extract(delivery.payload,'$.kind') IN ('text','delete')))
            ) WHERE message_id!='' AND message_id NOT GLOB '*[^0-9]*'
                AND CAST(message_id AS INTEGER) > (
                    SELECT MAX(CAST(message_id AS INTEGER) + 0) FROM delivery
                    WHERE channel=? AND chat_id=? AND logical_id=?
                        AND part!='' AND part NOT GLOB '*[^0-9]*'
                        AND json_extract(payload,'$.kind')='text'
                        AND message_id!='' AND message_id NOT GLOB '*[^0-9]*') LIMIT 1""",
            (channel, chat, channel, chat, *family, preview, channel, chat, logical)).fetchone())

    def move_delivery(self, channel, chat, logical):
        """Retire known text ids atomically; unknown fresh sends keep their evidence."""
        with self.db:
            rows = [row for row in self.delivery_parts(channel, chat, logical)
                    if row['part'].isdecimal() and not row['cancelled']]
            if not any(row['message_id'] is not None for row in rows):
                return False
            if not any(row['state'] == 'pending' or row['state'] == 'unknown' and row['message_id'] is not None for row in rows):
                return False
            # A refused delete cannot be retried as a fresh copy on the next
            # update, or when the final adopts the retained preview id.
            for row in rows:
                if row['message_id'] is not None and self.db.execute("SELECT 1 FROM delivery WHERE channel=? AND chat_id=? "
                        "AND logical_id IN (?,?) AND part=? AND json_extract(payload,'$.retained')=?",
                        (channel, chat, logical, logical.removesuffix(':final'), f"delete:{row['message_id']}", row['message_id'])).fetchone():
                    return False
            if not self.delivery_displaced(channel, chat, logical):
                return False
            for row in rows:
                if row['message_id'] is None:
                    continue
                self.retire_delivery(row['id'], commit=False)
            return True

    def retire_delivery(self, delivery_id, *, commit=True):
        """Move and shrink share physical cleanup; keep the numeric identity."""
        with self.db if commit else nullcontext():
            row = self.delivery(delivery_id)
            if row['cancelled'] or row['message_id'] is None:
                return row
            obsolete = json.loads(row['payload'])['kind'] == 'delete'
            cleanup = self.prepare_delivery(row['channel'], row['chat_id'], row['logical_id'], f"delete:{row['message_id']}",
                {'kind': 'delete', 'part': row['part']}, row['task_id'], row['turn_id'], commit=False)
            self.db.execute("UPDATE delivery SET message_id=?,state=? WHERE id=?",
                (row['message_id'], 'failed' if obsolete and row['state'] == 'failed' else cleanup['state'], cleanup['id']))
            self.db.execute("UPDATE delivery SET message_id=NULL,state=?,detail='',revision=revision+1 WHERE id=?",
                            ('sent' if obsolete else 'pending', row['id']))
            return self.delivery(cleanup['id'])

    def retain_delivery(self, delivery_id):
        """Resolve refused cleanup without sending another copy of its text."""
        with self.db:
            row = self.delivery(delivery_id)
            if row['cancelled'] or row['state'] != 'failed':
                return row
            payload = json.loads(row['payload'])
            target = self.db.execute('SELECT * FROM delivery WHERE channel=? AND chat_id=? AND logical_id=? AND part=?',
                (row['channel'], row['chat_id'], row['logical_id'], payload.get('part'))).fetchone()
            if (target is not None and not target['cancelled'] and target['state'] == 'pending'
                    and target['message_id'] is None and json.loads(target['payload'])['kind'] == 'text'):
                self.db.execute("UPDATE delivery SET message_id=?,revision=revision+1 WHERE id=?", (row['message_id'], target['id']))
                payload['retained'] = row['message_id']
                self.db.execute("UPDATE delivery SET payload=?,state='sent',message_id=NULL,detail='Delete refused; retained for editing' WHERE id=?",
                                (json.dumps(payload, sort_keys=True), row['id']))
            else:
                # There is no desired text at this index, or another unknown
                # send may already carry it. Retire only the known old content.
                payload = {'kind': 'text', 'text': '[Earlier preview superseded.]', 'retained': row['message_id']}
                self.db.execute("UPDATE delivery SET payload=?,state='pending',revision=revision+1,detail='Delete refused; retiring old content' WHERE id=?",
                                (json.dumps(payload, sort_keys=True), row['id']))
            return self.delivery(delivery_id)

    def begin_delivery(self, delivery_id, operation, payload, revision=None):
        with self.db:
            revision = self.delivery(delivery_id)['revision'] if revision is None else revision
            attempt = self.db.execute("INSERT INTO delivery_attempt(delivery_id,operation,payload,revision) VALUES (?,?,?,?)",
                                      (delivery_id, operation, json.dumps(payload, sort_keys=True), revision)).lastrowid
            # The attempted revision can be older than the current desired
            # payload (e.g. a flood wait). The actual write is still in flight.
            self.db.execute("UPDATE delivery SET state='sending' WHERE id=?", (delivery_id,))
        return attempt

    def delivery_outcome(self, delivery_id, attempt, state, message_id=None, detail=''):
        with self.db:
            self.db.execute("UPDATE delivery_attempt SET state=?,message_id=?,detail=? WHERE id=?",
                            (state, message_id, detail, attempt))
            current = self.delivery(delivery_id)
            attempted = self.db.execute("SELECT revision,operation FROM delivery_attempt WHERE id=?", (attempt,)).fetchone()
            deleted = attempted['operation'] == 'delete' and state == 'sent'
            if current['revision'] != attempted['revision'] and state != 'unknown':
                state = 'pending'  # Ack of progress cannot acknowledge a newer final answer.
            self.db.execute("UPDATE delivery SET state=?,message_id=COALESCE(?,message_id),detail=? WHERE id=?",
                            (state, message_id, detail, delivery_id))
            if deleted:
                self.db.execute("UPDATE delivery SET message_id=NULL WHERE id=?", (delivery_id,))
            row = self.delivery(delivery_id)
            if message_id is not None and row['task_id'] is not None:
                self.db.execute("INSERT OR REPLACE INTO outbound VALUES (?,?,?,?,?)",
                                (row['channel'], row['chat_id'], message_id, row['task_id'], row['turn_id']))

    def recover_deliveries(self):
        with self.db:
            self.db.execute("UPDATE delivery SET state='unknown',detail='Interrupted send without acknowledgement' "
                            "WHERE message_id IS NULL AND EXISTS (SELECT 1 FROM delivery_attempt WHERE delivery_id=delivery.id "
                            "AND operation='send' AND state IN ('sending','unknown'))")
            self.db.execute("UPDATE delivery_attempt SET state='unknown',detail='Interrupted before acknowledgement' WHERE state='sending'")
            self.db.execute("UPDATE delivery SET state=CASE WHEN message_id IS NULL THEN 'unknown' ELSE 'pending' END,"
                            "detail='Interrupted before acknowledgement' WHERE state='sending'")

    def switch_task(self, inbound_id, task_id):
        with self.db:
            row = self.inbound(inbound_id)
            self.db.execute("UPDATE task SET status='active',last_active=? WHERE id=? AND conversation_id=?",
                            (now(), task_id, row['conversation_id']))
            self.db.execute("UPDATE conversation SET active_task_id=? WHERE id=?", (task_id, row['conversation_id']))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))

    def title_task(self, inbound_id, task_id, title):
        with self.db:
            self.db.execute("UPDATE task SET title=?,title_source='manual' WHERE id=?", (title, task_id))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))

    def title_candidates(self):
        return self.db.execute("SELECT id FROM task WHERE title_source='fallback' AND title_attempted=0 "
                               "AND status='active' AND EXISTS (SELECT 1 FROM turn "
                               "WHERE task_id=task.id AND status='completed')").fetchall()

    def claim_title(self, task_id, models):
        """One durable attempt, using the first successful turn's executor/result."""
        with self.db:
            eligible = self.db.execute("SELECT 1 FROM task WHERE id=? AND title_source='fallback' "
                                       "AND title_attempted=0 AND status='active'", (task_id,)).fetchone()
            if not eligible or not models:
                return None
            for turn in self.db.execute("SELECT * FROM turn WHERE task_id=? AND status='completed' ORDER BY inbound_id",
                                        (task_id,)):
                result = self.stored_result(turn['id'])
                if result is not None and not result[1] and result[0].strip():
                    break
            else:
                return None
            if turn['executor'] not in models:
                return None
            changed = self.db.execute("UPDATE task SET title_attempted=1 WHERE id=? AND title_source='fallback' "
                                      "AND title_attempted=0 AND status='active'", (task_id,)).rowcount
            if not changed:
                return None
            first = self.db.execute("SELECT inbound.id,inbound.text FROM inbound JOIN turn ON turn.inbound_id=inbound.id "
                                    "WHERE turn.task_id=? ORDER BY inbound.id LIMIT 1", (task_id,)).fetchone()
            request = dict(task_id=task_id, turn_id=turn['id'], executor=turn['executor'],
                           model=models[turn['executor']], first_inbound_id=first['id'])
            self.title_event(request, 'started')
        return {**request, 'user': first['text'], 'answer': result[0]}

    def title_event(self, request, outcome, title=None):
        """Metadata only: never replay title output as conversational events."""
        payload = {key: request[key] for key in ('task_id', 'turn_id', 'first_inbound_id')}
        payload.update(type='task_title', outcome=outcome, title_executor=request['executor'],
                       title_model=request['model'])
        if 'title_usage' in request:
            payload['title_usage'] = request['title_usage']
        if title is not None:
            payload['title'] = title
        self.append(request['turn_id'], Record(None, payload, stream='meta'))

    def generated_title(self, request, title):
        with self.db:
            changed = self.db.execute("UPDATE task SET title=?,title_source='generated' WHERE id=? "
                                      "AND title_source='fallback' AND status='active'",
                                      (title, request['task_id'])).rowcount
            self.title_event(request, 'generated' if changed else 'superseded', title if changed else None)

    def set_model(self, inbound_id, executor, model, task_id=None, *, reset_effort=False, new_task=False):
        with self.db:
            row = self.inbound(inbound_id)
            task = self.task(row['conversation_id'], task_id) if task_id is not None else self.active_task(row['conversation_id'])
            if new_task:
                task = None
            switched = task is not None and task['executor'] != executor
            if task is None:
                task_id = self._create_task(row['conversation_id'], executor, model)
            else:
                task_id = task['id']
                self.db.execute("UPDATE task SET executor=?,model=?,effort=? WHERE id=?",
                                (executor, model, None if reset_effort else task['effort'], task_id))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))
        return self.task(row['conversation_id'], task_id), switched

    def set_effort(self, inbound_id, effort, task_id=None, *, new_task=False):
        with self.db:
            row = self.inbound(inbound_id)
            task = self.task(row['conversation_id'], task_id) if task_id is not None else self.active_task(row['conversation_id'])
            if new_task:
                task = None
            task_id = task['id'] if task else self._create_task(row['conversation_id'])
            self.db.execute("UPDATE task SET effort=? WHERE id=?", (effort, task_id))
            self.db.execute("UPDATE inbound SET state='done' WHERE id=?", (inbound_id,))
        return self.task(row['conversation_id'], task_id)

    def session(self, task_id, executor):
        return self.db.execute("SELECT * FROM task_session WHERE task_id=? AND executor=?", (task_id, executor)).fetchone()

    def missed_transcript(self, task_id, executor, before_inbound_id):
        session = self.session(task_id, executor)
        return self.transcript(task_id, before_inbound_id, after_inbound_id=session['synced_inbound_id'] if session else 0)

    def transcript(self, task_id, before_inbound_id, limit=30000, after_inbound_id=0):
        """Recent whole turns, or excerpts of the newest oversized turn."""
        limit = max(0, limit)
        blocks, size, truncated, newest = [], 0, False, None
        rows = self.db.execute("SELECT turn.id,turn.inbound_id,COALESCE(inbound.prepared_text,inbound.text) AS text FROM turn JOIN inbound ON inbound.id=turn.inbound_id "
                               "WHERE turn.task_id=? AND turn.inbound_id<? AND (turn.inbound_id>? OR EXISTS "
                               "(SELECT 1 FROM steering WHERE parent_turn_id=turn.id AND state='accepted' AND inbound_id>?)) "
                               "AND NOT EXISTS (SELECT 1 FROM steering WHERE inbound_id=turn.inbound_id AND state='accepted') AND (turn.status IN ('completed','failed') "
                               "OR (turn.status='cancelled' AND EXISTS (SELECT 1 FROM raw_event WHERE turn_id=turn.id))) "
                               "ORDER BY turn.inbound_id DESC",
                               (task_id, before_inbound_id, after_inbound_id, after_inbound_id))
        for row in rows:
            result = self.stored_result(row["id"])
            answer = result[0] if result is not None else "[No stored final answer.]"
            if output := self.output(row['id']):
                answer = output['text']
                artifacts = json.loads(output['artifacts'])
                if artifacts:
                    answer += ('\n\nGenerated files retained by Eivoh (not a delivery acknowledgement). '
                               'Read these snapshots as prior input; write revisions in the current turn output directory:\n' +
                               json.dumps([{key: artifact[key] for key in ('name', 'path', 'kind')} for artifact in artifacts], ensure_ascii=False))
            inputs = ([row['text']] if row['inbound_id'] > after_inbound_id else [])
            inputs += [item['text'] for item in self.accepted_inputs(row['id']) if item['id'] > after_inbound_id]
            user = '\n\nUser (during the same turn):\n'.join(inputs)
            if newest is None:
                newest = (user, answer)
            block = f"User:\n{user}\n\nAssistant:\n{answer}\n\n"
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
        # A previous run may have accepted input N+1 while delivery of N stayed
        # uncertain. When N later runs (possibly with a different executor), its
        # history must include that whole completed run, including N+1. Record
        # that progress so switching back never drops or replays that fragment.
        context_through = self.db.execute("SELECT COALESCE(MAX(steering.inbound_id),0) FROM steering JOIN turn parent "
            "ON parent.id=parent_turn_id WHERE parent.task_id=? AND parent.inbound_id<? "
            "AND parent.status IN ('completed','failed','cancelled') AND steering.state='accepted'",
            (turn['task_id'], turn['inbound_id'])).fetchone()[0]
        metadata = {'type': 'turn_prompt', 'prompt': prompt}
        if context_through > turn['inbound_id']:
            metadata['context_through'] = context_through
        self.append(turn['id'], Record(None, metadata, stream='meta'))
        return prompt

    def stored_result(self, turn_id):
        """Replay normalized events after a crash, without invoking the executor."""
        text, error, completed, deltas = "", None, False, False
        parts = {}  # Complete parts survive attempts; partial streaming text does not.
        for row in self.db.execute("SELECT events FROM raw_event WHERE turn_id=? ORDER BY sequence", (turn_id,)):
            for event in json.loads(row[0]):
                kind, data = event["kind"], event["data"]
                if kind == "session_started":
                    # Each resumed attempt has its own output and errors.
                    text, error, completed, deltas = "", None, False, False
                elif kind == 'answer_part':
                    parts[data['id']] = data['text']
                elif kind == "text_delta":
                    text = data['text'] if data.get('replace') else text + data['text']
                    deltas = True
                elif kind == "message":
                    if data.get('replace'):
                        text = data['text']
                    elif not deltas:
                        text += data["text"]
                    deltas = False
                elif kind == "error":
                    error = error or str(data.get("message", "Executor error"))
                elif kind == "turn_completed":
                    completed = True
                    included = set(data.get('answer_part_ids', ()))
                    previous = [part for part_id, part in parts.items() if part_id not in included]
                    text = '\n\n'.join(part for part in [*previous, data.get('text') or text] if part)
                    if data["status"] != "completed" and error is None:
                        error = "Executor reported failure"
        return (text, error) if completed else None

    def stop(self, inbound_id, task_id=None, close=False):
        """Cancel before killing; only earlier turns in the requested scope."""
        with self.db:
            row = self.inbound(inbound_id)
            # Cancellation also covers completed work awaiting network delivery.
            output_scope = "SELECT id FROM turn WHERE conversation_id=? AND inbound_id<?" + (" AND task_id=?" if task_id is not None else '')
            output_args = (row['conversation_id'], inbound_id) + ((task_id,) if task_id is not None else ())
            self.db.execute(f"UPDATE turn_output SET cancelled=1 WHERE turn_id IN ({output_scope}) AND delivered=0", output_args)
            self.db.execute("UPDATE delivery SET cancelled=1 WHERE channel=? AND chat_id=? AND state!='sent'" +
                            (" AND task_id=?" if task_id is not None else ''),
                            (row['channel'], row['chat_id']) + ((task_id,) if task_id is not None else ()))
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


COMMANDS = frozenset({'/new', '/tasks', '/task', '/model', '/effort', '/stop', '/close', '/title', '/q'})


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
