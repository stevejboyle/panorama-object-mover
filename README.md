# Panorama Object Mover

Move PAN-OS objects between Panorama scopes (device group ↔ device group, shared ↔ device group, and shared ↔ shared).  
Every operation is logged to a timestamped CSV including a compact summary **and** the full `<entry>` XML so you can restore later.

## Features
- ✅ Supports **address**, **address-group**, **service**, **service-group**
- ✅ Handles **shared** and **device group** scopes
- ✅ **Collision policy**: skip (default) or overwrite via env var
- ✅ **Timestamped CSV log** with summary + full XML for restore
- ✅ Clear restore path using logged XML
- ⚠️ No automatic commit (commit separately when satisfied)

## Repo Layout
```
panorama-object-mover/
├─ move-objects-between-dg.py
├─ requirements.txt
├─ .gitignore
├─ LICENSE
├─ README.md
└─ samples/
   ├─ objects.csv
   └─ panw.cfg.example
```

## Prerequisites
- Python 3.8+
- `requests` (see `requirements.txt`)

## Setup
```bash
git clone <your-repo-url>.git
cd panorama-object-mover
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
cp samples/panw.cfg.example ./panw.cfg               # then edit with your values
```

## Configuration (`panw.cfg`)
Key/value pairs, one per line:
```
panorama_ip=192.168.1.10
api_key=YOUR_API_KEY
```
> Keep `panw.cfg` out of Git (already in `.gitignore`).

## Input CSV (`objects.csv`)
Header row:
```
object_name,object_type,src_scope,dst_scope
```
Where `src_scope` and `dst_scope` are either a device group name or the literal `shared`.

Example (`samples/objects.csv`):
```
object_name,object_type,src_scope,dst_scope
test-addr-1,address,Branch1,shared
test-svc-1,service,shared,Branch2
vip-db,address,DC-Core,Branch2
web-ports,service-group,shared,shared
prod-tags,address-group,Branch1,DC-Core
```

## Usage
```bash
# Default: reads ./panw.cfg and ./objects.csv
python3 move-objects-between-dg.py

# Specify a different CSV input
python3 move-objects-between-dg.py my-objects.csv

# Overwrite collisions at destination (instead of default 'skip')
MOVE_COLLISION=overwrite python3 move-objects-between-dg.py
```

- The script writes a log like `moves_YYYYmmdd_HHMMSS.csv` in the working directory.
- **Commit is not automatic.** Commit changes in Panorama when you're ready.

## Restore from Log
Each row contains the full `<entry ...>...</entry>` XML. You can restore by `set`-ing it at the destination container XPath:
- Shared: `/config/shared/<type>`
- Device-group: `/config/devices/entry[@name='localhost.localdomain']/device-group/entry[@name='<DG>']/<type>`

## Notes & Caveats
- Moving **address-groups** or **service-groups** can break references if member objects are not present in the destination scope (or `shared`). The script warns—migrate dependencies first.
- This script does **not** validate policy references or perform a commit.
- Test on a lab Panorama first.

---

**Author:** Steve Boyle - PANW (NOT OFFICIALLY SUPPORTED) 
MIT License
