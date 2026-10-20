"""SQLite storage: an industry-neutral schema, connection settings and the sample-data loader.

Sites, items, lots, lanes, suppliers and countries describe any supply chain. Industry specifics
(vocabulary, handling rules, sample network) come from the domain packs in app/domains/.
In production this module is replaced by connectors to the ERP / WMS / TMS / POS systems.
"""
import json
import os
import sqlite3
from datetime import date, datetime, time, timedelta

from . import domains
from . import guardrails as G

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE countries(code TEXT PRIMARY KEY, name TEXT, customs_days INT DEFAULT 0, restricted INT DEFAULT 0);
CREATE TABLE sites(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT, country TEXT, caps TEXT DEFAULT '',
                   capacity INT, operational INT DEFAULT 1);
CREATE TABLE items(id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, tags TEXT DEFAULT '', seasonal INT,
                   impact INT, pack INT, price REAL, shelf_days INT, hs_code TEXT, duty_pct REAL DEFAULT 0);
CREATE TABLE lots(id INTEGER PRIMARY KEY, sku TEXT, site TEXT, lot_no TEXT, qty INT, expiry TEXT,
                  status TEXT DEFAULT 'available', note TEXT);
CREATE TABLE positions(sku TEXT, site TEXT, in_transit INT DEFAULT 0, forecast_14d INT, safety INT,
                       PRIMARY KEY(sku, site));
CREATE TABLE demand(id INTEGER PRIMARY KEY, sku TEXT, site TEXT, day TEXT, qty INT);
CREATE TABLE lanes(a TEXT, b TEXT, mode TEXT, hours INT, caps TEXT DEFAULT '', trip_cost REAL DEFAULT 0,
                   vehicle_units INT, PRIMARY KEY(a, b));
CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT, country TEXT, lead_days INT, price_mult REAL,
                       freight_pct REAL, compliance INT, is_primary INT, caps TEXT, categories TEXT, incoterm TEXT,
                       available INT DEFAULT 1);
CREATE TABLE permits(id INTEGER PRIMARY KEY, ref TEXT, category TEXT, country TEXT, valid_until TEXT);
CREATE TABLE substitutes(sku TEXT, alt TEXT, note TEXT, PRIMARY KEY(sku, alt));
CREATE TABLE calendar(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, start TEXT, end TEXT, factor REAL,
                    scope TEXT DEFAULT '', sites TEXT DEFAULT '');
CREATE TABLE actions(id INTEGER PRIMARY KEY, kind TEXT, sku TEXT, from_site TEXT, to_site TEXT, supplier TEXT,
                     qty INT, value REAL, status TEXT, approved_by TEXT, role TEXT, created_at TEXT, eta TEXT,
                     received_at TEXT, received_qty INT, detail TEXT);
CREATE TABLE loads(id INTEGER PRIMARY KEY, a TEXT, b TEXT, day TEXT, capacity INT, used INT, cost REAL);
CREATE TABLE outcomes(id INTEGER PRIMARY KEY, day TEXT, kind TEXT, sku TEXT, site TEXT, units INT, value REAL,
                      ref TEXT, sample INT DEFAULT 0);
CREATE TABLE recalls(lot_no TEXT PRIMARY KEY, reason TEXT, created_at TEXT, by TEXT);
CREATE TABLE holds(sku TEXT, site TEXT, by TEXT, created_at TEXT, PRIMARY KEY(sku, site));
CREATE TABLE scenarios(key TEXT PRIMARY KEY, label TEXT, active INT DEFAULT 0, params TEXT DEFAULT '{}');
CREATE TABLE policy(key TEXT PRIMARY KEY, value REAL);
CREATE TABLE events(id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), msg TEXT);
CREATE INDEX lots_by_item ON lots(sku, site, status, expiry);
CREATE INDEX lots_by_no ON lots(lot_no);
CREATE INDEX demand_by_day ON demand(day, sku, site);
CREATE INDEX actions_by_status ON actions(status, kind);
CREATE INDEX outcomes_by_day ON outcomes(day, kind);
"""

# weekly demand shape with mean 1 and standard deviation 0.2, scaled to each item's volatility
PATTERN = (1.3, 0.8, 1.1, 0.7, 1.2, 0.9, 1.0)


def connect():
    conn = sqlite3.connect(os.environ.get("SANAR_DB", "sanar.db"), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def log(conn, msg):
    conn.execute("INSERT INTO events(msg) VALUES (?)", (msg,))


def current_domain(conn):
    try:
        r = conn.execute("SELECT value FROM meta WHERE key='domain'").fetchone()
        return r["value"] if r else None
    except sqlite3.OperationalError:
        return None


def reset(conn, domain=None):
    """Drop everything and load the sample network of `domain` (default: the current one)."""
    pack = domains.get(domain or current_domain(conn))
    try:
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            conn.execute(f"DROP TABLE IF EXISTS {r['name']}")
        conn.executescript(SCHEMA)
        load(conn, pack)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()  # never leave a half-loaded network holding the write lock
        raise


def history(forecast, cv):
    """28 days of demand (yesterday first) summing to twice the 14-day forecast, varying by `cv`."""
    raw = [forecast / 14 * (1 + (PATTERN[d % 7] - 1) * cv / 0.2) for d in range(28)]
    out = [int(x) for x in raw]
    for i in sorted(range(28), key=lambda i: raw[i] - out[i], reverse=True)[:2 * forecast - sum(out)]:
        out[i] += 1
    return out


def load(conn, pack):
    s, today = pack.sample, date.today()
    day = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    stamp = lambda n: datetime.combine(today + timedelta(days=n), time(9)).isoformat(timespec="seconds")  # noqa: E731
    conn.execute("INSERT INTO meta VALUES ('domain', ?)", (pack.name,))
    conn.executemany("INSERT INTO countries(code,name,customs_days) VALUES (?,?,?)", s["countries"])
    conn.executemany("INSERT INTO sites(id,name,kind,country,caps,capacity) VALUES (?,?,?,?,?,?)", s["sites"])
    conn.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)", s["items"])
    # lanes: a, b, mode, hours, capabilities[, cost per trip, units per vehicle]
    conn.executemany("INSERT INTO lanes VALUES (?,?,?,?,?,?,?)",
                     [(*sorted(r[:2]), *r[2:5], r[5] if len(r) > 5 else 0, r[6] if len(r) > 6 else None)
                      for r in s["lanes"]])
    conn.executemany("INSERT INTO suppliers(name,country,lead_days,price_mult,freight_pct,compliance,is_primary,caps,"
                     "categories,incoterm) VALUES (?,?,?,?,?,?,?,?,?,?)", s["suppliers"])
    conn.executemany("INSERT INTO permits(ref,category,country,valid_until) VALUES (?,?,?,?)",
                     [(*p[:3], day(p[3])) for p in s["permits"]])
    conn.executemany("INSERT INTO substitutes VALUES (?,?,?)", s["substitutes"])
    conn.executemany("INSERT INTO calendar(name,kind,start,end,factor,scope,sites) VALUES (?,?,?,?,?,?,?)",
                     [(n, k, day(start), day(start + length - 1), f, scope, sites)
                      for n, k, start, length, f, scope, sites in s.get("calendar", [])])
    conn.executemany("INSERT INTO scenarios(key,label,params) VALUES (?,?,?)",
                     [(k, label, json.dumps(params)) for k, (label, params) in s["scenarios"].items()])
    conn.executemany("INSERT INTO policy VALUES (?,?)", G.defaults(pack).items())
    for sku, *_ in s["items"]:
        for site, *_ in s["sites"]:
            lots, forecast, safety = s["stock"].get(sku, {}).get(site, ([], 0, 0))
            conn.execute("INSERT INTO positions VALUES (?,?,0,?,?)", (sku, site, forecast, safety))
            for i, (qty, days) in enumerate(lots, 1):
                conn.execute("INSERT INTO lots(sku,site,lot_no,qty,expiry) VALUES (?,?,?,?,?)",
                             (sku, site, f"{sku}-{site}{i}", qty, None if days is None else day(days)))
            conn.executemany("INSERT INTO demand(sku,site,day,qty) VALUES (?,?,?,?)",
                             [(sku, site, day(-d - 1), q) for d, q in
                              enumerate(history(forecast, s.get("volatility", {}).get(sku, 0.2))) if q])
    _supplier_history(conn, s, stamp)
    log(conn, f"Control tower started for {pack.title}. Monitoring {len(s['sites'])} "
              f"{pack.labels['sites'].lower()}, {len(s['items'])} {pack.labels['items'].lower()}.")


def _supplier_history(conn, s, stamp):
    """Past deliveries (for scorecards) and orders already overdue (for late-shipment alerts)."""
    sup = {r[0]: r for r in s["suppliers"]}
    items = {r[0]: r for r in s["items"]}
    sites = {r[0]: r for r in s["sites"]}
    customs = {r[0]: r[2] for r in s["countries"]}

    def order(name, sku, site, qty):
        _, country, lead, mult, freight, *_ = sup[name]
        imported = country != sites[site][3]
        lead += customs.get(sites[site][3], 0) if imported else 0
        unit = items[sku][7] * mult * (1 + freight / 100) * (1 + (items[sku][10] / 100 if imported else 0))
        return ("import" if imported else "po"), lead, round(qty * unit, 2)

    def insert(kind, sku, site, name, qty, value, status, created, eta, received=None, got=None):
        cur = conn.execute(
            "INSERT INTO actions(kind,sku,to_site,supplier,qty,value,status,approved_by,role,created_at,eta,"
            "received_at,received_qty,detail) VALUES (?,?,?,?,?,?,?,'sample history','history',?,?,?,?,?)",
            (kind, sku, site, name, qty, value, status, stamp(-created), stamp(-eta),
             stamp(-received) if received is not None else None, got, json.dumps({"lines": [], "sample": True})))
        conn.execute("INSERT INTO outcomes(day,kind,sku,site,units,value,ref,sample) VALUES (?,?,?,?,?,?,?,1)",
                     (stamp(-created)[:10], "spend", sku, site, qty, value, f"{kind}-{cur.lastrowid}"))

    for name, sku, site, qty, delays, short in s.get("deliveries", []):
        kind, lead, value = order(name, sku, site, qty)
        n = len(delays)
        for k, (late, miss) in enumerate(zip(delays, short)):
            received = 12 * (n - k)
            insert(kind, sku, site, name, qty, value, "received", received + late + lead, received + late, received,
                   qty - miss)
    for name, sku, site, qty, overdue in s.get("late", []):
        kind, lead, value = order(name, sku, site, qty)
        insert(kind, sku, site, name, qty, value, "in_transit", lead + overdue, overdue)
        conn.execute("UPDATE positions SET in_transit=in_transit+? WHERE sku=? AND site=?", (qty, sku, site))


def init():
    conn = connect()
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        reset(conn)
    conn.close()
