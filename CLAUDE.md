# Bench-perftest

## Purpose
Scripts and configuration to run `perftest` (ib_write_bw/ib_read_bw/ib_send_bw from [linux-rdma/perftest](https://github.com/linux-rdma/perftest)) within the crucible framework. Measures raw RDMA bandwidth and message rate, independent of the kernel TCP stack -- used to establish a PCIe/NIC hardware baseline (e.g. before attributing a TCP throughput shortfall to the kernel network stack).

## Language
- Bash for benchmark execution scripts
- Python for post-processing (`perftest-post-process.py`)

## Key Files
| File | Purpose |
|------|---------|
| `rickshaw.json` | Rickshaw integration: client/server scripts |
| `benchmark-metadata.json` | Machine-readable description and CDM-indexed source/type list (consumed by `crucible benchmarks list`) |
| `perftest-base` | Shared functions: device/netdev resolution, NUMA lookup, GID-index resolution, cpu-pin resolution |
| `perftest-client` / `perftest-server-start` / `perftest-server-stop` | Benchmark execution |
| `perftest-post-process.py` | Parses perftest's periodic `--run_infinitely` stdout reports into crucible metrics |
| `workshop.json` | Engine image build requirements (`perftest`, `rdma-core` distro packages) |

## One benchmark, one `--verb` param
`verb` (`write`/`read`/`send`, default `write`) selects which binary
(`ib_write_bw`/`ib_read_bw`/`ib_send_bw`) the client/server scripts
exec, rather than modeling these as three separate benchmarks --
mirrors `bench-uperf`'s multiple-test-types-in-one-script pattern. All
three binaries share essentially identical CLI/output shape.

## Device selection
Exactly one of `--device`/`--ifname` must resolve to a device:
- `--device=<name>` is used directly.
- `--ifname=<netdev>` is resolved to an RDMA device by scanning
  `/sys/class/infiniband/*/device/net/<ifname>` (`resolve_perftest_device`
  in `perftest-base`).

This differs from `bench-iperf`'s netdev-to-IP resolution (which
`bench-perftest` also needs, for the server's control-channel IP) --
here there's an *additional* netdev-to-RDMA-device mapping layer,
since perftest addresses hardware via `-d <ib_dev>`, not an IP.

## Live periodic logging via `--run_infinitely`, not a one-shot result
Both client and server pass `--run_infinitely` (`-D <report-interval>`
becomes the interval between reports, not the total test length) so
perftest prints a fresh BW/msgrate report every `report-interval`
seconds for the whole run, matching how `bench-iperf`/`bench-uperf`
produce periodic samples rather than a single final number. Because of
this:
- Neither side exits on its own anymore. `perftest-client` backgrounds
  the process, polls its captured output for the first real periodic
  report line (see "Duration means measured duration" below), then
  sleeps for `duration` seconds before killing it (SIGTERM, SIGKILL
  fallback -- rc 143/137 are the expected success path, not a
  failure). The server goes back to the standard crucible daemon
  lifecycle -- background + PID file at start, SIGTERM/SIGKILL at stop
  (`perftest-server-start`/`perftest-server-stop`), same as
  `bench-iperf`. There is no one-shot-server special case anymore.
- perftest registers no `SIGTERM` handler and its stdout is fully
  buffered whenever it isn't a tty (verified against upstream source
  -- no `setvbuf`/`signal(SIGTERM,...)` calls anywhere), so every
  periodic report since the last libc flush would be silently lost the
  instant the process gets killed. Both scripts run the binary under
  `stdbuf --output=L` to force line-buffering so each report line is
  durable regardless of how/when the process is terminated. Don't
  drop this thinking it's unnecessary -- it's the fix for a real,
  verified data-loss bug, not defensive boilerplate.
- perftest's report lines carry no timestamp of their own (`#bytes
  #iterations BW_peak BW_average MsgRate`) -- `perftest-client` writes
  the resolved `report-interval` to `perftest-report-interval.txt`,
  and post-process derives each sample's `begin`/`end` from
  `perftest-start.txt`'s wall-clock anchor plus the sample's ordinal
  position in the stream times that interval.
- **Duration means measured duration, not wall-clock-since-launch.**
  QP/GID negotiation over the TCP control channel happens before
  perftest's periodic print thread starts counting report-interval,
  and that setup time is unpredictable (observed ~7-8s on real
  ConnectX-6 hardware, confirmed live). Originally `perftest-client`
  wrapped the whole invocation in `timeout <duration>s`, which meant
  setup time ate into the requested `duration` -- a 30s request
  produced only ~22s/22 samples of real measurement. Fixed by having
  the client poll the captured output for the first line matching the
  periodic report format before starting the `duration`-second
  countdown (and before writing `perftest-start.txt`, which
  post-process anchors every sample's timestamp to) -- so `duration`
  now means actual measurement time, and per-sample timestamps aren't
  systematically shifted by however long setup happened to take.
- **Known restriction**: `verb=send` + `bidirectional=true` fails
  under `--run_infinitely` (confirmed in source:
  `has_recv_comp(verb)` is true only for `SEND`/`SEND_IMM`/
  `WRITE_IMM`, and perftest itself refuses to start a duplex test for
  those verbs in this mode). perftest exits(1) with a clear message
  before the test starts, which surfaces as-is through this script's
  `exit_error`, so no extra validation was added here -- `write`/`read`
  + `bidirectional=true`, and `send` without `bidirectional`, are
  unaffected.

## GID index (RoCEv2)
`gid-index` (default `auto`) controls perftest's `-x` flag:
- `auto`: scan `/sys/class/infiniband/<dev>/ports/<port>/gid_attrs/types/`
  for a `RoCE v2` entry and use that index; if none is found (pure
  InfiniBand fabric), omit `-x` entirely.
- `none`: never pass `-x`.
- a specific number: passed through as-is.

## CPU/NUMA affinity
perftest has **no native NUMA/core-affinity flags at all** -- verified
directly against the installed binary's own `--help` output (perftest
25.10.0.0.128; there is no `--numa_node`/`--pin_cores` or any other
affinity-related option). Earlier design notes assumed these flags
existed without checking `--help` first, and were wrong; the feature
was silently broken until fixed (never caught earlier because every
live test up to that point used the default `cpu-pin=none`).

`cpu-pin` is instead implemented with an external `taskset --cpu-list`
wrapper around the whole perftest invocation -- the same mechanism
`bench-iperf` uses for the same reason (iperf3 also has no native
affinity flags). `numa` wraps with the NIC's NUMA node's full CPU list
(via `perftest-base`'s existing `resolve_numa_node_for_netdev`); `cpu`
wraps with the explicit `cpu-pin-list` value. Both are pool-only
pinning (confines the whole process to a CPU range, lets the kernel
scheduler place it within that range) -- there's no per-thread
affinity, matching `bench-iperf`'s documented stance that this is an
accepted limitation of tools without native per-thread pinning, not a
bug to work around.

## Metrics
Parsed as a periodic time series from perftest's captured
`--run_infinitely` stdout (fixed-column format verified against
`src/perftest_parameters.h`'s `REPORT_FMT`/`REPORT_FMT_EXT` macros,
not assumed -- one `log_sample()` call per report line, not one final
value):
- `bw-avg-Gbps` (primary) <- `BW_average` column, one sample per
  report interval
- `msgrate-Mmsg-sec` <- `MsgRate` column, one sample per report
  interval
- `bw-peak-Gbps` <- **not** perftest's own `BW_peak` column: verified
  live that `--run_infinitely` forces perftest's peak tracking off
  (it prints "WARNING: BW peak won't be measured in this run" and
  `BW_peak` is always `0.00` in this mode). Instead computed in
  post-process as the max of the `bw-avg-Gbps` samples across the run
  -- one value for the whole test, not a time series.

Only the client's captured output (`perftest-client-result.txt`) feeds
these metrics -- see `perftest-post-process.py`'s `RESULT_FILE`
comment for why the server's own report of the same transfer is
archived but intentionally not indexed (double-counting, confirmed
live on real hardware).

Uses `toolbox.cdm_metrics.CDMMetrics` (the current, non-deprecated
metrics API -- see `toolbox/CLAUDE.md`), not the older
`toolbox.metrics` free-function module.

## Hardware access
Requires `/dev/infiniband/*` exposed into the engine container, via a
run file's `podman-settings.device`/`host-mounts`, or `osruntime:
chroot`. No crucible/rickshaw code changes are needed for this --
engines already run `podman --privileged`, which covers `IPC_LOCK`
(needed for RDMA memory registration). See README.md for a sample run
file.

## Conventions
- Primary branch is `main`
- Standard Bash modelines and 4-space indentation
- Python code follows 4-space indentation with standard modelines
