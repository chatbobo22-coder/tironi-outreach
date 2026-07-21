from pathlib import Path
import psycopg
from psycopg.rows import dict_row


class Database:
    def __init__(self, url: str):
        self.url = url

    def connect(self):
        return psycopg.connect(self.url, row_factory=dict_row)

    def migrate(self, sql_dir: Path):
        with self.connect() as conn:
            for path in sorted(sql_dir.glob("*.sql")):
                conn.execute(path.read_text(encoding="utf-8"))
            conn.commit()
