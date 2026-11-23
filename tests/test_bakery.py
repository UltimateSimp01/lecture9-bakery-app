"""Offline regression tests. Run with python -m unittest discover -s tests."""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import app


class BakeryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.inputs = Path(directory.name)
        for source in [PROJECT / "existing_orders.csv", *PROJECT.glob("InstagramOrders_*.html")]:
            shutil.copy2(source, self.inputs / source.name)
        source_patch = patch.object(app, "BASE_DIR", self.inputs)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)

    def start_run(self):
        with closing(sqlite3.connect(":memory:")) as seed:
            seed.row_factory = sqlite3.Row
            app.init_db(seed)
            seed.backup(self.db)

    def act(self, action, **fields):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return app.apply_action(self.db, {"action": action, **fields})

    def test_incomplete_seed_can_be_fixed_and_retried(self):
        seed_file = self.inputs / "existing_orders.csv"
        original = seed_file.read_bytes()
        header = "order,customer,item,pickup_day\n"
        cases = [
            (None, "is missing"),
            (header, "contains no orders"),
            ("order,item,pickup_day\n", "Expected one column"),
            (header + "#101,Kim,4 dozen cupcakes\n", "missing value"),
            (header + "invalid,Kim,4 dozen cupcakes,Mon 23 Nov\n", "Invalid order number"),
        ]
        for contents, error in cases:
            with self.subTest(error=error):
                if contents is None:
                    seed_file.unlink()
                else:
                    seed_file.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error):
                    app.init_db(self.db)
                self.assertEqual(self.db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        seed_file.write_bytes(original)
        app.init_db(self.db)
        before = app.state(self.db)
        self.assertEqual([order["id"] for order in before["orders"]], list(range(101, 114)))
        seed_file.unlink()
        app.init_db(self.db)
        self.assertEqual(app.state(self.db), before)

    def test_duplicate_seed_order_rolls_back(self):
        seed_file = self.inputs / "existing_orders.csv"
        original = seed_file.read_text(encoding="utf-8")
        duplicate = original.splitlines()[1]
        seed_file.write_text(original.rstrip() + "\n" + duplicate + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate order number"):
            app.init_db(self.db)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        seed_file.write_text(original, encoding="utf-8")
        app.init_db(self.db)
        self.assertEqual(len(app.state(self.db)["orders"]), 13)

    def test_incomplete_daily_messages_leave_the_day_available_for_retry(self):
        self.start_run()
        message_file = self.inputs / "InstagramOrders_Monday.html"
        original = message_file.read_text(encoding="utf-8")
        before = app.state(self.db)
        for damaged in (original.rsplit("</p>", 1)[0], original.replace("Order: 1 Cake", "")):
            with self.subTest(damage=damaged[-40:]):
                message_file.write_text(damaged, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Restore the original course file"):
                    self.act("fetch")
                self.assertEqual(app.state(self.db), before)
        message_file.write_text(original, encoding="utf-8")
        self.act("fetch")
        loaded = app.state(self.db)
        self.assertEqual([r["customer"] for r in loaded["requests"]], ["Jasmine", "Okafor", "Patel"])
        with self.assertRaisesRegex(ValueError, "already been loaded"):
            self.act("fetch")
        self.assertEqual(app.state(self.db), loaded)

    def finish_week(self):
        for _ in range(7):
            self.act("fetch")
            self.act("advance")
            for day in app.state(self.db)["days"]:
                self.assertLessEqual(day["used"], day["capacity"])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        return app.state(self.db)

    def test_course_week_and_week_two_pickups(self):
        self.start_run()
        sunday = self.finish_week()
        self.assertEqual(sunday["totals"], {"earned": 3000, "booked": 640, "helper_cost": 0})
        self.assertEqual(Counter((r["kind"], r["status"]) for r in sunday["requests"]), {
            ("new", "accepted"): 13, ("new", "rejected"): 6,
            ("change", "accepted"): 1, ("change", "rejected"): 2,
            ("cancel", "accepted"): 2, ("cancel", "rejected"): 1,
        })
        self.assertEqual(sunday["weeks"][0]["pickups"], 20)
        self.assertEqual(sunday["weeks"][1]["booked"], 640)
        self.act("advance")
        self.act("advance")
        final = app.state(self.db)
        self.assertTrue(final["complete"])
        self.assertEqual(final["totals"], {"earned": 3640, "booked": 0, "helper_cost": 0})
        self.assertEqual(final["weeks"][0], sunday["weeks"][0])
        self.assertEqual(final["weeks"][1]["pickups"], 4)

    def test_saturday_helper(self):
        self.start_run()
        self.act("helper", day="2026-11-28")
        with self.assertRaisesRegex(ValueError, "already booked"):
            self.act("helper", day="2026-11-28")
        sunday = self.finish_week()
        saturday = next(day for day in sunday["days"] if day["day"] == "2026-11-28")
        self.assertEqual((saturday["used"], saturday["capacity"], saturday["sales"]), (720, 720, 880))
        self.assertEqual(sunday["totals"], {"earned": 3320, "booked": 640, "helper_cost": 100})
        self.assertEqual(sunday["weeks"][0]["helper_cost"], 100)
        self.assertEqual(sunday["weeks"][1]["helper_cost"], 0)


if __name__ == "__main__":
    unittest.main()
