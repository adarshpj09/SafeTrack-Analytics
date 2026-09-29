"""
Generates SIMULATED GPS data that mimics SafeTrack's ESP32 + NEO-7M output.
Use this ONLY if you cannot export real rows from your Supabase table.
If you use it, say "simulated data" in your README.

Output: data/raw_locations.csv  (device_id, latitude, longitude, timestamp, sos)
"""
import numpy as np
import pandas as pd

rng = np.random.default_rng(42)

HOME_LAT, HOME_LON = 18.5204, 73.8567   # starting point (Pune)
rows = []

def add_trip(day_start, minutes, mode):
    """Random walk trip. mode: 'walk' (~5 km/h) or 'ride' (~30 km/h)."""
    lat, lon = HOME_LAT, HOME_LON
    step_s = 10                                   # one GPS reading every 10 s
    speed_ms = 1.4 if mode == "walk" else 8.0
    heading = rng.uniform(0, 2 * np.pi)
    t = day_start
    for _ in range(int(minutes * 60 / step_s)):
        heading += rng.normal(0, 0.25)
        d = max(0.0, rng.normal(speed_ms, speed_ms * 0.2)) * step_s   # metres
        lat += (d * np.cos(heading)) / 111_320
        lon += (d * np.sin(heading)) / (111_320 * np.cos(np.radians(lat)))
        rows.append(["ESP32-01", lat, lon, t, 0])
        t += pd.Timedelta(seconds=step_s)

for day in range(3):
    base = pd.Timestamp("2026-09-20 08:00:00") + pd.Timedelta(days=day)
    add_trip(base, 45, "walk")
    add_trip(base + pd.Timedelta(hours=5), 40, "ride")
    add_trip(base + pd.Timedelta(hours=9), 30, "walk")

df = pd.DataFrame(rows, columns=["device_id", "latitude", "longitude", "timestamp", "sos"])

# --- inject realistic messiness so the cleaning step has real work to do ---
n = len(df)
df.loc[rng.choice(n, 40, replace=False), "latitude"] = np.nan          # lost fix
df.loc[rng.choice(n, 15, replace=False), ["latitude", "longitude"]] = 0  # (0,0) glitch
jump_idx = rng.choice(n, 12, replace=False)                              # GPS jumps
df.loc[jump_idx, "latitude"] += rng.uniform(0.05, 0.2, 12)
df = pd.concat([df, df.sample(60, random_state=1)])                      # duplicates
df.loc[rng.choice(len(df), 8, replace=False), "sos"] = 1                 # SOS presses
df = df.sample(frac=1, random_state=7).reset_index(drop=True)            # shuffle

df.to_csv("data/raw_locations.csv", index=False)
print(f"Wrote data/raw_locations.csv with {len(df)} rows")
