# Bakery Order Desk

A local order tracker for the MGOC50 Lecture 9 bakery exercise. Read seven days of Instagram messages, decide which orders to take, reserve oven time, book a helper, and reconcile pickups and revenue by week.

## Open the app

1. Download and extract the whole repository to a writable folder.
2. On a Windows computer with Python 3 installed, double-click **Open Bakery Workbench**. The desk opens in your default browser.
3. For instructions, select **User guide** in the app or open [README.html](README.html).

The app uses Python's standard library and SQLite. It normally opens at `http://127.0.0.1:8000/`. When you launch it from a second project folder, it can use a separate local port. On other systems with Python 3, the app entry point is `python3 app.py --open`.

## Course data

The included `bakery.db` contains the saved classroom session. Each copy of the folder keeps its own progress. For a fresh run, extract another copy, rename its `bakery.db` to `bakery-saved.db` before opening it, and the app will load the 13 starting orders from `existing_orders.csv`. Messages are read from the seven original HTML files as you advance through the week.

## Verification

Run `python -m unittest discover -s tests` from the project folder to run the regression suite on temporary databases. The user guide includes the design diagrams, expert reviews, and expected results for the course scenarios.
