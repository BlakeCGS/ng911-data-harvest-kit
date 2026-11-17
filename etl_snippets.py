r"""etl_snippets: reusable pieces of an NG911 Data Harvest ETL.

These functions come from an ArcGIS Pro Python toolbox that loads county
data into the Indiana Data Harvest state template. They work against the
template and the NENA standard, not against one county's schema.

- Pure Python (no arcpy): street name expansion, legacy street names,
  "State Road" handling, parity, NGUID, parcel ID, value cleanup,
  placeholder blanking.
- arcpy (ArcGIS Pro): run a record function on every row (update_each),
  municipality from A3 polygons, domain case normalization, the
  required-field gate, geometry issue lists, topology QC on copies, the
  multipart parcel split.

Use it in ArcGIS Pro (Python window or a notebook):
    import sys
    sys.path.append(r"C:\NG911Kit")      # the folder that holds this file
    import etl_snippets as ets

The arcpy functions are adapted from the toolbox: logging is replaced by
return values, and paths are parameters. Test them on a copy of your data.
"""

import re
from collections import Counter

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# Directional abbreviation -> NENA StreetNameDirectional domain value.
DIR_MAP = {
    "N": "North", "S": "South", "E": "East", "W": "West",
    "NE": "Northeast", "NW": "Northwest", "SE": "Southeast", "SW": "Southwest",
}

# USPS Publication 28, Appendix C1: street type abbreviation -> full type.
# An expansion is applied only when the full value is in the template's
# StreetNameType domain.
TYPE_MAP = {
    "ALY": "Alley", "ANX": "Annex", "ARC": "Arcade", "AVE": "Avenue",
    "BCH": "Beach", "BG": "Burg", "BGS": "Burgs", "BLF": "Bluff",
    "BLFS": "Bluffs", "BLVD": "Boulevard", "BND": "Bend", "BR": "Branch",
    "BRG": "Bridge", "BRK": "Brook", "BRKS": "Brooks", "BTM": "Bottom",
    "BYP": "Bypass", "BYU": "Bayou", "CIR": "Circle", "CIRS": "Circles",
    "CLB": "Club", "CLF": "Cliff", "CLFS": "Cliffs", "CMN": "Common",
    "CMNS": "Commons", "COR": "Corner", "CORS": "Corners", "CP": "Camp",
    "CPE": "Cape", "CRES": "Crescent", "CRK": "Creek", "CRSE": "Course",
    "CRST": "Crest", "CSWY": "Causeway", "CT": "Court", "CTR": "Center",
    "CTRS": "Centers", "CTS": "Courts", "CURV": "Curve", "CV": "Cove",
    "CVS": "Coves", "CYN": "Canyon", "DL": "Dale", "DM": "Dam",
    "DR": "Drive", "DRS": "Drives", "DV": "Divide", "EST": "Estate",
    "ESTS": "Estates", "EXPY": "Expressway", "EXT": "Extension",
    "EXTS": "Extensions", "FLD": "Field", "FLDS": "Fields", "FLS": "Falls",
    "FLT": "Flat", "FLTS": "Flats", "FRD": "Ford", "FRDS": "Fords",
    "FRG": "Forge", "FRGS": "Forges", "FRK": "Fork", "FRKS": "Forks",
    "FRST": "Forest", "FRY": "Ferry", "FT": "Fort", "FWY": "Freeway",
    "GDN": "Garden", "GDNS": "Gardens", "GLN": "Glen", "GLNS": "Glens",
    "GRN": "Green", "GRNS": "Greens", "GRV": "Grove", "GRVS": "Groves",
    "GTWY": "Gateway", "HBR": "Harbor", "HBRS": "Harbors", "HL": "Hill",
    "HLS": "Hills", "HOLW": "Hollow", "HTS": "Heights", "HVN": "Haven",
    "HWY": "Highway", "INLT": "Inlet", "IS": "Island", "ISLE": "Isle",
    "ISS": "Islands", "JCT": "Junction", "JCTS": "Junctions", "KNL": "Knoll",
    "KNLS": "Knolls", "KY": "Key", "KYS": "Keys", "LCK": "Lock",
    "LCKS": "Locks", "LDG": "Lodge", "LF": "Loaf", "LGT": "Light",
    "LGTS": "Lights", "LK": "Lake", "LKS": "Lakes", "LN": "Lane",
    "LNDG": "Landing", "MDW": "Meadow", "MDWS": "Meadows", "ML": "Mill",
    "MLS": "Mills", "MNR": "Manor", "MNRS": "Manors", "MSN": "Mission",
    "MT": "Mount", "MTN": "Mountain", "MTNS": "Mountains",
    "MTWY": "Motorway", "NCK": "Neck", "OPAS": "Overpass", "ORCH": "Orchard",
    "PSGE": "Passage", "PKWY": "Parkway", "PL": "Place", "PLN": "Plain",
    "PLNS": "Plains", "PLZ": "Plaza", "PNE": "Pine", "PNES": "Pines",
    "PR": "Prairie", "PRT": "Port", "PRTS": "Ports", "PT": "Point",
    "PTS": "Points", "RADL": "Radial", "RD": "Road", "RDG": "Ridge",
    "RDGS": "Ridges", "RDS": "Roads", "RIV": "River", "RNCH": "Ranch",
    "RPD": "Rapid", "RPDS": "Rapids", "RST": "Rest", "RTE": "Route",
    "SHL": "Shoal", "SHLS": "Shoals", "SHR": "Shore", "SHRS": "Shores",
    "SKWY": "Skyway", "SMT": "Summit", "SPG": "Spring", "SPGS": "Springs",
    "SQ": "Square", "SQS": "Squares", "ST": "Street", "STA": "Station",
    "STRA": "Stravenue", "STRM": "Stream", "STS": "Streets", "TER": "Terrace",
    "TPKE": "Turnpike", "TRAK": "Track", "TRCE": "Trace", "TRFY": "Trafficway",
    "TRL": "Trail", "TRWY": "Throughway", "TUNL": "Tunnel", "UN": "Union",
    "UNS": "Unions", "UPAS": "Underpass", "VIS": "Vista", "VL": "Ville",
    "VLG": "Village", "VLGS": "Villages", "VLY": "Valley", "VLYS": "Valleys",
    "VW": "View", "VWS": "Views", "WLS": "Wells", "XING": "Crossing",
    "XRD": "Crossroad", "XRDS": "Crossroads",
}

LEGACY_DIR_VALUES = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def _invert(mapping):
    """Full value -> shortest abbreviation, for the legacy LSt_* fields."""
    out = {}
    for abbrev, full in mapping.items():
        k = full.lower()
        if k not in out or len(abbrev) < len(out[k]):
            out[k] = abbrev
    return out


REV_TYPE_MAP = _invert(TYPE_MAP)
REV_DIR_MAP = _invert(DIR_MAP)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def clean_ws(v):
    """Remove extra whitespace. Change empty and '<Null>' strings to None."""
    if v is None:
        return None
    v = " ".join(str(v).split())
    if not v or v.lower() == "<null>":
        return None
    return v


def to_county(v):
    """Add the word 'County' to a value that does not end with 'County'."""
    if not v:
        return v
    v = v.strip()
    return v if v.lower().endswith("county") else v + " County"


def strip_pid(v):
    """Remove the dashes and the periods from a state parcel ID."""
    return v.replace("-", "").replace(".", "") if v else v


def blank_placeholder(v, placeholders=("unincorporated",)):
    """Change a placeholder value (for example 'Unincorporated' in
    Uninc_Comm) to None. A real value passes through unchanged.
    The comparison ignores case and extra spaces."""
    if v is None:
        return None
    if " ".join(str(v).split()).lower() in {p.lower() for p in placeholders}:
        return None
    return v


# ---------------------------------------------------------------------------
# Street names
# ---------------------------------------------------------------------------

def expand_dir(v, domain_vals):
    """Change a directional abbreviation to the full domain value."""
    if not v:
        return v
    key = v.strip().upper().replace(".", "")
    out = DIR_MAP.get(key)
    if out and out in domain_vals:
        return out
    # Match a full word that has a different case.
    for d in domain_vals:
        if v.strip().lower() == d.lower():
            return d
    return v


def expand_type(v, domain_vals, domain_lookup):
    """Change a street-type abbreviation to the full domain value.

    domain_lookup is {value.lower(): value} for the StreetNameType domain.
    If there is no match in the domain, return the input value.
    """
    if not v:
        return v
    v = v.strip()
    if v in domain_vals:
        return v
    # Match a full word that has a different case.
    hit = domain_lookup.get(v.lower())
    if hit:
        return hit
    out = TYPE_MAP.get(v.upper().replace(".", ""))
    if out and out in domain_vals:
        return out
    return v


def move_state_road(rec):
    """Move a leading 'State Road' from St_Name to St_PreTyp.

    Move a leading 'Old' to St_PreMod. rec is a dict with St_Name,
    St_PreTyp and St_PreMod. Return True if the record changed.
    Apply the same rule to address points AND centerlines, or a road and
    its addresses will not match.
    """
    nm = rec.get("St_Name")
    if not nm:
        return False
    m = re.match(r"^(old\s+)?state\s+road\b\s*(.*)$", nm, re.IGNORECASE)
    if not m or not m.group(2).strip():
        return False
    if m.group(1) and not (rec.get("St_PreMod") or "").strip():
        rec["St_PreMod"] = "Old"
    if not (rec.get("St_PreTyp") or "").strip():
        rec["St_PreTyp"] = "State Road"
    rec["St_Name"] = m.group(2).strip()
    return True


def legacy_dir(v):
    """Change an NG911 directional to the legacy two-character abbreviation."""
    if not v:
        return None
    key = v.strip().upper().replace(".", "")
    if key in LEGACY_DIR_VALUES:
        return key
    return REV_DIR_MAP.get(v.strip().lower())


def legacy_typ(v):
    """Change an NG911 street type to the legacy four-character abbreviation.

    Return None for a longer value that has no abbreviation.
    """
    if not v:
        return None
    key = v.strip().upper().replace(".", "")
    if key in TYPE_MAP:          # The value is an abbreviation.
        return key[:4]
    hit = REV_TYPE_MAP.get(v.strip().lower())
    if hit:
        return hit[:4]
    return key if len(key) <= 4 else None


def apply_legacy_name(rec):
    """Fill LSt_PreDir, LSt_Name, LSt_Typ and LSt_PosDir from the NG911
    street name fields. Fill only a record that has an empty LSt_Name.
    This assumes the legacy (MSAG/ALI) name equals the GIS name.
    Return True if the record changed."""
    if (rec.get("LSt_Name") or "").strip():
        return False
    name = (rec.get("St_Name") or "").strip()
    if not name:
        return False
    rec["LSt_Name"] = name[:75]
    rec["LSt_PreDir"] = legacy_dir(rec.get("St_PreDir"))
    rec["LSt_Typ"] = legacy_typ(rec.get("St_PosTyp"))
    rec["LSt_PosDir"] = legacy_dir(rec.get("St_PosDir"))
    return True


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

def parity(frm, to):
    """Calculate the NENA parity code from an address range pair."""
    if frm is None and to is None:
        return None
    f, t = frm or 0, to or 0
    if f == 0 and t == 0:
        return "Z"
    if f % 2 == 1 and t % 2 == 1:
        return "O"
    if f % 2 == 0 and t % 2 == 0:
        return "E"
    return "B"


def clean_local_id(v):
    """Remove whitespace, GUID braces, and a trailing '.0' from a LOCAL_ID
    value. Return None if the value is empty."""
    if v is None:
        return None
    lid = str(v).strip()
    if lid.startswith("{") and lid.endswith("}"):
        lid = lid[1:-1]
    if lid.endswith(".0") and lid[:-2].isdigit():
        lid = lid[:-2]
    return lid or None


def build_nguid(layer_indicator, local_id, agency):
    """NENA NGUID: urn:emergency:uid:gis:{layer}:{local id}:{agency}.

    layer_indicator: SSAP, RCL, PCL, A3, A4, Ems, Fire, Pol, Psap ...
    Build the local id from an ID that survives truncate/append reloads.
    """
    lid = clean_local_id(local_id)
    if lid is None:
        return None
    return "urn:emergency:uid:gis:{0}:{1}:{2}".format(
        layer_indicator, lid, agency)


# ---------------------------------------------------------------------------
# arcpy: run a record function on every row
# ---------------------------------------------------------------------------

def update_each(fc, fields, func, where=None):
    """Run func on every row of fc and save the rows that func changed.

    func takes one record (a dict of field name -> value) and returns True
    when it changed the record, like move_state_road and apply_legacy_name.
    fields lists every field that func reads or writes. where is an optional
    SQL filter. Return the number of rows changed. Use a copy of your data.
    """
    import arcpy
    changed = 0
    with arcpy.da.UpdateCursor(fc, list(fields), where) as cur:
        for row in cur:
            rec = dict(zip(fields, row))
            if func(rec):
                cur.updateRow([rec[f] for f in fields])
                changed += 1
    return changed


# ---------------------------------------------------------------------------
# arcpy: cross-layer fixes
# ---------------------------------------------------------------------------

def assign_inc_muni(points_fc, a3_fc, muni_field="Inc_Muni",
                    outside="Unincorporated"):
    """Set Inc_Muni on address points from the incorporated area (A3)
    polygon that holds each point. A point outside every A3 polygon gets
    'Unincorporated'. Return {municipality: points set}."""
    import arcpy
    with arcpy.da.UpdateCursor(points_fc, [muni_field]) as cur:
        for row in cur:
            row[0] = outside
            cur.updateRow(row)
    munis = sorted({m for (m,) in arcpy.da.SearchCursor(a3_fc, [muni_field]) if m})
    pts, a3lyr = "kit_incmuni_pts", "kit_incmuni_a3"
    for lyr in (pts, a3lyr):
        if arcpy.Exists(lyr):
            arcpy.management.Delete(lyr)
    arcpy.management.MakeFeatureLayer(points_fc, pts)
    arcpy.management.MakeFeatureLayer(a3_fc, a3lyr)
    counts = {}
    try:
        fld = arcpy.AddFieldDelimiters(a3_fc, muni_field)
        for muni in munis:
            arcpy.management.SelectLayerByAttribute(
                a3lyr, "NEW_SELECTION",
                "{0} = '{1}'".format(fld, muni.replace("'", "''")))
            arcpy.management.SelectLayerByLocation(
                pts, "INTERSECT", a3lyr, selection_type="NEW_SELECTION")
            n = int(arcpy.management.GetCount(pts)[0])
            counts[muni] = n
            if n:
                arcpy.management.CalculateField(
                    pts, muni_field, repr(muni), "PYTHON3")
        arcpy.management.SelectLayerByAttribute(pts, "CLEAR_SELECTION")
    finally:
        for lyr in (pts, a3lyr):
            if arcpy.Exists(lyr):
                arcpy.management.Delete(lyr)
    return counts


# ---------------------------------------------------------------------------
# arcpy: validate before load
# ---------------------------------------------------------------------------

def load_domains(target_gdb):
    """Return {domain name: set of codes} for the coded-value domains."""
    import arcpy
    out = {}
    for d in arcpy.da.ListDomains(target_gdb):
        if d.domainType == "CodedValue":
            out[d.name] = set(d.codedValues.keys())
    return out


def normalize_to_domains(staged_fc, target_fc, domains):
    """Change the case of values to agree with the coded-value domains of the
    target ('Bloomington' -> 'BLOOMINGTON'). Return {field: rows fixed}."""
    import arcpy
    field_dom = {}
    for f in arcpy.ListFields(target_fc):
        if f.type == "String" and f.domain and f.domain in domains:
            field_dom[f.name] = domains[f.domain]
    staged_names = {s.name for s in arcpy.ListFields(staged_fc)}
    cols = [n for n in field_dom if n in staged_names]
    if not cols:
        return {}
    lookups = {n: {v.lower(): v for v in field_dom[n]} for n in cols}
    idx = {n: i for i, n in enumerate(cols)}
    fixed = Counter()
    with arcpy.da.UpdateCursor(staged_fc, cols) as cur:
        for row in cur:
            changed = False
            for n in cols:
                v = row[idx[n]]
                if v and v not in field_dom[n]:
                    hit = lookups[n].get(v.lower())
                    if hit:
                        row[idx[n]] = hit
                        fixed[n] += 1
                        changed = True
            if changed:
                cur.updateRow(row)
    return dict(fixed)


def required_field_gate(staged_fc, target_fc, domains):
    """Count domain violations and nulls in non-nullable target fields.

    File-gdb domains are not enforced on append, so check them yourself.
    Return (blocking_rows, null_counts, domain_counts). Do not truncate the
    target when blocking_rows > 0.
    """
    import arcpy
    tf = {f.name: f for f in arcpy.ListFields(target_fc)
          if f.type not in ("OID", "Geometry", "GlobalID")}
    staged = {f.name for f in arcpy.ListFields(staged_fc)}
    check_domain = [(n, f.domain) for n, f in tf.items()
                    if n in staged and f.domain in domains]
    check_null = [n for n, f in tf.items() if n in staged and not f.isNullable]
    cols = sorted({n for n, _ in check_domain} | set(check_null))
    if not cols:
        return 0, {}, {}
    idx = {n: i for i, n in enumerate(cols)}
    null_bad, domain_bad, blockers = Counter(), Counter(), 0
    with arcpy.da.SearchCursor(staged_fc, cols) as cur:
        for row in cur:
            blocks = False
            for name, dom in check_domain:
                v = row[idx[name]]
                if v not in (None, "") and v not in domains[dom]:
                    domain_bad[name] += 1
            for name in check_null:
                if row[idx[name]] in (None, ""):
                    null_bad[name] += 1
                    blocks = True
            blockers += blocks
    return blockers, dict(null_bad), dict(domain_bad)


# ---------------------------------------------------------------------------
# arcpy: geometry and topology
# ---------------------------------------------------------------------------

TOPOLOGY_RULES_RCL = [
    "Must Not Overlap (Line)",
    "Must Not Self-Overlap (Line)",
    "Must Not Self-Intersect (Line)",
    "Must Be Single Part (Line)",
]
TOPOLOGY_RULES_PCL = [
    "Must Not Overlap (Area)",
    "Must Not Have Gaps (Area)",
]


def geometry_issues(fc, is_centerline, min_length_m=10):
    """List features to fix at the source: multipart, and (centerlines)
    shorter than min_length_m. Return [(oid, NGUID, LOCAL_ID, problem)].
    Keep NGUID and LOCAL_ID so the owner of the source can find each one."""
    import arcpy
    out = []
    with arcpy.da.SearchCursor(fc, ["OID@", "SHAPE@", "NGUID", "LOCAL_ID"]) as cur:
        for oid, g, nguid, local_id in cur:
            if not g:
                continue
            issues = []
            if g.partCount > 1:
                issues.append("multipart")
            if is_centerline and g.getLength("GEODESIC", "METERS") < min_length_m:
                issues.append("shorter than {0} m".format(min_length_m))
            if issues:
                out.append((oid, nguid, local_id, "; ".join(issues)))
    return out


def topology_qc(work_gdb, centerlines=None, parcels=None, sr=None):
    """Build and validate an NG911 topology on COPIES of the data.

    The input feature classes are not changed. Return the paths of the
    exported error feature classes that contain errors (TopoErrors_point,
    _line, _poly) and the path of the topology.
    """
    import os
    import arcpy
    sr = sr or arcpy.Describe(centerlines or parcels).spatialReference
    fds = os.path.join(work_gdb, "Topology_QC")
    arcpy.management.CreateFeatureDataset(work_gdb, "Topology_QC", sr)
    members = []
    if centerlines:
        fc = os.path.join(fds, "RoadCenterline_QC")
        arcpy.conversion.ExportFeatures(centerlines, fc)
        members.append((fc, TOPOLOGY_RULES_RCL))
    if parcels:
        fc = os.path.join(fds, "ParcelPolygon_QC")
        arcpy.conversion.ExportFeatures(parcels, fc)
        members.append((fc, TOPOLOGY_RULES_PCL))
    topo = os.path.join(fds, "QC_Topology")
    arcpy.management.CreateTopology(fds, "QC_Topology")
    for fc, _ in members:
        arcpy.management.AddFeatureClassToTopology(topo, fc, 1, 1)
    for fc, rules in members:
        for rule in rules:
            arcpy.management.AddRuleToTopology(topo, rule, fc)
    arcpy.management.ValidateTopology(topo)
    arcpy.management.ExportTopologyErrors(topo, work_gdb, "TopoErrors")
    out = []
    for suffix in ("_point", "_line", "_poly"):
        fc = os.path.join(work_gdb, "TopoErrors" + suffix)
        if arcpy.Exists(fc) and int(arcpy.management.GetCount(fc)[0]) > 0:
            out.append(fc)
    return out, topo


def split_multipart_parcels(staged_fc, agency):
    """Split multipart parcels; keep the parcel ID on every part.

    The first part keeps the LOCAL_ID; later parts take a suffix (-2, -3),
    so NGUIDs stay unique. Run AFTER Repair Geometry (OGC), which can create
    new multipart polygons. Return (parcels split, features added).
    Note: if the CAMA rule checks every parcel, each new part can add a
    CAMA anomaly. Measure the total before you keep this step.
    """
    import arcpy
    multi = sum(1 for (g,) in arcpy.da.SearchCursor(staged_fc, ["SHAPE@"])
                if g and g.partCount > 1)
    if not multi:
        return 0, 0
    before = int(arcpy.management.GetCount(staged_fc)[0])
    tmp = staged_fc + "_sp"
    if arcpy.Exists(tmp):
        arcpy.management.Delete(tmp)
    arcpy.management.MultipartToSinglepart(staged_fc, tmp)
    seen = Counter()
    with arcpy.da.UpdateCursor(
            tmp, ["ORIG_FID", "LOCAL_ID"],
            sql_clause=(None, "ORDER BY ORIG_FID, OBJECTID")) as cur:
        for row in cur:
            seen[row[0]] += 1
            if seen[row[0]] > 1 and row[1]:
                row[1] = "{0}-{1}".format(row[1], seen[row[0]])
                cur.updateRow(row)
    if "ORIG_FID" in {f.name for f in arcpy.ListFields(tmp)}:
        arcpy.management.DeleteField(tmp, "ORIG_FID")
    with arcpy.da.UpdateCursor(tmp, ["LOCAL_ID", "NGUID"]) as cur:
        for row in cur:
            row[0] = clean_local_id(row[0])
            row[1] = build_nguid("PCL", row[0], agency)
            cur.updateRow(row)
    after = int(arcpy.management.GetCount(tmp)[0])
    arcpy.management.Delete(staged_fc)
    arcpy.management.Rename(tmp, staged_fc)
    return multi, after - before
