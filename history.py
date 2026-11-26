"""Local measurement history. All times on disk are Unix UTC seconds."""

import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from statistics import median


class History:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS readings(
                device TEXT, ts REAL, bpm INTEGER, interval INTEGER, seconds REAL,
                PRIMARY KEY(device, ts));
            CREATE TABLE IF NOT EXISTS battery(
                device TEXT, ts REAL, level INTEGER, interval INTEGER, PRIMARY KEY(device, ts));
            CREATE TABLE IF NOT EXISTS requests(
                device TEXT, ts REAL, interval INTEGER, PRIMARY KEY(device, ts));
            CREATE TABLE IF NOT EXISTS band_frames(
                device TEXT, ts REAL, frame BLOB NOT NULL, PRIMARY KEY(device, ts));
            CREATE TABLE IF NOT EXISTS osc_holds(
                device TEXT, ts REAL, PRIMARY KEY(device, ts));
            CREATE TABLE IF NOT EXISTS link_events(
                device TEXT, ts REAL, event TEXT, detail TEXT, PRIMARY KEY(device,ts,event));
        """)

    def write(self, address, kind, value):
        with self.db:
            if kind == "reading":
                self.db.execute("INSERT OR IGNORE INTO readings VALUES(?,?,?,?,?)", (address, *value[:4]))
                if len(value) >= 5:
                    self.db.execute("INSERT OR IGNORE INTO band_frames VALUES(?,?,?)", (address, value[0], value[4]))
                if len(value) >= 6 and value[5] == "held":
                    self.db.execute("INSERT OR IGNORE INTO osc_holds VALUES(?,?)", (address,value[0]))
            elif kind == "request":
                self.db.execute("INSERT OR IGNORE INTO requests VALUES(?,?,?)", (address, *value))
            elif kind == "identity":
                _, _, level, interval, ts = value
                if level is not None:
                    self.db.execute("INSERT OR IGNORE INTO battery VALUES(?,?,?,?)", (address, ts, level, interval))
            elif kind == "link":
                self.db.execute("INSERT OR IGNORE INTO link_events VALUES(?,?,?,?)", (address,*value))

    def readings(self, device, start, end):
        return self.db.execute("SELECT ts,bpm,interval FROM readings WHERE device=? AND ts BETWEEN ? AND ? ORDER BY ts",
                               (device, start, end)).fetchall()

    def latest(self, device):
        return self.db.execute("SELECT ts,bpm,interval FROM readings WHERE device=? ORDER BY ts DESC LIMIT 1",
                               (device,)).fetchone()

    def recent_band_stamps(self, device, now):
        rows = self.db.execute("SELECT frame FROM band_frames WHERE device=? AND ts>=? ORDER BY ts",
                               (device, now-86400))
        return [(frame[8:10], int.from_bytes(frame[12:16], "big")) for frame, in rows]

    def pulse_check(self, device, start, end):
        # Compare only acquisitions contained inside the reference count.
        rows = self.db.execute("""SELECT bpm FROM readings WHERE device=? AND ts BETWEEN ? AND ?
                                  AND ts-COALESCE(seconds,0)>=? ORDER BY ts""", (device,start,end,start))
        values = [bpm for bpm, in rows]
        return (values, median(values) if values else None)

    def plot(self, device, start, end, battery=False):
        table, field = ("battery", "level") if battery else ("readings", "bpm")
        where = f"FROM {table} WHERE device=? AND ts BETWEEN ? AND ?"
        args = (device, start, end)
        summary = self.db.execute(f"SELECT COUNT(*), AVG({field}), MIN({field}), MAX({field}) {where}", args).fetchone()
        if summary[0] <= 1400:
            points = self.db.execute(f"SELECT ts,{field},interval {where} ORDER BY ts", args).fetchall()
        else:
            # SQLite returns the timestamp from the row with the single MIN or MAX.
            # Keep both extrema per bucket so a long window preserves narrow peaks.
            bucket = max(1, (end-start)/650)
            sql = f"SELECT ts,{{}}({field}),interval {where} GROUP BY CAST((ts-?)/? AS INTEGER)"
            params = (*args, start, bucket)
            points = sorted(set(self.db.execute(sql.format("MIN"), params)) |
                            set(self.db.execute(sql.format("MAX"), params)))
        return points, summary

    def batteries(self, device, start, end):
        return self.db.execute("SELECT ts,level,interval FROM battery WHERE device=? AND ts BETWEEN ? AND ? ORDER BY ts",
                               (device, start, end)).fetchall()

    def stats(self, device, now):
        since = now - 86400
        count = self.db.execute("SELECT COUNT(*) FROM requests WHERE device=? AND ts>=?", (device,since)).fetchone()[0]
        samples, duration = self.db.execute("SELECT COUNT(*),AVG(seconds) FROM readings WHERE device=? AND ts>=?",
                                            (device,since)).fetchone()
        return count, samples, duration

    def export(self, device, file):
        query = """SELECT r.ts,'heart_rate',r.bpm,'BPM',r.interval,f.frame,h.ts FROM readings r
                   LEFT JOIN band_frames f ON f.device=r.device AND f.ts=r.ts
                   LEFT JOIN osc_holds h ON h.device=r.device AND h.ts=r.ts WHERE r.device=?
                   UNION ALL SELECT ts,'battery',level,'percent',interval,NULL,NULL FROM battery WHERE device=?
                   UNION ALL SELECT ts,'measurement_request',1,'request',interval,NULL,NULL FROM requests WHERE device=?
                   UNION ALL SELECT ts,'bluetooth_'||event,detail,'event',NULL,NULL,NULL FROM link_events WHERE device=?
                   ORDER BY 1"""
        with open(file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(("local_time", "metric", "value", "unit", "sampling_interval_seconds", "band_frame_hex","osc_held"))
            for ts, metric, value, unit, interval, frame, held in self.db.execute(query, (device,)*4):
                writer.writerow((datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds"),
                                 metric, value, unit, interval, frame.hex() if frame else "",
                                 ("yes" if held is not None else "no") if frame else ""))


def discharge(points):
    """Estimate wall-clock runtime under the recent use mix, after the last rise or long gap."""
    if not points:
        return None
    start = 0
    for i in range(1, len(points)):
        if points[i][1] > points[i-1][1] or points[i][0]-points[i-1][0] > 2*3600:
            start = i
    segment = points[start:]
    first, last = segment[0], segment[-1]
    hours = (last[0]-first[0])/3600
    drop = first[1]-last[1]
    if hours < 6 or drop < 10 or len({p[1] for p in segment}) < 3:
        return None
    rate = drop/hours
    return rate, last[1]/rate, 100/rate


def profile_estimates(points):
    """Use qualifying observations taken entirely at the same interval."""
    from itertools import groupby
    estimates={}
    for interval, group in groupby(points, key=lambda p:p[2]):
        result=discharge(list(group))
        if result:
            estimates[interval]=result[2]
    return estimates
