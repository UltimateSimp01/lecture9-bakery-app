"""Run the order desk's Python actions on a browser worker's private SQLite file."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import app


DATABASE = Path("/bakery/preview.sqlite3")


def dispatch(operation, payload_json):
    try:
        if operation == "seed":
            DATABASE.unlink(missing_ok=True)
        with closing(sqlite3.connect(DATABASE, isolation_level=None)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN" if operation == "state" else "BEGIN IMMEDIATE")
            if operation == "seed":
                app.init_db(db)
                value = app.state(db)
            elif operation == "state":
                value = app.state(db)
            elif operation == "apply":
                payload = json.loads(payload_json)
                if not isinstance(payload, dict) or type(payload.get("version")) is not int:
                    raise ValueError("Refresh the order desk and try the action again.")
                if app.state(db)["version"] != payload["version"]:
                    raise ValueError("The dashboard has changed. Review the latest orders before trying again.")
                message = app.apply_action(db, payload)
                value = {"state": app.state(db), "message": message}
            else:
                raise ValueError("Unknown order desk operation.")
        return json.dumps({"ok": True, "value": value}, ensure_ascii=False)
    except (ValueError, OSError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except sqlite3.Error:
        return json.dumps({"ok": False, "error": "The preview could not read or update the saved orders."})
