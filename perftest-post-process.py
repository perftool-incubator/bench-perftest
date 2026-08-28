#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

import sys
import os
import re
import json
from pathlib import Path

TOOLBOX_HOME = os.environ.get('TOOLBOX_HOME')
if TOOLBOX_HOME is None:
    print("This script requires libraries that are provided by the toolbox project.")
    print("Toolbox can be acquired from https://github.com/perftool-incubator/toolbox and")
    print("then use 'export TOOLBOX_HOME=/path/to/toolbox' so that it can be located.")
    exit(1)
else:
    p = Path(TOOLBOX_HOME) / 'python'
    if not p.exists() or not p.is_dir():
        print("ERROR: <TOOLBOX_HOME>/python ('%s') does not exist!" % (p))
        exit(2)
    sys.path.append(str(p))
from toolbox.cdm_metrics import CDMMetrics

# perftest's client and server independently report bandwidth/message
# rate for the *same* transfer -- logging both as CDM metrics under
# the same desc would double-count them, since CDM's default
# aggregation sums same-type metrics across engine-role (confirmed
# live: a real run reported ~50.58 Gbps, ~2x the actual 25.29 Gbps,
# until this was fixed). So only the client's result file feeds CDM
# metrics; the server's captured output is still archived as a raw
# output file (see benchmark-metadata.json) but intentionally not
# indexed.
RESULT_FILE = "perftest-client-result.txt"
REPORT_INTERVAL_FILE = "perftest-report-interval.txt"

# perftest-client pipes perftest's output through perftest-timestamp-
# lines before capturing it, prefixing each line with its own
# wall-clock arrival time -- perftest's own report lines carry no
# timestamp, and its internal print-thread sleep() can drift under CPU
# contention from the main polling loop (confirmed live: only 23 of an
# expected ~30 reports arrived in a clean 30-second window at
# report-interval=1), so a fixed report-interval can't be trusted to
# derive accurate per-sample timestamps after the fact. Column layout
# after the timestamp verified against src/perftest_parameters.h's
# REPORT_FMT/REPORT_FMT_EXT macros: "#bytes #iterations BW_peak
# BW_average MsgRate".
REPORT_LINE_RE = re.compile(
    r"^(\d+\.\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$"
)

# (crucible metric type, REPORT_LINE_RE capture-group index) -- group 1
# is the arrival timestamp prepended by perftest-timestamp-lines;
# column order after it verified against perftest's REPORT_FMT/
# REPORT_FMT_EXT macros: #bytes(2) #iterations(3) BW_peak(4)
# BW_average(5) MsgRate(6). BW_peak is deliberately excluded here:
# --run_infinitely forces perftest's own peak tracking off (it prints
# "WARNING: BW peak won't be measured in this run" and the column is
# always 0.00 in this mode, confirmed live), so bw-peak-Gbps is instead
# derived below as the max of the per-interval bw-avg-Gbps samples.
METRICS = [
    ("bw-avg-Gbps", 5),
    ("msgrate-Mmsg-sec", 6),
]
BW_AVG_GROUP = 5


def find_result_file():
    if os.path.exists(RESULT_FILE):
        return RESULT_FILE
    return None


def main():
    iter_sample = {
        'rickshaw-bench-metric': {'schema': {'version': '2021.04.12'}},
        'benchmark': 'perftest',
        'primary-period': 'measurement',
        'primary-metric': 'bw-avg-Gbps',
        'periods': [],
    }
    period = {'name': 'measurement', 'metric-files': []}

    result_file = find_result_file()
    if result_file is None:
        # This role produced no results for this sample -- report an
        # empty period rather than failing the whole iteration.
        iter_sample['periods'].append(period)
        os.makedirs('postprocess', exist_ok=True)
        with open('postprocess/post-process-data.json', 'w') as f:
            json.dump(iter_sample, f)
        return 0

    report_interval_ms = int(open(REPORT_INTERVAL_FILE).readline().rstrip()) * 1000

    with open(result_file) as f:
        rows = [m for m in (REPORT_LINE_RE.match(line) for line in f) if m]

    metrics = CDMMetrics()
    logged = False
    # Every sample's "end" is its own real arrival timestamp, and every
    # "begin" but the first is the previous sample's real "end" -- no
    # assumed spacing. The first sample has no previous real timestamp
    # to anchor to (perftest-start.txt is written right after the
    # detection loop in perftest-client confirms the first line has
    # already arrived, so it can land *after* that line's own
    # timestamp due to polling latency -- using it here produced a
    # begin > end sample on real hardware), so its "begin" alone is
    # estimated as one report-interval before its own "end".
    first_begin_ms = None
    prev_ts_ms = None
    for row in rows:
        sample_end = int(float(row.group(1)) * 1000)
        sample_begin = prev_ts_ms if prev_ts_ms is not None else sample_end - report_interval_ms
        if first_begin_ms is None:
            first_begin_ms = sample_begin
        prev_ts_ms = sample_end
        for metric_type, group in METRICS:
            desc = {'source': 'perftest', 'class': 'throughput', 'type': metric_type}
            sample = {'begin': sample_begin, 'end': sample_end, 'value': float(row.group(group))}
            metrics.log_sample("0", desc, {}, sample)
            logged = True

    if rows:
        peak_desc = {'source': 'perftest', 'class': 'throughput', 'type': 'bw-peak-Gbps'}
        peak_sample = {
            'begin': first_begin_ms,
            'end': int(float(rows[-1].group(1)) * 1000),
            'value': max(float(row.group(BW_AVG_GROUP)) for row in rows),
        }
        metrics.log_sample("0", peak_desc, {}, peak_sample)
        logged = True

    if logged:
        metric_data_name = metrics.finish_samples(dont_delete=True)
        period['metric-files'].append(metric_data_name)

    iter_sample['periods'].append(period)
    os.makedirs('postprocess', exist_ok=True)
    with open('postprocess/post-process-data.json', 'w') as f:
        json.dump(iter_sample, f)
    return 0


if __name__ == "__main__":
    exit(main())
