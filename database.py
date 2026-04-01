import os
import sqlite3

PROJECT_FOLDER = os.path.dirname(__file__)
DATABASE_PATH = os.path.join(PROJECT_FOLDER, 'habits.db')

TRACKER_TYPES = ('subject', 'habit', 'goal')
CHECKIN_STATUSES = ('clean', 'slipped', 'unsure')
MIN_GOAL_DAYS = 1
MAX_GOAL_DAYS = 254
DEFAULT_GOAL_DAYS = 21

USERS_TABLE_SQL = '''
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
'''

TRACKER_TYPE_SQL = ', '.join(f"'{tracker_type}'" for tracker_type in TRACKER_TYPES)
CHECKIN_STATUS_SQL = ', '.join(f"'{status}'" for status in CHECKIN_STATUSES)

TRACKERS_TABLE_SQL = f'''
CREATE TABLE IF NOT EXISTS trackers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    tracker_name  TEXT NOT NULL,
    tracker_type  TEXT NOT NULL CHECK(tracker_type IN ({TRACKER_TYPE_SQL})),
    quote_text    TEXT NOT NULL DEFAULT '',
    goal_days     INTEGER NOT NULL CHECK(goal_days BETWEEN {MIN_GOAL_DAYS} AND {MAX_GOAL_DAYS}),
    start_date    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
'''

CHECKIN_LOGS_TABLE_SQL = f'''
CREATE TABLE IF NOT EXISTS checkin_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id INTEGER NOT NULL,
    date       TEXT NOT NULL,
    status     TEXT NOT NULL CHECK(status IN ({CHECKIN_STATUS_SQL})),
    timestamp  INTEGER NOT NULL,
    FOREIGN KEY (tracker_id) REFERENCES trackers(id) ON DELETE CASCADE,
    UNIQUE(tracker_id, date)
);
'''


def open_database():
    """Open the SQLite database with dictionary-style rows."""
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def init_db():
    """Create the database tables and upgrade older tracker schemas."""
    connection = open_database()
    try:
        connection.execute(USERS_TABLE_SQL)
        connection.execute(TRACKERS_TABLE_SQL)
        connection.execute(CHECKIN_LOGS_TABLE_SQL)
        migrate_trackers_table(connection)
        connection.commit()
    finally:
        connection.close()


def list_table_columns(connection, table_name):
    """Return only the column names for one table."""
    rows = connection.execute(f'PRAGMA table_info({table_name})').fetchall()
    return [row['name'] for row in rows]


def migrate_trackers_table(connection):
    """
    Older versions stored tracker quote data in `note` and carried an unused
    `target_text` column. We rebuild the table once so the database matches
    the current beginner-friendly model.
    """
    column_names = list_table_columns(connection, 'trackers')
    if not column_names:
        return

    schema_is_current = (
        'quote_text' in column_names
        and 'target_text' not in column_names
        and 'note' not in column_names
    )
    if schema_is_current:
        return

    quote_source = 'quote_text' if 'quote_text' in column_names else 'note'
    created_at_source = 'created_at' if 'created_at' in column_names else 'CURRENT_TIMESTAMP'
    updated_at_source = 'updated_at' if 'updated_at' in column_names else 'CURRENT_TIMESTAMP'
    start_date_source = 'start_date' if 'start_date' in column_names else 'CURRENT_DATE'

    connection.execute('PRAGMA foreign_keys = OFF')
    connection.executescript(
        f'''
        CREATE TABLE trackers_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            tracker_name  TEXT NOT NULL,
            tracker_type  TEXT NOT NULL CHECK(tracker_type IN ({TRACKER_TYPE_SQL})),
            quote_text    TEXT NOT NULL DEFAULT '',
            goal_days     INTEGER NOT NULL CHECK(goal_days BETWEEN {MIN_GOAL_DAYS} AND {MAX_GOAL_DAYS}),
            start_date    TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        INSERT INTO trackers_new (
            id,
            user_id,
            tracker_name,
            tracker_type,
            quote_text,
            goal_days,
            start_date,
            created_at,
            updated_at
        )
        SELECT
            id,
            user_id,
            tracker_name,
            tracker_type,
            COALESCE({quote_source}, ''),
            COALESCE(goal_days, {DEFAULT_GOAL_DAYS}),
            {start_date_source},
            {created_at_source},
            {updated_at_source}
        FROM trackers;

        DROP TABLE trackers;
        ALTER TABLE trackers_new RENAME TO trackers;
        '''
    )
    connection.execute('PRAGMA foreign_keys = ON')


def create_user(username, email, password_hash):
    connection = open_database()
    try:
        cursor = connection.execute(
            '''
            INSERT INTO users (username, email, password_hash)
            VALUES (?, ?, ?)
            ''',
            (username, email, password_hash),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_user_by_email(email):
    connection = open_database()
    try:
        return connection.execute(
            'SELECT id, username, email, password_hash FROM users WHERE email = ?',
            (email,),
        ).fetchone()
    finally:
        connection.close()


def get_user_by_id(user_id):
    connection = open_database()
    try:
        return connection.execute(
            'SELECT id, username, email, password_hash FROM users WHERE id = ?',
            (user_id,),
        ).fetchone()
    finally:
        connection.close()


def email_exists(email):
    connection = open_database()
    try:
        return connection.execute(
            'SELECT 1 FROM users WHERE email = ?',
            (email,),
        ).fetchone() is not None
    finally:
        connection.close()


def username_exists(username):
    connection = open_database()
    try:
        return connection.execute(
            'SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)',
            (username,),
        ).fetchone() is not None
    finally:
        connection.close()


def create_tracker(user_id, tracker_name, tracker_type, quote_text, goal_days, start_date):
    """Save one tracker row for one user."""
    connection = open_database()
    try:
        cursor = connection.execute(
            '''
            INSERT INTO trackers (
                user_id, tracker_name, tracker_type, quote_text, goal_days, start_date
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (user_id, tracker_name, tracker_type, quote_text, goal_days, start_date),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_trackers(user_id):
    """Return every tracker for one user in the order they were added."""
    connection = open_database()
    try:
        rows = connection.execute(
            '''
            SELECT
                id,
                user_id,
                tracker_name AS name,
                tracker_type AS type,
                quote_text AS quote,
                goal_days,
                start_date
            FROM trackers
            WHERE user_id = ?
            ORDER BY id ASC
            ''',
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_tracker(tracker_id):
    """Return one tracker by id, or None if it does not exist."""
    connection = open_database()
    try:
        row = connection.execute(
            '''
            SELECT
                id,
                user_id,
                tracker_name AS name,
                tracker_type AS type,
                quote_text AS quote,
                goal_days,
                start_date
            FROM trackers
            WHERE id = ?
            ''',
            (tracker_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        connection.close()


def get_tracker_count(user_id):
    """Count how many trackers belong to one user."""
    connection = open_database()
    try:
        row = connection.execute(
            'SELECT COUNT(*) AS count FROM trackers WHERE user_id = ?',
            (user_id,),
        ).fetchone()
        return int(row['count'])
    finally:
        connection.close()


def update_tracker(tracker_id, tracker_name, tracker_type, quote_text, goal_days):
    """Update the editable tracker fields from the edit form."""
    connection = open_database()
    try:
        connection.execute(
            '''
            UPDATE trackers
            SET
                tracker_name = ?,
                tracker_type = ?,
                quote_text = ?,
                goal_days = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (tracker_name, tracker_type, quote_text, goal_days, tracker_id),
        )
        connection.commit()
    finally:
        connection.close()


def reset_tracker_progress(tracker_id, start_date):
    """
    When the goal changes in a way that would make old progress confusing,
    we clear the old check-ins and restart the tracker from today.
    """
    connection = open_database()
    try:
        connection.execute('DELETE FROM checkin_logs WHERE tracker_id = ?', (tracker_id,))
        connection.execute(
            '''
            UPDATE trackers
            SET start_date = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (start_date, tracker_id),
        )
        connection.commit()
    finally:
        connection.close()


def save_checkin(tracker_id, check_date, status, timestamp):
    """Insert or update one check-in result for one tracker day."""
    connection = open_database()
    try:
        connection.execute(
            '''
            INSERT INTO checkin_logs (tracker_id, date, status, timestamp)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tracker_id, date) DO UPDATE SET
                status = excluded.status,
                timestamp = excluded.timestamp
            ''',
            (tracker_id, check_date, status, timestamp),
        )
        connection.commit()
    finally:
        connection.close()


def get_checkin_logs(tracker_id):
    """Return the full check-in history for one tracker."""
    connection = open_database()
    try:
        rows = connection.execute(
            '''
            SELECT tracker_id, date, status, timestamp
            FROM checkin_logs
            WHERE tracker_id = ?
            ORDER BY date ASC
            ''',
            (tracker_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
