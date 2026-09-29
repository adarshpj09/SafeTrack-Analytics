"""
SafeTrack Analytics: ETL pipeline + SQL analysis on GPS location data.

Flow:  raw CSV  ->  clean  ->  transform  ->  load into SQLite  ->  SQL analysis  ->  charts
Run:   python pipeline.py
"""
import sqlite3
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RAW_CSV = "data/raw_locations.csv"
DB_PATH = "output/safetrack.db"

# Geofence: circle around this point (change to your own safe zone)
GEO_LAT, GEO_LON, GEO_RADIUS_M = 18.5847, 73.7376, 500   # campus safe zone (approx.)
MAX_SPEED_KMH = 120          # anything faster between two fixes = GPS glitch


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two lat/lon points (vectorised)."""
    R = 6_371_000
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


# ---------------------------------------------------------------- EXTRACT
def extract():
    df = pd.read_csv(RAW_CSV)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    print(f"[extract] {len(df)} raw rows")
    return df


# ------------------------------------------------------------------ CLEAN
def clean(df):
    n0 = len(df)
    df = df.drop_duplicates()
    df = df.dropna(subset=["latitude", "longitude", "timestamp"])
    df = df[df["latitude"].between(-90, 90) & df["longitude"].between(-180, 180)]
    df = df[~((df["latitude"] == 0) & (df["longitude"] == 0))]      # (0,0) = no fix
    df = df.sort_values(["device_id", "timestamp"]).reset_index(drop=True)
    print(f"[clean]   {n0} -> {len(df)} rows (removed {n0 - len(df)})")
    return df


# -------------------------------------------------------------- TRANSFORM
def transform(df):
    g = df.groupby("device_id")
    prev_lat, prev_lon, prev_ts = g["latitude"].shift(), g["longitude"].shift(), g["timestamp"].shift()

    df["dist_m"] = haversine_m(prev_lat, prev_lon, df["latitude"], df["longitude"])
    df["dt_s"] = (df["timestamp"] - prev_ts).dt.total_seconds()
    df["speed_kmh"] = (df["dist_m"] / df["dt_s"]) * 3.6

    # Anomalies: physically impossible jumps (GPS glitches)
    df["is_glitch"] = (df["speed_kmh"] > MAX_SPEED_KMH).astype(int)
    glitches = df[df["is_glitch"] == 1].copy()
    df = df[df["is_glitch"] == 0].copy()

    # Recompute distance/speed after removing glitches so they don't pollute totals
    g = df.groupby("device_id")
    df["dist_m"] = haversine_m(g["latitude"].shift(), g["longitude"].shift(),
                               df["latitude"], df["longitude"])
    df["dt_s"] = (df["timestamp"] - g["timestamp"].shift()).dt.total_seconds()
    df["speed_kmh"] = (df["dist_m"] / df["dt_s"]) * 3.6

    # Geofence flag + time features
    df["dist_from_zone_m"] = haversine_m(df["latitude"], df["longitude"], GEO_LAT, GEO_LON)
    df["inside_geofence"] = (df["dist_from_zone_m"] <= GEO_RADIUS_M).astype(int)
    df["date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
    df["hour"] = df["timestamp"].dt.hour
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    glitches["timestamp"] = glitches["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    print(f"[transform] {len(glitches)} GPS glitches flagged, {len(df)} clean rows kept")
    return df.fillna({"dist_m": 0, "dt_s": 0, "speed_kmh": 0}), glitches


# ------------------------------------------------------------------- LOAD
def load(df, glitches):
    con = sqlite3.connect(DB_PATH)
    df.to_sql("locations", con, if_exists="replace", index=False)
    glitches[["device_id", "latitude", "longitude", "timestamp", "speed_kmh"]] \
        .to_sql("gps_glitches", con, if_exists="replace", index=False)
    print(f"[load]    wrote tables 'locations' and 'gps_glitches' to {DB_PATH}")
    return con


# ------------------------------------------------------------ SQL ANALYSIS
QUERIES = {
    "1. Daily distance and average speed": """
        SELECT date,
               COUNT(*)                          AS readings,
               ROUND(SUM(dist_m) / 1000.0, 2)    AS distance_km,
               ROUND(AVG(speed_kmh), 2)          AS avg_speed_kmh,
               ROUND(MAX(speed_kmh), 2)          AS max_speed_kmh
        FROM locations
        GROUP BY date
        ORDER BY date;
    """,
    "2. Activity by hour of day": """
        SELECT hour, COUNT(*) AS readings, ROUND(AVG(speed_kmh), 2) AS avg_speed_kmh
        FROM locations
        GROUP BY hour
        ORDER BY hour;
    """,
    "3. Geofence exits (window function: LAG)": """
        WITH flagged AS (
            SELECT timestamp, inside_geofence,
                   LAG(inside_geofence) OVER (PARTITION BY device_id ORDER BY timestamp) AS prev_inside
            FROM locations
        )
        SELECT date(timestamp) AS date, COUNT(*) AS geofence_exits
        FROM flagged
        WHERE prev_inside = 1 AND inside_geofence = 0
        GROUP BY date(timestamp)
        ORDER BY date;
    """,
    "4. SOS events with location": """
        SELECT timestamp, ROUND(latitude, 5) AS lat, ROUND(longitude, 5) AS lon
        FROM locations
        WHERE sos = 1
        ORDER BY timestamp;
    """,
    "5. Movement type (walking vs vehicle) by speed band": """
        SELECT CASE WHEN speed_kmh < 1   THEN 'stationary'
                    WHEN speed_kmh < 8   THEN 'walking'
                    ELSE 'vehicle' END   AS movement,
               COUNT(*)                  AS readings,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM locations), 1) AS pct
        FROM locations
        GROUP BY movement
        ORDER BY readings DESC;
    """,
}


def run_sql(con):
    for title, q in QUERIES.items():
        print(f"\n--- {title} ---")
        print(pd.read_sql_query(q, con).to_string(index=False))


# ----------------------------------------------------------------- CHARTS
def charts(df):
    fig, ax = plt.subplots(figsize=(6, 6))
    inside = df[df["inside_geofence"] == 1]
    outside = df[df["inside_geofence"] == 0]
    ax.scatter(outside["longitude"], outside["latitude"], s=3, label="outside geofence")
    ax.scatter(inside["longitude"], inside["latitude"], s=3, c="green", label="inside geofence")
    sos = df[df["sos"] == 1]
    ax.scatter(sos["longitude"], sos["latitude"], s=60, c="red", marker="x", label="SOS")
    ax.set_title("Location trail"); ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    ax.legend(); fig.tight_layout(); fig.savefig("output/trail.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.hist(df["speed_kmh"], bins=50)
    ax.set_title("Speed distribution"); ax.set_xlabel("km/h"); ax.set_ylabel("readings")
    fig.tight_layout(); fig.savefig("output/speed_hist.png", dpi=150); plt.close(fig)
    print("\n[charts]  saved output/trail.png and output/speed_hist.png")


if __name__ == "__main__":
    raw = extract()
    cleaned = clean(raw)
    final, glitch_rows = transform(cleaned)
    connection = load(final, glitch_rows)
    run_sql(connection)
    charts(final)
    connection.close()
