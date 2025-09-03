#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
move-objects-between-dg.py

Move PAN-OS objects between Panorama scopes (device-group <-> device-group, shared <-> device-group).

Inputs
------
- panw.cfg: key=value lines with:
    panorama_ip=1.2.3.4
    api_key=YOUR_API_KEY
- objects.csv: columns:
    object_name,object_type,src_scope,dst_scope
  where scope is either a device group name or the literal "shared"

Behavior
--------
- Copies the XML <entry> from src, adds it to dst, then deletes from src.
- Logs each move to moves_YYYYmmdd_HHMMSS.csv with fields:
    timestamp,object_name,object_type,src_scope,dst_scope,status,message,summary,xml
- On name collision at destination, it will SKIP (configurable).

Supported object types
----------------------
address, address-group, service, service-group

Notes
-----
- Moving address-groups (especially dynamic or with members) across scopes can break references
  if member objects are not available in the destination scope. The script warns but proceeds.
- This script does not run a commit. Commit separately once you’re satisfied.
"""

import csv
import datetime as dt
import xml.etree.ElementTree as ET
import requests
import os
import sys
from typing import Optional

requests.packages.urllib3.disable_warnings()

SUPPORTED_TYPES = {
    "address": "address",
    "address-group": "address-group",
    "service": "service",
    "service-group": "service-group",
}

# --- Config / IO --------------------------------------------------------------

def read_config(cfg_file="panw.cfg"):
    cfg = {}
    with open(cfg_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):  # allow comments/blank lines
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("panorama_ip", "api_key"):
        if k not in cfg:
            raise ValueError(f"Missing '{k}' in {cfg_file}")
    return cfg

def open_logger():
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"moves_{ts}.csv"
    f = open(fname, "w", newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(["timestamp","object_name","object_type","src_scope","dst_scope","status","message","summary","xml"])
    return f, w, fname

# --- Panorama API helpers -----------------------------------------------------

def api_get_config(pan_ip, api_key, xpath):
    url = f"https://{pan_ip}/api/"
    params = {"type": "config", "action": "get", "key": api_key, "xpath": xpath}
    r = requests.get(url, params=params, verify=False, timeout=60)
    r.raise_for_status()
    return r.text

def api_set_config(pan_ip, api_key, xpath, element_xml):
    url = f"https://{pan_ip}/api/"
    params = {"type": "config", "action": "set", "key": api_key, "xpath": xpath, "element": element_xml}
    r = requests.post(url, params=params, verify=False, timeout=60)
    r.raise_for_status()
    return r.text

def api_delete_config(pan_ip, api_key, xpath):
    url = f"https://{pan_ip}/api/"
    params = {"type": "config", "action": "delete", "key": api_key, "xpath": xpath}
    r = requests.post(url, params=params, verify=False, timeout=60)
    r.raise_for_status()
    return r.text

# --- XPaths for scopes --------------------------------------------------------

def container_xpath_for_scope(scope: str, obj_type: str) -> str:
    """
    Returns the container (no entry) xpath where entries of this type live for the given scope.
    - scope == "shared" -> /config/shared/<type>
    - else device-group -> /config/devices/.../device-group/entry[@name='<scope>']/<type>
    """
    node = SUPPORTED_TYPES[obj_type]
    if scope.lower() == "shared":
        return f"/config/shared/{node}"
    return ("/config/devices/entry[@name='localhost.localdomain']"
            f"/device-group/entry[@name='{scope}']/{node}")

def entry_xpath_for_scope(scope: str, obj_type: str, name: str) -> str:
    """Full xpath to the specific <entry name='...'> for the given scope."""
    return container_xpath_for_scope(scope, obj_type) + f"/entry[@name='{name}']"

# --- Parsing helpers ----------------------------------------------------------

def extract_entry_xml(api_xml: str) -> Optional[ET.Element]:
    """
    Given an API <response> XML string from 'get', return the <entry ...> element (or None).
    """
    try:
        root = ET.fromstring(api_xml)
    except ET.ParseError:
        return None
    entry = root.find(".//entry")
    return entry

def serialize_entry(elem: ET.Element) -> str:
    return ET.tostring(elem, encoding="unicode")

def make_summary(obj_type: str, entry_elem: ET.Element) -> str:
    """
    Create a short human-friendly summary for logging (IP, members, ports, etc.).
    """
    if obj_type == "address":
        ip = entry_elem.findtext("./ip-netmask") or entry_elem.findtext("./ip-range") or entry_elem.findtext("./fqdn") or ""
        desc = entry_elem.findtext("./description") or ""
        return f"address: {ip} | {desc}".strip()
    if obj_type == "address-group":
        dynamic = entry_elem.findtext("./dynamic/filter")
        if dynamic:
            return f"addr-group(dynamic): filter='{dynamic[:80]}'"
        members = [m.text for m in entry_elem.findall("./static/member") if m.text]
        return f"addr-group(static): members={len(members)}"
    if obj_type == "service":
        proto = entry_elem.findtext("./protocol/tcp/port") or entry_elem.findtext("./protocol/udp/port") or ""
        desc = entry_elem.findtext("./description") or ""
        return f"service: {proto} | {desc}".strip()
    if obj_type == "service-group":
        members = [m.text for m in entry_elem.findall("./members/member") if m.text]
        return f"service-group: members={len(members)}"
    return ""

def detect_addr_group_reference_risk(obj_type: str, entry_elem: Optional[ET.Element]) -> bool:
    if obj_type not in ("address-group", "service-group") or entry_elem is None:
        return False
    return True

def object_exists_in_scope(pan_ip, api_key, scope: str, obj_type: str, name: str) -> bool:
    dst_entry = entry_xpath_for_scope(scope, obj_type, name)
    xml = api_get_config(pan_ip, api_key, dst_entry)
    elem = extract_entry_xml(xml)
    return elem is not None

# --- Core move ----------------------------------------------------------------

def move_one(pan_ip, api_key, obj_name, obj_type, src_scope, dst_scope, logger, collision_policy="skip"):
    """
    collision_policy: 'skip' or 'overwrite' (set+delete anyway). Default 'skip'.
    """
    now = dt.datetime.now().isoformat(timespec="seconds")
    obj_type = obj_type.strip().lower()

    if obj_type not in SUPPORTED_TYPES:
        msg = f"Unsupported type '{obj_type}'. Supported: {', '.join(SUPPORTED_TYPES)}"
        logger.writerow([now, obj_name, obj_type, src_scope, dst_scope, "error", msg, "", ""])
        print(f"[!] {msg}")
        return

    # 1) Read source entry
    src_entry_xpath = entry_xpath_for_scope(src_scope, obj_type, obj_name)
    try:
        src_xml = api_get_config(pan_ip, api_key, src_entry_xpath)
    except Exception as e:
        msg = f"API get failed from src: {e}"
        logger.writerow([now, obj_name, obj_type, src_scope, dst_scope, "error", msg, "", ""])
        print(f"[!] {msg}")
        return

    entry_elem = extract_entry_xml(src_xml)
    if entry_elem is None:
        msg = f"Object not found in source scope '{src_scope}'."
        logger.writerow([now, obj_name, obj_type, src_scope, dst_scope, "error", msg, "", src_xml])
        print(f"[!] {msg}")
        return

    # 2) Collision check at destination
    if object_exists_in_scope(pan_ip, api_key, dst_scope, obj_type, obj_name):
        if collision_policy == "skip":
            msg = "Destination already has object with same name. Skipping."
            summary = make_summary(obj_type, entry_elem)
            logger.writerow([now, obj_name, obj_type, src_scope, dst_scope, "skipped", msg, summary, serialize_entry(entry_elem)])
            print(f"[-] {msg} ({obj_name})")
            return
        elif collision_policy == "overwrite":
            # delete destination then continue
            dst_entry_xpath = entry_xpath_for_scope(dst_scope, obj_type, obj_name)
            try:
                api_delete_config(pan_ip, api_key, dst_entry_xpath)
                print(f"[i] Deleted existing '{obj_name}' in dst to overwrite.")
            except Exception as e:
                msg = f"Failed to delete existing dst object before overwrite: {e}"
                summary = make_summary(obj_type, entry_elem)
                logger.writerow([now, obj_name, obj_type, src_scope, dst_scope, "error", msg, summary, serialize_entry(entry_elem)])
                print(f"[!] {msg}")
                return

    # 3) Add to destination (set at container xpath with full <entry>)
    dst_container_xpath = container_xpath_for_scope(dst_scope, obj_type)
    entry_xml_str = serialize_entry(entry_elem)

    # Advisory note for groups
    if detect_addr_group_reference_risk(obj_type, entry_elem):
        print(f"[!] Warning: moving group '{obj_name}' may break member references if they don’t exist in '{dst_scope}'.")

    try:
        api_set_config(pan_ip, api_key, dst_container_xpath, entry_xml_str)
        print(f"[+] Added '{obj_name}' to '{dst_scope}'.")
    except Exception as e:
        msg = f"Failed to add to destination: {e}"
        summary = make_summary(obj_type, entry_elem)
        logger.writerow([now, obj_name, obj_type, src_scope, dst_scope, "error", msg, summary, entry_xml_str])
        print(f"[!] {msg}")
        return

    # 4) Delete from source
    try:
        api_delete_config(pan_ip, api_key, src_entry_xpath)
        print(f"[+] Removed '{obj_name}' from '{src_scope}'.")
        status = "moved"
        message = "ok"
    except Exception as e:
        # If delete fails, we’ve effectively copied not moved. Log as 'copied'.
        status = "copied"
        message = f"Added to dst but failed to delete from src: {e}"
        print(f"[!] {message}")

    # 5) Log
    summary = make_summary(obj_type, entry_elem)
    logger.writerow([now, obj_name, obj_type, src_scope, dst_scope, status, message, summary, entry_xml_str])

def main():
    cfg = read_config("panw.cfg")
    pan_ip = cfg["panorama_ip"]
    api_key = cfg["api_key"]

    # Optional behavior tweak via env var:
    collision_policy = os.getenv("MOVE_COLLISION", "skip").lower().strip()  # 'skip' or 'overwrite'

    log_f, log_w, log_name = open_logger()
    print(f"[i] Logging to {log_name}")

    # Expected CSV header: object_name,object_type,src_scope,dst_scope
    input_csv = sys.argv[1] if len(sys.argv) > 1 else "objects.csv"
    count = 0
    with open(input_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            obj_name = row["object_name"].strip()
            obj_type = row["object_type"].strip()
            src_scope = row["src_scope"].strip()
            dst_scope = row["dst_scope"].strip()
            move_one(pan_ip, api_key, obj_name, obj_type, src_scope, dst_scope, log_w, collision_policy=collision_policy)
            count += 1

    log_f.close()
    print(f"[i] Processed {count} row(s).")
    print("[i] NOTE: This script does not commit changes. Commit in Panorama when ready.")

if __name__ == "__main__":
    main()
