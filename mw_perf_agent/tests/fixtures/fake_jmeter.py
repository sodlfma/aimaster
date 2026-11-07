#!/usr/bin/env python3
"""A stand-in for `jmeter` used by tests and offline demos.

Understands the subset of CLI flags the runner emits (-n -t -q -l -j -e -o -J -v),
reads step*.threads / step*.duration from user.properties (and -J overrides),
prints Summariser lines and writes a synthetic CSV JTL + report/statistics.json.
Set FAKE_JMETER_FAST=1 to skip the real-time sleeps."""
import json
import os
import random
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_args(argv):
    args = {"J": {}}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-t", "-q", "-l", "-j", "-o"):
            args[a] = argv[i + 1]
            i += 2
        elif a.startswith("-J"):
            k, _, v = a[2:].partition("=")
            args["J"][k] = v
            i += 1
        else:
            i += 1
    return args


def main(argv):
    if "-v" in argv:
        print("    _    ____   _    ____ _   _ _____       _ __  __ _____ _____ _____ ____\n Copyright (c) 1999-2024 The Apache Software Foundation\n 5.6.3")
        return 0
    args = parse_args(argv)
    props = {}
    if args.get("-q") and Path(args["-q"]).exists():
        for line in Path(args["-q"]).read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                props[k.strip()] = v.strip()
    props.update(args["J"])

    # Step names from the JMX ThreadGroups
    names = []
    if args.get("-t") and Path(args["-t"]).exists():
        root = ET.parse(args["-t"]).getroot()
        names = [tg.get("testname") for tg in root.iter("ThreadGroup")]
    steps = []
    for i in range(1, 10):
        if f"step{i}.threads" not in props:
            break
        steps.append({
            "name": names[i - 1] if i - 1 < len(names) else f"Step {i}",
            "threads": int(float(props[f"step{i}.threads"])),
            "duration": int(float(props.get(f"step{i}.duration", 5))),
        })
    if not steps:
        steps = [{"name": "Step 1", "threads": 10, "duration": 5}]

    fast = os.environ.get("FAKE_JMETER_FAST", "1") == "1"
    jtl = Path(args.get("-l", "result.jtl"))
    jtl.parent.mkdir(parents=True, exist_ok=True)
    if args.get("-j"):
        Path(args["-j"]).write_text("INFO o.a.j.JMeter: Running test\n")
    random.seed(42)
    now = int(time.time() * 1000)
    t = now
    rows = ["timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect"]
    stats = {}
    total = 0
    for si, s in enumerate(steps):
        base_lat = 50 * (1 + si * 1.5)
        per_sec = max(1, s["threads"] // 2)
        for sec in range(max(1, s["duration"])):
            errs = 0
            for k in range(per_sec):
                lat = int(random.gauss(base_lat, base_lat * 0.2)) + (400 if (si == 2 and sec % 7 == 3) else 0)
                lat = max(1, lat)
                code, ok, msg = "200", "true", "OK"
                if si == 3 and random.random() < 0.15:
                    code, ok, msg = "503", "false", "Service Unavailable"
                    errs += 1
                elif si == 1 and random.random() < 0.03:
                    code, ok, msg = "500", "false", "Cannot get a connection, pool error Timeout waiting for idle object"
                    errs += 1
                tn = f"{s['name']} {si+1}-{(k % s['threads'])+1}"
                rows.append(f'{t},{lat},GET /api/v1/test,{code},"{msg}",{tn},text,{ok},,512,120,{s["threads"]},{s["threads"]},http://x/api/v1/test,{lat-5},0,2')
                total += 1
            t += 1000
            if sec % 5 == 4 or sec == s["duration"] - 1:
                print(f"summary +   {per_sec*5:5d} in 00:00:05 = {per_sec:6.1f}/s Avg: {int(base_lat):5d} Min: {int(base_lat*0.5):5d} Max: {int(base_lat*2):5d} Err: {errs:5d} ({(errs/max(1,per_sec))*100:.2f}%) Active: {s['threads']} Started: {s['threads']} Finished: 0", flush=True)
            if not fast:
                time.sleep(1)
        stats[s["name"]] = {"transaction": s["name"], "sampleCount": per_sec * s["duration"], "errorCount": 0, "errorPct": 0.0,
                            "meanResTime": base_lat, "pct1ResTime": base_lat * 1.3, "pct2ResTime": base_lat * 1.5,
                            "pct3ResTime": base_lat * 2, "throughput": per_sec}
    jtl.write_text("\n".join(rows) + "\n")
    if args.get("-o"):
        out = Path(args["-o"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "statistics.json").write_text(json.dumps(stats))
        (out / "index.html").write_text("<html><body>fake dashboard</body></html>")
    print(f"summary = {total:7d} in 00:00:30 =   50.0/s Avg:   120 Min:     4 Max:  2100 Err:    12 (0.21%)")
    print("Tidying up ...    @ " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("... end of run")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
