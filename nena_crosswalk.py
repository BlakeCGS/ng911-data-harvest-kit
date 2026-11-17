"""nena_crosswalk: a field crosswalk for loading local GIS data into a
NENA-based state template geodatabase.

The crosswalk is one CSV file. It has one row for each field of each target
feature class. The template geodatabase supplies the target side of every
row, so a county only fills in the source side.

The module does four jobs:

1. Read a schema: the target template, or a source layer.
   - read_schema_arcpy()      needs ArcGIS Pro (arcpy). Gives string lengths.
   - read_schema_gdb_items()  needs only pyogrio (GDAL). It reads the
                              geodatabase's own catalog. No string lengths.
2. Make a crosswalk from a template, with suggested source fields
   (make_rows). The suggestions come from exact names, field aliases, a
   synonym list, and loose name matches.
3. Refresh a crosswalk when the template changes (refresh_rows). It keeps
   every choice that still applies and flags new, changed, and removed
   target fields.
4. Check a crosswalk against the live template and the source layers before
   a load (validate), and turn it into a lookup for the load step
   (to_lookup).

The module has no ArcGIS dependency at import time.
"""

import csv
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict, namedtuple

__version__ = "0.1.0"

# The CSV columns, in file order. A person edits only source_field, method
# (to set 'skip') and notes. The tools fill the other columns.
COLUMNS = ["layer", "target_field", "source_field", "method", "status",
           "required", "type", "length", "domain", "alias", "notes"]

# method values
EXACT, ALIAS, SYNONYM, LOOSE, MANUAL, SKIP = (
    "exact", "alias", "synonym", "loose", "manual", "skip")
# status values (blank means "no change since the crosswalk was made")
NEW, CHANGED, REMOVED = "NEW", "CHANGED", "REMOVED"

# Separator between fallback source fields in one cell: "a | b".
SEP = "|"

# Field types that a crosswalk never maps.
SYSTEM_TYPES = {"OID", "Geometry", "GlobalID", "Raster", "Blob"}
SYSTEM_NAMES = {"shape_length", "shape_area", "shape.stlength()",
                "shape.starea()", "objectid"}

Field = namedtuple("Field", "name alias type length nullable domain")


# ---------------------------------------------------------------------------
# Schema readers. A schema is {layer_name: [Field, ...]} in field order.
# ---------------------------------------------------------------------------

def _keep(f):
    return f.type not in SYSTEM_TYPES and f.name.lower() not in SYSTEM_NAMES


def read_schema_arcpy(workspace_or_fc, layers=None):
    """Read field definitions with arcpy.

    workspace_or_fc is a geodatabase (all feature classes and tables, or only
    the names in layers) or one feature class / layer.
    """
    import arcpy  # noqa: imported here so the module loads without Pro

    def one(path):
        out = []
        for f in arcpy.ListFields(path):
            fld = Field(f.name, f.aliasName or "", f.type,
                        f.length if f.type == "String" else None,
                        bool(f.isNullable), f.domain or "")
            if _keep(fld):
                out.append(fld)
        return out

    desc = arcpy.Describe(workspace_or_fc)
    if getattr(desc, "dataType", "") != "Workspace":
        return {desc.name: one(workspace_or_fc)}

    schema = OrderedDict()
    with arcpy.EnvManager(workspace=workspace_or_fc):
        names = list(arcpy.ListFeatureClasses() or [])
        for ds in arcpy.ListDatasets(feature_type="Feature") or []:
            names += [os.path.join(ds, n)
                      for n in arcpy.ListFeatureClasses(feature_dataset=ds) or []]
        names += list(arcpy.ListTables() or [])
    for n in names:
        base = os.path.basename(n)
        if layers and base not in layers:
            continue
        schema[base] = one(os.path.join(workspace_or_fc, n))
    return schema


_ESRI_TYPE = {
    "esriFieldTypeString": "String", "esriFieldTypeInteger": "Integer",
    "esriFieldTypeSmallInteger": "SmallInteger",
    "esriFieldTypeBigInteger": "BigInteger",
    "esriFieldTypeDouble": "Double", "esriFieldTypeSingle": "Single",
    "esriFieldTypeDate": "Date", "esriFieldTypeDateOnly": "DateOnly",
    "esriFieldTypeTimeOnly": "TimeOnly",
    "esriFieldTypeTimestampOffset": "TimestampOffset",
    "esriFieldTypeGUID": "Guid", "esriFieldTypeGlobalID": "GlobalID",
    "esriFieldTypeOID": "OID", "esriFieldTypeGeometry": "Geometry",
    "esriFieldTypeRaster": "Raster", "esriFieldTypeBlob": "Blob",
}


def schema_from_gdb_items(items, layers=None):
    """Build a schema from GDB_Items rows: an iterable of
    {"Name": ..., "Definition": <xml>} dicts."""
    schema = OrderedDict()
    for row in items:
        xml = row.get("Definition") or ""
        if "GPFieldInfoEx" not in xml:
            continue  # domains, feature datasets, the workspace
        name = row.get("Name")
        if layers and name not in layers:
            continue
        root = ET.fromstring(xml)
        out = []
        for f in root.iter("GPFieldInfoEx"):
            def g(tag, f=f):
                el = f.find(tag)
                return el.text if el is not None and el.text is not None else ""
            fld = Field(g("Name"), g("AliasName"),
                        _ESRI_TYPE.get(g("FieldType"), g("FieldType")),
                        None, g("IsNullable").lower() != "false",
                        g("DomainName"))
            if _keep(fld):
                out.append(fld)
        schema[name] = out
    return schema


_OGR_TYPE = {
    ("OFTString", "OFSTNone"): "String", ("OFTString", "OFSTUUID"): "Guid",
    ("OFTInteger", "OFSTNone"): "Integer",
    ("OFTInteger", "OFSTInt16"): "SmallInteger",
    ("OFTInteger64", "OFSTNone"): "BigInteger",
    ("OFTReal", "OFSTNone"): "Double", ("OFTReal", "OFSTFloat32"): "Single",
    ("OFTDateTime", "OFSTNone"): "Date", ("OFTDate", "OFSTNone"): "DateOnly",
    ("OFTTime", "OFSTNone"): "TimeOnly", ("OFTBinary", "OFSTNone"): "Blob",
}


def read_schema_gdb_items(gdb_path, layers=None):
    """Read field definitions from a file geodatabase without arcpy.

    Needs pyogrio (pip install pyogrio). The field list comes from each table
    itself. Aliases, the required flag, domains and Esri field types come
    from the geodatabase catalog (GDB_Items) where it describes the field.
    The catalog can miss fields that were added by other software, so it is
    never the only source. String lengths are not read; that column stays
    blank.
    """
    import pyogrio  # noqa
    from pyogrio.raw import read  # noqa
    meta, _fids, _geom, data = read(gdb_path, layer="GDB_Items",
                                    read_geometry=False,
                                    LIST_ALL_TABLES="YES")
    cols = list(meta["fields"])
    items = [{c: data[j][i] for j, c in enumerate(cols)}
             for i in range(len(data[0]))]
    catalog = schema_from_gdb_items(items, layers)

    schema = OrderedDict()
    for name, _gtype in pyogrio.list_layers(gdb_path):
        if layers and name not in layers:
            continue
        info = pyogrio.read_info(gdb_path, layer=name)
        known = {f.name.lower(): f for f in catalog.get(name, [])}
        out = []
        for fname, otype, osub in zip(info["fields"], info["ogr_types"],
                                      info["ogr_subtypes"]):
            f = known.get(fname.lower())
            if f is None:
                f = Field(fname, "", _OGR_TYPE.get((otype, osub), otype),
                          None, True, "")
            if _keep(f):
                out.append(f)
        schema[name] = out
    return schema


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def norm(name):
    """Loose form of a name: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def load_synonyms(path):
    """Read a synonyms CSV (layer, target_field, source_name[, origin]).

    Return {(layer, target_field_lower): [source_name, ...]} in file order.
    layer '*' applies to every layer.
    """
    syn = OrderedDict()
    if not path or not os.path.exists(path):
        return syn
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            key = ((r.get("layer") or "*").strip(),
                   (r.get("target_field") or "").strip().lower())
            src = (r.get("source_name") or "").strip()
            if key[1] and src:
                syn.setdefault(key, [])
                if src not in syn[key]:
                    syn[key].append(src)
    return syn


def suggest(layer, target, source_fields, synonyms=None, taken=()):
    """Suggest a source field for one target Field.

    source_fields is a list of source Field. taken holds source names that
    already have a strong match (exact or alias) elsewhere, so a weaker
    match does not reuse them. Return (source_name, method) or ("", "").
    """
    by_lower = {f.name.lower(): f for f in source_fields}
    tname = target.name.lower()

    # 1. Same name. The load step maps these on its own.
    if tname in by_lower:
        return by_lower[tname].name, EXACT

    # 2. The source alias is the target name or the target alias.
    wanted = {norm(target.name)}
    if target.alias:
        wanted.add(norm(target.alias))
    for f in source_fields:
        if f.alias and norm(f.alias) in wanted and f.name not in taken:
            return f.name, ALIAS

    # 3. A known synonym.
    syn = synonyms or {}
    for key in ((layer, tname), ("*", tname)):
        for cand in syn.get(key, []):
            f = by_lower.get(cand.lower())
            if f and f.name not in taken:
                return f.name, SYNONYM

    # 4. Loose name match (St_PreDir ~ stpredir).
    for f in source_fields:
        if norm(f.name) in wanted and f.name not in taken:
            return f.name, LOOSE
    return "", ""


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _row_from_field(layer, f):
    return OrderedDict([
        ("layer", layer), ("target_field", f.name), ("source_field", ""),
        ("method", ""), ("status", ""),
        ("required", "Y" if not f.nullable else ""),
        ("type", f.type or ""), ("length", "" if f.length is None else str(f.length)),
        ("domain", f.domain or ""), ("alias", f.alias or ""), ("notes", "")])


def make_rows(target_schema, sources=None, synonyms=None, notes=None,
              layers=None):
    """Make crosswalk rows for each target layer.

    sources: {target_layer: [source Field, ...]} for the layers that have a
    source. notes: {(layer, field): text} to put in the notes column.
    """
    sources = sources or {}
    notes = notes or {}
    rows = []
    for layer, tfields in target_schema.items():
        if layers and layer not in layers:
            continue
        src = sources.get(layer) or []
        # Strong matches (same name, alias) first. A weaker match may not
        # take a source field that a strong match already uses, and each
        # source field is suggested once.
        strong = OrderedDict()
        for tf in tfields:
            s, m = suggest(layer, tf, src, synonyms)
            if m in (EXACT, ALIAS) and s not in strong.values():
                strong[tf.name] = s
        used = set(strong.values())
        for tf in tfields:
            r = _row_from_field(layer, tf)
            if tf.name in strong:
                s = strong[tf.name]
                r["source_field"] = s
                r["method"] = EXACT if s.lower() == tf.name.lower() else ALIAS
            elif src:
                s, m = suggest(layer, tf, src, synonyms, taken=used)
                if s and m not in (EXACT, ALIAS):
                    r["source_field"], r["method"] = s, m
                    used.add(s)
            r["notes"] = notes.get((layer, tf.name), "")
            rows.append(r)
    return rows


def refresh_rows(old_rows, target_schema, sources=None, synonyms=None,
                 notes=None, layers=None):
    """Bring a crosswalk up to date with a (new) template.

    - A target field in both: keep source_field, method and notes. Mark
      CHANGED, with the differences in notes, if type, length, required or
      domain changed.
    - A target field only in the template: add it, marked NEW, with a
      suggestion if a source is given.
    - A row whose field is gone from the template: keep it at the end of its
      layer, marked REMOVED, so the choice is not lost. The load ignores it.
    A REMOVED and a NEW field in one layer with the same alias, or the same
    loose name, get a "possible rename" note.
    """
    old = OrderedDict(((r["layer"], r["target_field"]), r) for r in old_rows)
    fresh = make_rows(target_schema, sources, synonyms, notes, layers)
    out = []
    by_layer = OrderedDict()
    for r in fresh:
        by_layer.setdefault(r["layer"], []).append(r)
    old_layers = OrderedDict()
    for r in old_rows:
        old_layers.setdefault(r["layer"], []).append(r)
    for layer in list(by_layer) + [l for l in old_layers if l not in by_layer]:
        if layers and layer not in layers:
            continue
        new_rows = by_layer.get(layer, [])
        live = {r["target_field"] for r in new_rows}
        added, removed = [], []
        for r in new_rows:
            prev = old.get((layer, r["target_field"]))
            if prev is None:
                r["status"] = NEW
                added.append(r)
            else:
                diffs = []
                for col in ("type", "length", "required", "domain"):
                    a, b = (prev.get(col) or ""), (r.get(col) or "")
                    if col == "length" and (not a or not b):
                        continue  # one side read without lengths
                    if a != b:
                        diffs.append("{0} {1} -> {2}".format(
                            col, a or "(none)", b or "(none)"))
                r["source_field"] = prev.get("source_field", "")
                r["method"] = prev.get("method", "")
                r["notes"] = prev.get("notes", "") or r["notes"]
                if diffs:
                    r["status"] = CHANGED
                    r["notes"] = "; ".join(
                        ["template: " + ", ".join(diffs)]
                        + ([r["notes"]] if r["notes"] else []))
            out.append(r)
        for prev in old_layers.get(layer, []):
            if prev["target_field"] not in live:
                g = OrderedDict((c, prev.get(c, "")) for c in COLUMNS)
                g["status"] = REMOVED
                removed.append(g)
                out.append(g)
        # Rename hints.
        for g in removed:
            for a in added:
                same_alias = g["alias"] and norm(g["alias"]) == norm(a["alias"])
                same_name = norm(g["target_field"]) == norm(a["target_field"])
                if same_alias or same_name:
                    hint = "possible rename of {0}".format(g["target_field"])
                    a["notes"] = "; ".join(x for x in (a["notes"], hint) if x)
                    if g["source_field"] and not a["source_field"]:
                        a["source_field"] = g["source_field"]
                        a["method"] = MANUAL
    return out


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [OrderedDict((c, (r.get(c) or "").strip()) for c in COLUMNS)
                for r in csv.DictReader(fh)]
    return [r for r in rows if r["layer"] and r["target_field"]]


def split_sources(cell):
    return [s.strip() for s in (cell or "").split(SEP) if s.strip()]


# ---------------------------------------------------------------------------
# Use at load time
# ---------------------------------------------------------------------------

Lookup = namedtuple("Lookup", "candidates skip")


def to_lookup(rows):
    """Return {layer: Lookup(candidates={target: [source, ...]}, skip={...})}.

    REMOVED rows are ignored. A row with method 'skip' keeps its target
    field empty, even if the source has a field of the same name.
    """
    out = {}
    for r in rows:
        if r["status"] == REMOVED:
            continue
        lk = out.setdefault(r["layer"], Lookup({}, set()))
        if r["method"] == SKIP:
            lk.skip.add(r["target_field"])
            continue
        cands = split_sources(r["source_field"])
        if cands:
            lk.candidates[r["target_field"]] = cands
    return out


def resolve(target_fields, source_fields, lookup):
    """Decide the source field for each target field.

    Order: a name the crosswalk gives (first one present), then the same
    name in the source. Return ({target: source}, [unmapped targets]).
    """
    src = {f.lower(): f for f in source_fields}
    lk = lookup or Lookup({}, set())
    mapped, unmapped = {}, []
    for t in target_fields:
        if t in lk.skip:
            unmapped.append(t)
            continue
        chosen = None
        for cand in lk.candidates.get(t, []):
            if cand.lower() in src:
                chosen = src[cand.lower()]
                break
        if chosen is None:
            chosen = src.get(t.lower())
        if chosen is None:
            unmapped.append(t)
        else:
            mapped[t] = chosen
    return mapped, unmapped


def validate(rows, target_schema, sources=None, longest_value=None):
    """Check a crosswalk against the live template and the sources.

    sources: {target_layer: [source Field, ...]} for the layers being loaded.
    longest_value: optional function (target_layer, source_field) -> the
    length of the longest value in the source data, or None. When given, a
    source field that is declared longer than its target is a warning only if
    a real value does not fit.
    Return a list of (severity, layer, message). severity is ERROR (stop
    that layer), WARNING (load, but say so) or INFO (for the run log only).
    """
    sources = sources or {}
    issues = []
    seen = Counter((r["layer"], r["target_field"]) for r in rows)
    for (layer, field), n in seen.items():
        if n > 1:
            issues.append(("ERROR", layer, "Target field '{0}' has {1} rows. "
                           "Keep one.".format(field, n)))
    by_layer = OrderedDict()
    for r in rows:
        by_layer.setdefault(r["layer"], []).append(r)

    for layer, lrows in by_layer.items():
        if layer not in target_schema:
            if any(r["status"] != REMOVED for r in lrows):
                issues.append(("WARNING", layer, "The template has no layer "
                               "with this name. Its rows are not used."))
            continue
        live = OrderedDict((f.name, f) for f in target_schema[layer])
        listed = {r["target_field"] for r in lrows}
        missing = [n for n in live if n not in listed]
        if missing:
            issues.append(("WARNING", layer, "The template has fields that "
                           "the crosswalk does not list: {0}. Refresh the "
                           "crosswalk. Same-name source fields still "
                           "map.".format(", ".join(missing))))
        gone = [r["target_field"] for r in lrows
                if r["target_field"] not in live and r["status"] != REMOVED]
        if gone:
            issues.append(("WARNING", layer, "The crosswalk lists fields that "
                           "the template does not have: {0}. Refresh the "
                           "crosswalk.".format(", ".join(gone))))
        drift = []
        for r in lrows:
            f = live.get(r["target_field"])
            if f is None:
                continue
            if r["type"] and r["type"] != f.type:
                drift.append("{0} type {1} -> {2}".format(f.name, r["type"], f.type))
            if r["length"] and f.length is not None and r["length"] != str(f.length):
                drift.append("{0} length {1} -> {2}".format(f.name, r["length"], f.length))
            if r["required"] != ("" if f.nullable else "Y"):
                drift.append("{0} required {1} -> {2}".format(
                    f.name, r["required"] or "N", "N" if f.nullable else "Y"))
        if drift:
            issues.append(("WARNING", layer, "The template changed after the "
                           "crosswalk was made: {0}. Refresh the crosswalk and "
                           "check these rows.".format("; ".join(drift))))

        src = sources.get(layer)
        if src is None:
            continue
        sfields = {f.name.lower(): f for f in src}
        lk = to_lookup(lrows).get(layer, Lookup({}, set()))
        mapped, _ = resolve(list(live), [f.name for f in src], lk)
        absent = [(t, c) for t, c in lk.candidates.items()
                  if t in live and not any(x.lower() in sfields for x in c)]
        # A required field with no source is a warning. Other fields, and
        # fields that the notes say the ETL fills, are information only.
        notes = {r["target_field"]: r["notes"] for r in lrows}
        for sev, group in (
                ("WARNING", [a for a in absent if not live[a[0]].nullable
                             and not notes.get(a[0], "").startswith("ETL ")]),
                ("INFO", [a for a in absent if live[a[0]].nullable
                          or notes.get(a[0], "").startswith("ETL ")])):
            if group:
                issues.append((sev, layer, "No listed source field is in the "
                               "source layer for: {0}.".format(
                                   "; ".join("{0} ({1})".format(t, " | ".join(c))
                                             for t, c in group))))
        reused = [s for s, n in Counter(mapped.values()).items() if n > 1]
        if reused:
            issues.append(("WARNING", layer, "One source field feeds more "
                           "than one target field: {0}. Check that this is "
                           "intended.".format(", ".join(sorted(reused)))))
        for t, s in mapped.items():
            tf, sf = live[t], sfields[s.lower()]
            if (tf.type == "String" and sf.type == "String" and tf.length
                    and sf.length and sf.length > tf.length):
                actual = longest_value(layer, s) if longest_value else None
                if actual is None:
                    issues.append(("WARNING", layer, "{0} ({1} chars) is "
                                   "longer than {2} ({3} chars). Long values "
                                   "are cut off.".format(s, sf.length, t,
                                                         tf.length)))
                elif actual > tf.length:
                    issues.append(("WARNING", layer, "{0}: the longest value "
                                   "has {1} chars, but {2} holds {3}. Those "
                                   "values are cut off.".format(
                                       s, actual, t, tf.length)))
                else:
                    issues.append(("INFO", layer, "{0} is declared {1} chars "
                                   "and {2} holds {3}, but the longest value "
                                   "({4} chars) fits.".format(
                                       s, sf.length, t, tf.length, actual)))
            if tf.type == "String" or sf.type == "String":
                continue
            if _family(tf.type) != _family(sf.type):
                issues.append(("WARNING", layer, "{0} is {1} but {2} is {3}. "
                               "Values may not convert.".format(
                                   s, sf.type, t, tf.type)))
        if not mapped:
            issues.append(("ERROR", layer, "No source field matches this "
                           "layer. Is the right layer in this slot?"))
    return issues


def _family(t):
    if t in ("Integer", "SmallInteger", "BigInteger", "Double", "Single"):
        return "number"
    if t in ("Date", "DateOnly", "TimestampOffset", "TimeOnly"):
        return "date"
    return t


def summary(rows):
    """Count rows by layer and method, for a report line."""
    c = Counter()
    for r in rows:
        if r["status"] == REMOVED:
            continue
        c[(r["layer"], r["method"] or "blank")] += 1
    return c


def read_source(path):
    """Read the fields of ONE source feature class or table.

    path is a full path such as C:\\County.gdb\\Centerlines (a feature
    dataset in the path is fine). Uses arcpy when it is available, otherwise
    pyogrio. Return a list of Field.
    """
    try:
        import arcpy  # noqa: F401
        have_arcpy = True
    except ImportError:
        have_arcpy = False
    if have_arcpy:
        return list(read_schema_arcpy(path).values())[0]
    low = path.lower().replace("\\", "/")
    cut = low.find(".gdb/")
    if cut < 0:
        raise ValueError("Give a path inside a .gdb, for example "
                         "C:/County.gdb/Centerlines")
    gdb, name = path[:cut + 4], os.path.basename(path.replace("\\", "/"))
    schema = read_schema_gdb_items(gdb, [name])
    if not schema:
        raise ValueError("Layer {0} not found in {1}".format(name, gdb))
    return list(schema.values())[0]


def field_mappings(source_fc, target_fc, mapping):
    """Build an arcpy FieldMappings object from resolve()'s mapping.

    mapping is {target field: source field}. Each output field takes the
    target field's name, alias, type and length. ObjectID, GlobalID and
    shape fields are skipped. Use the result as the field_mapping argument
    of Export Features (to a staged copy). Needs ArcGIS Pro.
    """
    import arcpy
    tgt = {f.name: f for f in arcpy.ListFields(target_fc)}
    fms = arcpy.FieldMappings()
    for t_name, s_name in mapping.items():
        tf = tgt.get(t_name)
        if tf is None or not s_name or tf.type in ("OID", "Geometry", "GlobalID") \
                or tf.name.lower() in ("shape_length", "shape_area"):
            continue
        fm = arcpy.FieldMap()
        fm.addInputField(source_fc, s_name)
        of = fm.outputField
        of.name = tf.name
        of.aliasName = tf.aliasName
        of.type = tf.type
        if tf.type == "String":
            of.length = tf.length
        fm.outputField = of
        fms.addFieldMap(fm)
    return fms


USAGE = """Make or refresh a crosswalk CSV from the state template.

  python nena_crosswalk.py TEMPLATE.gdb OUT.csv [options]

Options:
  --old OLD.csv          refresh this crosswalk (keeps your choices)
  --synonyms FILE.csv    synonym list (synonyms.csv in the kit)
  --source LAYER=PATH    one of your layers for a template layer, for
                         suggestions. Repeat it for each layer, for example
                         --source RoadCenterLine=C:/County.gdb/Centerlines
  --layers A,B           only these template layers

Without ArcGIS Pro this needs pyogrio (pip install pyogrio).
In the ArcGIS Pro Python Command Prompt, arcpy is used instead.
"""


def main(argv):
    """Command line entry point. Return the number of rows written."""
    pos, opts, sources = [], {}, {}
    it = iter(argv)
    for a in it:
        if a in ("--old", "--synonyms", "--layers"):
            opts[a] = next(it, "")
        elif a == "--source":
            layer, _, path = next(it, "").partition("=")
            sources[layer.strip()] = read_source(path.strip())
        elif a in ("-h", "--help"):
            print(USAGE)
            return 0
        else:
            pos.append(a)
    if len(pos) < 2:
        print(USAGE)
        return 0
    try:
        import arcpy  # noqa: F401
        tgt = read_schema_arcpy(pos[0])
    except ImportError:
        tgt = read_schema_gdb_items(pos[0])
    layers = [x.strip() for x in opts["--layers"].split(",")] if opts.get("--layers") else None
    syn = load_synonyms(opts["--synonyms"]) if opts.get("--synonyms") else None
    old = read_csv(opts["--old"]) if opts.get("--old") else None
    if old:
        rows = refresh_rows(old, tgt, sources or None, syn, layers=layers)
    else:
        rows = make_rows(tgt, sources or None, syn, layers=layers)
    write_csv(rows, pos[1])
    print("Wrote {0} rows to {1}".format(len(rows), pos[1]))
    for (layer, method), n in sorted(summary(rows).items()):
        print("  {0:28s} {1:8s} {2}".format(layer, method, n))
    return len(rows)


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
