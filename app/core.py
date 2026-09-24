import re
import sqlite3
from contextlib import contextmanager


def mentioned(text, username):
    return bool(re.search(r'(?<![\w.@])@' + re.escape(username) + r'(?![\w.])', text, re.I))


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, since REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    account TEXT, thread TEXT, id TEXT, ts REAL, sender TEXT, body TEXT,
                    PRIMARY KEY(account, thread, id));
                CREATE TABLE IF NOT EXISTS jobs (
                    account TEXT, thread TEXT, id TEXT, status TEXT,
                    PRIMARY KEY(account, thread, id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def since(self, account, now):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO accounts VALUES (?,?)', (account, now))
            return db.execute('SELECT since FROM accounts WHERE id=?', (account,)).fetchone()[0]

    def start_session(self, account, now):
        # Reset only on activation, not on each poll: old pending requests stay silent
        # after restart while new requests can still retry within this session.
        with self.connect() as db:
            db.execute('INSERT INTO accounts VALUES (?,?) '
                       'ON CONFLICT(id) DO UPDATE SET since=excluded.since', (account, now))

    def add(self, account, thread, mid, ts, sender, body):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)',
                       (account, thread, mid, ts, sender, body))

    def done(self, account, thread, mid):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM jobs WHERE account=? AND thread=? AND id=?',
                              (account, thread, mid)).fetchone() is not None

    def claim(self, account, thread, mid):
        with self.connect() as db:
            return db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?)',
                              (account, thread, mid, 'sending')).rowcount == 1

    def finish(self, account, thread, mid, status):
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=? WHERE account=? AND thread=? AND id=?',
                       (status, account, thread, mid))

    def context(self, account, thread, until, count, chars):
        with self.connect() as db:
            rows = db.execute('SELECT sender,body FROM messages WHERE account=? AND thread=? '
                              'AND ts<=? ORDER BY ts DESC, id DESC LIMIT ?',
                              (account, thread, until, count)).fetchall()
        result = []
        for sender, body in rows:
            if chars <= 0:
                break
            body = body[:chars]
            result.append({'role': 'assistant' if sender == account else 'user',
                           'content': body if sender == account else f'[user {sender}] {body}'})
            chars -= len(body)
        return result[::-1]

    def prune(self, account, thread, keep):
        with self.connect() as db:
            db.execute('DELETE FROM messages WHERE account=? AND thread=? AND id NOT IN '
                       '(SELECT id FROM messages WHERE account=? AND thread=? ORDER BY ts DESC LIMIT ?)',
                       (account, thread, account, thread, keep))
