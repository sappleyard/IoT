"""A consistent copy of the Pi's database on the Mac, for audibility.py and dataset.py.

Lives in model/data/noise.db (git-ignored: it holds the home location). The Pi keeps only the
last 30 days of levels and positions, so older days are merged back from the R2 archive,
downloading each day's file once into model/data/archive/.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "noise.db"
ARCHIVE_CACHE = HERE / "data" / "archive"   # days downloaded from R2 (they never change)
HOST = "sebpi@sebpi"


def fetch():
    """Copy the Pi's database consistently (VACUUM INTO, so a half-written file is never copied)."""
    DB.parent.mkdir(exist_ok=True)
    script = ("import sqlite3, os\n"
              "os.path.exists('/tmp/noise-copy.db') and os.remove('/tmp/noise-copy.db')\n"
              "sqlite3.connect(os.path.expanduser('~/aircraft-noise-data/noise.db'))"
              ".execute(\"VACUUM INTO '/tmp/noise-copy.db'\")\n")
    subprocess.run(["ssh", HOST, "python3", "-"], input=script.encode(), check=True)
    subprocess.run(["scp", "-q", f"{HOST}:/tmp/noise-copy.db", str(DB)], check=True)
    subprocess.run(["ssh", HOST, "rm", "-f", "/tmp/noise-copy.db"], check=True)
    # The Pi keeps only recent levels and positions: add the older days back from the archive.
    sys.path.insert(0, str(HERE.parent / "collection" / "pi"))
    from aircraft_noise.archive import merge
    db = sqlite3.connect(DB)
    merge(db, ARCHIVE_CACHE)
    db.close()
