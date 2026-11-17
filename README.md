# NG911 Data Harvest kit

Reusable pieces of a process that loads county GIS data into the Indiana
NG911 Data Harvest state template. They work against the state template and
the NENA standard, so most of them apply to any Indiana county.

The kit website explains every function in plain language, with an
example. Start there if you are new to Python:

- https://demo-app.cultivategeospatial.com/ng911-harvest-kit/
- Mirror: https://blakecgs.github.io/ng911-data-harvest-kit/ (source: `docs/index.html`)

## Start here

| If you... | Use |
|---|---|
| Do not write code | The pre-flight checklist on the website, and `crosswalk_blank_all_layers.csv` (open it in Excel) |
| Use ArcGIS Pro and can paste Python | "Use the kit in ArcGIS Pro" below |
| Write Python | The docstrings in both .py files |

## Files

| File | What it is | Needs |
|---|---|---|
| `etl_snippets.py` | Value fixes, street names, IDs, municipality from A3, the required-field gate, geometry and topology checks | Python 3. Functions that use arcpy need ArcGIS Pro |
| `nena_crosswalk.py` | The field crosswalk: make a CSV from the template, suggest your fields, refresh it after a template change, check it, and build a field map | Python 3. ArcGIS Pro, or `pyogrio` without Pro |
| `crosswalk_blank_all_layers.csv` | A blank crosswalk for all 20 layers of the 2025 state template. Fill in `source_field` | Excel or any text editor |
| `synonyms.csv` | Field names that the suggestion step tries. Add your own | — |

## Use the kit in ArcGIS Pro

You do not need to install anything. ArcGIS Pro has Python and arcpy.

1. Unzip the kit to a folder, for example `C:\NG911Kit`.
2. Make a copy of your data to work on.
3. In Pro, open the Python window (Analysis tab > Python > Python Window),
   or a notebook (Insert tab > New Notebook).
4. Type these lines:

```python
import sys
sys.path.append(r"C:\NG911Kit")
import etl_snippets as ets
import nena_crosswalk as xw
```

5. Use the functions, for example:

```python
ets.build_nguid("RCL", 1042, "co.example.in.us")
ets.update_each(r"C:\NG911\Work.gdb\RoadCenterLine",
                ["St_Name", "St_PreTyp", "St_PreMod"], ets.move_state_road)
```

In the Field Calculator (Python), put the first three lines in the code
block and call a function in the expression, for example
`ets.clean_ws(!St_Name!)`.

If you change a .py file, restart ArcGIS Pro so that it reads the new file.

## Make a crosswalk with suggestions (command line)

Open the ArcGIS Pro Python Command Prompt (Windows Start menu > ArcGIS).
Without Pro, use any Python 3 with `pip install pyogrio`.

```
cd C:\NG911Kit
python nena_crosswalk.py C:\NG911\DataHarvest_Template.gdb my_crosswalk.csv ^
  --synonyms synonyms.csv ^
  --source RoadCenterLine=C:\NG911\County.gdb\Centerlines ^
  --source SiteStructureAddressPoint=C:\NG911\County.gdb\AddressPoints
```

After the state publishes a new template, add `--old my_crosswalk.csv`.
Rows are marked NEW, CHANGED or REMOVED, and your choices are kept.

## Notes

- Test every function on a copy of your data first.
- `apply_legacy_name` assumes the legacy (MSAG/ALI) street name is the GIS name.
- `split_multipart_parcels` can raise your VEP total if the CAMA rule checks every parcel. Measure before you keep it.
- Example values in the functions are made up. The results on the website come from Monroe County, Indiana.

## License

MIT. See `LICENSE`.

Blake Shanayda, Cultivate Geospatial Solutions (CGS)
