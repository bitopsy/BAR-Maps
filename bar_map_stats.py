#!/usr/bin/env python3
"""
bar_map_stats.py - Statistics for Beyond All Reason / Spring-engine maps (.sd7, .sdz, or unpacked folder)

What it reports
  * Name, short name, author, version, description, file size, MD5
  * Map size (map units, elmos), height range, % water, start positions
  * Metal: number of mexes (metal spots), total / average / min / max metal per second,
    spot spacing, mexes per start position
  * Energy: number of geothermal vents, wind min/max, tidal strength
  * Features on the map and total reclaimable Metal / Energy

Where the data comes from
  mapinfo.lua                      -> name, author, wind, tidal, maxMetal, start positions...
  maps/*.smf                       -> size, heightmap, metalmap, placed features
  mapconfig/map_metal_layout.lua   -> metal spots (preferred; this is what BAR uses)
  (fallback: clusters of the SMF metalmap, values are then ESTIMATES)
  mapconfig/featureplacer/*.lua    -> extra features / geo vents placed by Lua
  features/*.lua (inside the map)  -> reclaim values (metal / energy) of custom features

Reclaim values of features that are defined by the *game* (BAR rocks, trees, wrecks...) are not
inside the map archive. To include them, pass the game with --game (BAR .sd7 or unpacked .sdd
folder), or give a JSON file with --featuredefs  {"feature_name": {"metal": 10, "energy": 0}}.

Requirements: Python 3.8+, and either `pip install py7zr` or a 7z/7za/7zz binary on PATH.

Usage
  python bar_map_stats.py "Relativity_V1.sd7"
  python bar_map_stats.py map1.sd7 map2.sd7 --game "BAR.sdd" --list-spots
  python bar_map_stats.py map.sd7 --json > stats.json
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from array import array
from collections import Counter
from pathlib import Path

# ----------------------------------------------------------------------------------------------
# Archive access
# ----------------------------------------------------------------------------------------------

def norm(name):
    n = name.replace("\\", "/").lstrip("./").lower()
    return n


def want_map(n):
    return (
        n == "mapinfo.lua"
        or (n.startswith("maps/") and n.endswith(".smf"))
        or (n.startswith("mapconfig/") and n.endswith(".lua"))
        or (n.startswith("features/") and n.endswith(".lua"))
    )


def want_game(n):
    return n.startswith("features/") and n.endswith(".lua")


def _read_dir(root, pred):
    out = {}
    root = Path(root)
    for p in root.rglob("*"):
        if p.is_file():
            n = norm(str(p.relative_to(root)))
            if pred(n):
                out[n] = p.read_bytes()
    return out


def _read_zip(path, pred):
    out = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            n = norm(info.filename)
            if not info.is_dir() and pred(n):
                out[n] = z.read(info)
    return out


def _read_7z(path, pred):
    try:
        import py7zr
    except ImportError:
        py7zr = None

    if py7zr is not None:
        tmp = tempfile.mkdtemp(prefix="barmap_")
        try:
            with py7zr.SevenZipFile(path, "r") as z:
                names = [i.filename for i in z.list() if not i.is_directory]
                wanted = [n for n in names if pred(norm(n))]
            if wanted:
                with py7zr.SevenZipFile(path, "r") as z:
                    z.extract(path=tmp, targets=wanted)
            return _read_dir(tmp, pred)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    exe = next((shutil.which(x) for x in ("7z", "7zz", "7za", "7zr") if shutil.which(x)), None)
    if exe is None:
        sys.exit("Need either py7zr (pip install py7zr) or a 7z binary on PATH to read .sd7 files.")
    tmp = tempfile.mkdtemp(prefix="barmap_")
    try:
        cmd = [exe, "x", "-y", f"-o{tmp}", str(path)]
        for ext in ("smt", "dds", "png", "jpg", "jpeg", "tga", "ogg", "wav", "s3o", "3do", "bmp"):
            cmd.append(f"-xr!*.{ext}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _read_dir(tmp, pred)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_files(path, pred):
    path = Path(path)
    if path.is_dir():
        return _read_dir(path, pred)
    ext = path.suffix.lower()
    if ext in (".sdz", ".zip"):
        return _read_zip(path, pred)
    return _read_7z(path, pred)


# ----------------------------------------------------------------------------------------------
# Tiny Lua helpers (regex based; good enough for mapinfo / layout / feature files)
# ----------------------------------------------------------------------------------------------

_COMMENT_RE = re.compile(
    r'("(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')|--\[(=*)\[.*?\]\2\]|--[^\n]*', re.S
)
_NUM = r"(-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|-?\.\d+)"


def lua_text(raw):
    t = raw.decode("utf-8", "replace")
    return _COMMENT_RE.sub(lambda m: m.group(1) or "", t)


def lua_str(t, key):
    m = re.search(rf"\b{key}\s*=\s*([\"'])(.*?)\1", t, re.S | re.I)
    return m.group(2).strip() if m else None


def lua_num(t, key):
    m = re.search(rf"\b{key}\s*=\s*{_NUM}", t, re.I)
    return float(m.group(1)) if m else None


def innermost_blocks(t):
    return re.findall(r"\{([^{}]*)\}", t)


def toplevel_tables(t):
    """Yield (key, body) for every `key = { ... }` that sits directly inside the first table."""
    i = t.find("{")
    if i < 0:
        return
    depth, j, n, start = 0, i, len(t), 0
    while j < n:
        c = t[j]
        if c in "\"'":
            q = c
            j += 1
            while j < n and t[j] != q:
                if t[j] == "\\":
                    j += 1
                j += 1
        elif c == "{":
            depth += 1
            if depth == 2:
                start = j
                pre = t[max(0, j - 160):j]
                m = re.search(r"(?:\[\s*[\"']([^\"']+)[\"']\s*\]|([\w\-]+))\s*=\s*$", pre)
                key = (m.group(1) or m.group(2)) if m else None
        elif c == "}":
            if depth == 2 and key:
                yield key.lower(), t[start + 1:j]
            depth -= 1
            if depth <= 0:
                break
        j += 1


def parse_featuredefs(raw):
    defs = {}
    t = lua_text(raw)
    for key, body in toplevel_tables(t):
        metal = lua_num(body, "metal")
        energy = lua_num(body, "energy")
        if metal is not None or energy is not None:
            defs[key] = (metal or 0.0, energy or 0.0)
    return defs


# ----------------------------------------------------------------------------------------------
# mapinfo.lua
# ----------------------------------------------------------------------------------------------

def parse_mapinfo(raw):
    if not raw:
        return {}
    t = lua_text(raw)
    d = {k: lua_str(t, k) for k in ("name", "shortname", "author", "description", "version")}
    for k in ("maxMetal", "extractorRadius", "gravity", "tidalStrength", "minWind", "maxWind",
              "minheight", "maxheight"):
        d[k] = lua_num(t, k)
    d["starts"] = [
        (float(x), float(z))
        for x, z in re.findall(
            rf"startPos\s*=\s*\{{\s*x\s*=\s*{_NUM}\s*,\s*z\s*=\s*{_NUM}", t, re.I)
    ]
    return d


# ----------------------------------------------------------------------------------------------
# SMF (binary map file)
# ----------------------------------------------------------------------------------------------

SMF_HDR = struct.Struct("<16s7i2f7i")  # 80 bytes


def parse_smf(raw):
    if len(raw) < SMF_HDR.size:
        raise ValueError("SMF file too small")
    (magic, version, mapid, mapx, mapy, square, texel, tile, hmin, hmax,
     hptr, tptr, tilesptr, miniptr, mptr, fptr, nextra) = SMF_HDR.unpack_from(raw, 0)
    if not magic.startswith(b"spring map file"):
        raise ValueError("Not a Spring map file (bad magic)")

    smf = dict(version=version, mapx=mapx, mapy=mapy, square=square, hmin=hmin, hmax=hmax)

    # Heightmap
    n_h = (mapx + 1) * (mapy + 1)
    hm = array("H")
    hm.frombytes(raw[hptr:hptr + n_h * 2])
    if sys.byteorder == "big":
        hm.byteswap()
    smf["heightmap"] = hm

    # Metalmap: one byte per 16x16 elmo square
    mw, mh = mapx // 2, mapy // 2
    smf["metalmap"] = raw[mptr:mptr + mw * mh]
    smf["metal_w"], smf["metal_h"] = mw, mh

    # Features
    feats = []
    if fptr and fptr + 8 <= len(raw):
        n_types, n_feats = struct.unpack_from("<ii", raw, fptr)
        pos = fptr + 8
        names = []
        for _ in range(n_types):
            end = raw.index(b"\0", pos)
            names.append(raw[pos:end].decode("latin-1").lower())
            pos = end + 1
        rec = struct.Struct("<i5f")
        for _ in range(n_feats):
            if pos + rec.size > len(raw):
                break
            ftype, x, y, z, rot, size = rec.unpack_from(raw, pos)
            pos += rec.size
            if 0 <= ftype < len(names):
                feats.append((names[ftype], x, z))
    smf["features"] = feats
    return smf


# ----------------------------------------------------------------------------------------------
# Metal spots
# ----------------------------------------------------------------------------------------------

def spots_from_layout(files):
    """Parse mapconfig/*metal*layout*.lua -> list of (x, z, metal). Metal defaults to 2.0."""
    for n, raw in sorted(files.items()):
        base = n.rsplit("/", 1)[-1]
        if n.startswith("mapconfig/") and "metal" in base:
            t = lua_text(raw)
            spots = []
            for b in innermost_blocks(t):
                x, z = lua_num(b, "x"), lua_num(b, "z")
                if x is None or z is None:
                    continue
                m = lua_num(b, "metal")
                spots.append((x, z, 2.0 if m is None else m))
            if spots:
                return spots, n
    return [], None


def spots_from_metalmap(smf, max_metal, scale=1.0):
    """Fallback: group non-zero metalmap squares into clusters (8-connectivity)."""
    mm, w, h = smf["metalmap"], smf["metal_w"], smf["metal_h"]
    seen = bytearray(len(mm))
    spots = []
    for start in range(len(mm)):
        if mm[start] == 0 or seen[start]:
            continue
        stack, total, sx, sz, cnt = [start], 0, 0.0, 0.0, 0
        seen[start] = 1
        while stack:
            i = stack.pop()
            cx, cz = i % w, i // w
            v = mm[i]
            total += v
            sx += (cx + 0.5) * v
            sz += (cz + 0.5) * v
            cnt += 1
            for dz in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, nz = cx + dx, cz + dz
                    if 0 <= nx < w and 0 <= nz < h:
                        j = nz * w + nx
                        if mm[j] and not seen[j]:
                            seen[j] = 1
                            stack.append(j)
        spots.append((sx / total * 16, sz / total * 16, total / 255.0 * max_metal * scale))
    return spots


def nn_stats(pts):
    if len(pts) < 2:
        return None
    ds = [min(math.hypot(a[0] - b[0], a[1] - b[1]) for j, b in enumerate(pts) if j != i)
          for i, a in enumerate(pts)]
    return min(ds), sum(ds) / len(ds)


# ----------------------------------------------------------------------------------------------
# Features / reclaim
# ----------------------------------------------------------------------------------------------

GEO_RE = re.compile(r"geo.?vent", re.I)


def features_from_config(files):
    out = []
    for n, raw in files.items():
        base = n.rsplit("/", 1)[-1]
        if n.startswith("mapconfig/") and ("featureplacer" in n or "feature" in base):
            t = lua_text(raw)
            for b in innermost_blocks(t):
                nm = lua_str(b, "name")
                x, z = lua_num(b, "x"), lua_num(b, "z")
                if nm and x is not None and z is not None:
                    out.append((nm.lower(), x, z))
    return out


def md5_of(path):
    path = Path(path)
    if not path.is_file():
        return None
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------------------------

def analyze(path, extra_defs, mex_scale=1.0):
    files = load_files(path, want_map)
    if not files:
        raise ValueError("No map files found in archive (is this a Spring map?)")

    info = parse_mapinfo(files.get("mapinfo.lua"))
    smf_name = next((n for n in sorted(files) if n.endswith(".smf")), None)
    if not smf_name:
        raise ValueError("No maps/*.smf found")
    smf = parse_smf(files[smf_name])

    r = {"file": str(path), "smf": smf_name}
    p = Path(path)
    if p.is_file():
        r["file_size_mb"] = round(p.stat().st_size / 1048576, 2)
        r["md5"] = md5_of(p)

    fallback_name = Path(smf_name).stem.replace("_", " ")
    r["name"] = info.get("name") or fallback_name
    for k in ("shortname", "author", "version", "description"):
        r[k] = info.get(k)

    # --- size
    mapx, mapy, sq = smf["mapx"], smf["mapy"], smf["square"]
    r["size_units"] = (mapx / 64, mapy / 64)
    r["size_elmos"] = (mapx * sq, mapy * sq)
    hmin = info.get("minheight") if info.get("minheight") is not None else smf["hmin"]
    hmax = info.get("maxheight") if info.get("maxheight") is not None else smf["hmax"]
    r["height_range"] = (hmin, hmax)
    hm = smf["heightmap"]
    if hm and hmax > hmin and hmin < 0:
        thr = (0 - hmin) / (hmax - hmin) * 65535
        r["water_pct"] = round(100.0 * sum(1 for v in hm if v < thr) / len(hm), 1)
    else:
        r["water_pct"] = 0.0
    r["start_positions"] = len(info.get("starts", []))

    # --- metal
    max_metal = info.get("maxMetal") if info.get("maxMetal") is not None else 0.02
    spots, src = spots_from_layout(files)
    if spots:
        r["mex_source"] = src
        r["mex_estimated"] = False
    else:
        spots = spots_from_metalmap(smf, max_metal, mex_scale)
        r["mex_source"] = "metalmap clusters (estimate)"
        r["mex_estimated"] = True
    vals = [s[2] for s in spots]
    r["mex_count"] = len(spots)
    r["metal_total_per_s"] = round(sum(vals), 3)
    r["metal_avg_per_spot"] = round(sum(vals) / len(vals), 3) if vals else 0.0
    r["metal_min_max"] = (min(vals), max(vals)) if vals else (0, 0)
    r["metal_value_histogram"] = dict(sorted(Counter(round(v, 2) for v in vals).items()))
    r["mex_per_start"] = (round(len(spots) / r["start_positions"], 2)
                          if r["start_positions"] else None)
    nn = nn_stats([(s[0], s[1]) for s in spots])
    r["mex_spacing_min_avg_elmos"] = tuple(round(x) for x in nn) if nn else None
    area_units = (mapx / 64) * (mapy / 64)
    r["mex_per_map_unit2"] = round(len(spots) / area_units, 2) if area_units else None
    mm = smf["metalmap"]
    r["metalmap_nonzero_squares"] = sum(1 for b in mm if b)
    r["maxMetal"] = info.get("maxMetal")
    r["extractorRadius"] = info.get("extractorRadius")
    r["_spots"] = spots

    # --- features, geos, reclaim
    feats = [(n, x, z, "smf") for n, x, z in smf["features"]]
    feats += [(n, x, z, "lua") for n, x, z in features_from_config(files)]

    geos, others = [], []
    for n, x, z, srcf in feats:
        if GEO_RE.search(n):
            if not any(abs(x - gx) < 16 and abs(z - gz) < 16 for gx, gz in geos):
                geos.append((x, z))
        else:
            others.append(n)
    r["geo_count"] = len(geos)
    r["_geos"] = geos

    defs = {}
    for n, raw in files.items():
        if n.startswith("features/") and n.endswith(".lua"):
            defs.update(parse_featuredefs(raw))
    defs.update(extra_defs)

    counts = Counter(others)
    tot_m = tot_e = 0.0
    resolved, unresolved = {}, {}
    for name, c in counts.items():
        if name in defs:
            m, e = defs[name]
            tot_m += m * c
            tot_e += e * c
            resolved[name] = {"count": c, "metal_each": m, "energy_each": e}
        else:
            unresolved[name] = c
    r["feature_count"] = sum(counts.values())
    r["reclaim_metal"] = round(tot_m)
    r["reclaim_energy"] = round(tot_e)
    r["features_resolved"] = resolved
    r["features_unresolved"] = unresolved
    r["reclaim_complete"] = not unresolved

    # --- energy sources
    r["wind_min_max"] = (info.get("minWind"), info.get("maxWind"))
    r["tidal_strength"] = info.get("tidalStrength")
    r["gravity"] = info.get("gravity")
    return r


# ----------------------------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------------------------

def fmt(v, nd=1):
    return "n/a" if v is None else (f"{v:,.{nd}f}" if isinstance(v, float) else f"{v:,}")


def print_report(r, list_spots=False, top=10):
    line = "=" * 66
    print(line)
    print(f" {r['name']}" + (f"  v{r['version']}" if r.get("version") else ""))
    print(line)
    print(f" Author        : {r.get('author') or 'unknown'}")
    if r.get("description"):
        print(f" Description   : {r['description']}")
    if r.get("file_size_mb") is not None:
        print(f" Archive       : {r['file_size_mb']} MB   md5 {r['md5']}")
    print(f" SMF file      : {r['smf']}")

    w, h = r["size_units"]
    ew, eh = r["size_elmos"]
    print("\n-- Map " + "-" * 58)
    print(f" Size          : {w:g} x {h:g} map units  ({ew:,} x {eh:,} elmos)")
    print(f" Height range  : {fmt(r['height_range'][0], 0)} .. {fmt(r['height_range'][1], 0)}"
          f"   water coverage ~{r['water_pct']}%")
    print(f" Start pos     : {r['start_positions']} (from mapinfo.lua)")
    if r.get("gravity") is not None:
        print(f" Gravity       : {r['gravity']:g}")

    print("\n-- Metal " + "-" * 56)
    flag = "  [ESTIMATE]" if r["mex_estimated"] else ""
    print(f" Source        : {r['mex_source']}{flag}")
    print(f" Mexes         : {r['mex_count']}")
    print(f" Total metal/s : {r['metal_total_per_s']:g}")
    print(f" Avg m/s / mex : {r['metal_avg_per_spot']:g}   "
          f"(min {r['metal_min_max'][0]:g}, max {r['metal_min_max'][1]:g})")
    if r["mex_per_start"] is not None:
        print(f" Mex / start   : {r['mex_per_start']}")
    if r["mex_per_map_unit2"] is not None:
        print(f" Mex density   : {r['mex_per_map_unit2']} per map unit^2")
    if r["mex_spacing_min_avg_elmos"]:
        a, b = r["mex_spacing_min_avg_elmos"]
        print(f" Nearest mex   : min {a} / avg {b} elmos")
    if len(r["metal_value_histogram"]) > 1:
        hist = ", ".join(f"{k:g} m/s x{v}" for k, v in r["metal_value_histogram"].items())
        print(f" Spot values   : {hist}")
    if r.get("maxMetal") is not None:
        print(f" maxMetal      : {r['maxMetal']:g}   extractorRadius: {r.get('extractorRadius')}")

    print("\n-- Energy " + "-" * 55)
    print(f" Geo vents     : {r['geo_count']}")
    lo, hi = r["wind_min_max"]
    print(f" Wind          : {fmt(lo)} .. {fmt(hi)}   tidal: {fmt(r['tidal_strength'])}")

    print("\n-- Reclaim " + "-" * 54)
    print(f" Features      : {r['feature_count']:,}")
    print(f" Reclaim metal : {r['reclaim_metal']:,}")
    print(f" Reclaim energy: {r['reclaim_energy']:,}")
    if not r["reclaim_complete"]:
        tot_un = sum(r["features_unresolved"].values())
        print(f" ! {tot_un:,} features have no known metal/energy value, so the totals above")
        print("   are a lower bound. Pass --game <BAR.sdd|BAR.sd7> to resolve them.")
        for name, c in sorted(r["features_unresolved"].items(), key=lambda kv: -kv[1])[:top]:
            print(f"     {c:>5} x {name}")
    if r["features_resolved"]:
        print(" Top reclaimable features:")
        rows = sorted(r["features_resolved"].items(),
                      key=lambda kv: -(kv[1]["count"] * (kv[1]["metal_each"] + kv[1]["energy_each"])))
        for name, d in rows[:top]:
            print(f"     {d['count']:>5} x {name:<28} M {d['metal_each']:g}  E {d['energy_each']:g}")

    if list_spots:
        print("\n-- Mex list (x, z, metal/s) " + "-" * 38)
        for x, z, m in r["_spots"]:
            print(f"   {x:8.0f} {z:8.0f}   {m:g}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Stats for Beyond All Reason / Spring maps")
    ap.add_argument("maps", nargs="+", help=".sd7 / .sdz file or unpacked map folder")
    ap.add_argument("--game", action="append", default=[],
                    help="BAR game archive/folder to read feature (reclaim) definitions from")
    ap.add_argument("--featuredefs", help='JSON {"name": {"metal": N, "energy": N}}')
    ap.add_argument("--mex-scale", type=float, default=1.0,
                    help="multiplier for metalmap-fallback m/s estimates")
    ap.add_argument("--list-spots", action="store_true", help="print every mex position")
    ap.add_argument("--top", type=int, default=10, help="rows in feature tables")
    ap.add_argument("--json", action="store_true", help="JSON output")
    a = ap.parse_args()

    extra = {}
    for g in a.game:
        for n, raw in load_files(g, want_game).items():
            extra.update(parse_featuredefs(raw))
    if a.featuredefs:
        with open(a.featuredefs, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                extra[k.lower()] = (float(v.get("metal", 0)), float(v.get("energy", 0)))

    results = []
    for m in a.maps:
        try:
            r = analyze(m, extra, a.mex_scale)
        except Exception as e:  # keep going on multi-map runs
            print(f"[error] {m}: {e}", file=sys.stderr)
            continue
        results.append(r)
        if not a.json:
            print_report(r, a.list_spots, a.top)

    if a.json:
        out = []
        for r in results:
            r = dict(r)
            if not a.list_spots:
                r.pop("_spots", None)
                r.pop("_geos", None)
            else:
                r["mexes"] = [{"x": x, "z": z, "metal": m} for x, z, m in r.pop("_spots")]
                r["geos"] = [{"x": x, "z": z} for x, z in r.pop("_geos")]
            out.append(r)
        print(json.dumps(out if len(out) != 1 else out[0], indent=2))

    if len(results) > 1 and not a.json:
        print("Summary")
        print(f"{'Map':<30}{'Size':>8}{'Mex':>6}{'M/s':>9}{'Avg':>6}{'Geo':>5}{'Rec.M':>9}{'Rec.E':>9}")
        for r in results:
            sz = f"{r['size_units'][0]:g}x{r['size_units'][1]:g}"
            print(f"{r['name'][:29]:<30}{sz:>8}{r['mex_count']:>6}{r['metal_total_per_s']:>9g}"
                  f"{r['metal_avg_per_spot']:>6g}{r['geo_count']:>5}{r['reclaim_metal']:>9,}"
                  f"{r['reclaim_energy']:>9,}")


if __name__ == "__main__":
    main()
