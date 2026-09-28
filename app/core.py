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
                CREATE TABLE IF NOT EXISTS images (
                    account TEXT, thread TEXT, id TEXT, ts REAL, sender TEXT,
                    url TEXT, jpeg BLOB, PRIMARY KEY(account, thread, id));
                CREATE TABLE IF NOT EXISTS model_cooldowns (
                    endpoint TEXT, model TEXT, until REAL NOT NULL,
                    PRIMARY KEY(endpoint, model));
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

    def add_image(self, account, thread, mid, ts, sender, url=None, jpeg=None):
        with self.connect() as db:
            db.execute('INSERT INTO images VALUES (?,?,?,?,?,?,?) '
                       'ON CONFLICT(account,thread,id) DO UPDATE SET '
                       'url=COALESCE(excluded.url,images.url), jpeg=COALESCE(excluded.jpeg,images.jpeg)',
                       (account, thread, mid, ts, sender, url, jpeg))

    def model_cooldown(self, endpoint, model):
        with self.connect() as db:
            row = db.execute('SELECT until FROM model_cooldowns WHERE endpoint=? AND model=?',
                             (endpoint, model)).fetchone()
        return row[0] if row else 0

    def set_model_cooldown(self, endpoint, model, until):
        with self.connect() as db:
            db.execute('INSERT INTO model_cooldowns VALUES (?,?,?) '
                       'ON CONFLICT(endpoint,model) DO UPDATE SET until=MAX(until,excluded.until)',
                       (endpoint, model, until))

    def image(self, account, thread, until, mid=None, sender=None):
        with self.connect() as db:
            row = db.execute('SELECT id,url,jpeg FROM images WHERE account=? AND thread=? AND ts<=? '
                             'AND (? IS NULL OR id=?) AND (? IS NULL OR sender=?) '
                             'ORDER BY ts DESC,id DESC LIMIT 1',
                             (account, thread, until, mid, mid, sender, sender)).fetchone()
        return dict(zip(('id', 'url', 'jpeg'), row)) if row else None

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
            db.execute('DELETE FROM images WHERE account=? AND thread=? AND id NOT IN '
                       '(SELECT id FROM messages WHERE account=? AND thread=? '
                       'ORDER BY ts DESC,id DESC LIMIT ?)', (account, thread, account, thread, keep))
            db.execute('DELETE FROM messages WHERE account=? AND thread=? AND id NOT IN '
                       '(SELECT id FROM messages WHERE account=? AND thread=? ORDER BY ts DESC LIMIT ?)',
                       (account, thread, account, thread, keep))
