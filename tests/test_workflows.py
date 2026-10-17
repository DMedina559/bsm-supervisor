"""Check persistent workflow exclusion and schema migration."""

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from bsm_supervisor.store import BusyError, Store


class WorkflowStoreTests(unittest.TestCase):
    def test_v1_database_migration_preserves_inventory_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "supervisor.sqlite3")) as db, db:
                db.executescript("""
                    CREATE TABLE installations (
                        id TEXT PRIMARY KEY, version TEXT, path TEXT,
                        registered_at TEXT DEFAULT ''
                    );
                    INSERT INTO installations VALUES ('old', '4.0.0b6', '/old', '');
                    CREATE TABLE operations (
                        id INTEGER PRIMARY KEY, action TEXT, state TEXT,
                        detail TEXT DEFAULT '', started_at TEXT DEFAULT '',
                        finished_at TEXT
                    );
                    INSERT INTO operations(action, state) VALUES ('old', 'succeeded');
                    PRAGMA user_version=1;
                """)
            store = Store(root)
            self.assertEqual(store.installations()[0]["id"], "old")
            self.assertEqual(store.operations()[0]["action"], "old")
            with store.workflow() as workflow_id:
                with store.mutation("child"):
                    pass
                store.finish_workflow(workflow_id, "succeeded", "done")
            self.assertEqual(store.operations()[0]["workflow_id"], workflow_id)

    def test_interrupted_workflow_blocks_other_connections_and_rebinds_for_recovery(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with store.workflow() as workflow_id:
                with store.mutation("prepared"):
                    pass
            reopened = Store(Path(directory))
            with self.assertRaises(BusyError):
                reopened.begin("unrelated")
            with reopened.workflow(workflow_id):
                with reopened.mutation("recovered"):
                    pass
                reopened.finish_workflow(workflow_id, "failed", "cancelled")
            with reopened.mutation("next"):
                pass

    def test_shared_store_does_not_treat_another_thread_as_reentrant(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            failures = []
            with store.workflow() as workflow_id:

                def mutate():
                    try:
                        with store.mutation("concurrent"):
                            pass
                    except BusyError:
                        failures.append("blocked")

                thread = threading.Thread(target=mutate)
                thread.start()
                thread.join(timeout=2)
                self.assertEqual(failures, ["blocked"])
                store.finish_workflow(workflow_id, "succeeded", "done")


if __name__ == "__main__":
    unittest.main()
