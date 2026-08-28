# bench-perftest

perftest (ib_write_bw/ib_read_bw/ib_send_bw) RDMA throughput benchmark for the [crucible](https://github.com/perftool-incubator/crucible) performance testing framework.

Wraps [linux-rdma/perftest](https://github.com/linux-rdma/perftest) to
measure raw PCIe/RDMA throughput and message rate, independent of the
kernel TCP stack. Useful for establishing a PCIe/NIC hardware baseline
before attributing a TCP throughput shortfall to the kernel network
stack (e.g. comparing against `bench-uperf`/`bench-iperf` results on
the same hosts/interfaces).

## Hardware prerequisite

This benchmark needs RDMA verbs device access
(`/dev/infiniband/*`) inside the engine. crucible engines already run
with `podman --privileged` (which covers `IPC_LOCK`, needed for RDMA
memory registration), but the RDMA device nodes still need to be
explicitly exposed. In a `remotehosts` run file, either:

- set `podman-settings.device` to the relevant `/dev/infiniband/*`
  paths (e.g. `/dev/infiniband/uverbs0`, `/dev/infiniband/rdma_cm`), or
- use `"osruntime": "chroot"` for direct hardware access with no
  container isolation layer, the same approach `cyclictest`/`oslat`/
  `trafficgen` use.

## Device selection

Exactly one of `--device` or `--ifname` must be set (both default to
`"none"`):

- `--device=<name>` -- the RDMA device name directly (e.g. `mlx5_2`),
  as reported by `ibv_devices`.
- `--ifname=<netdev>` -- a netdev name (e.g. `ens6f0`); the RDMA
  device is resolved via `/sys/class/infiniband/*/device/net/<ifname>`.

## Parameters

| Param | Default | Notes |
|---|---|---|
| `verb` | `write` | `write`, `read`, or `send` -- selects `ib_write_bw`/`ib_read_bw`/`ib_send_bw` |
| `device` / `ifname` | `none` / `none` | exactly one required, see above |
| `duration` | `5` | length of the actual measurement window in seconds -- timed from when perftest's periodic reporting begins (after RDMA connection setup), not from process launch, since `--run_infinitely` (see Metrics below) never stops on its own |
| `report-interval` | `1` | seconds between periodic BW/msgrate reports during the run, passed to `-D` alongside `--run_infinitely` |
| `size` | `65536` | message size in bytes, passed to `-s` |
| `qp` | `1` | number of queue pairs, passed to `-q` |
| `bidirectional` | `false` | `true`/`false`, passed to `-b` |
| `connection` | `RC` | `RC`/`UC`/`UD`/`SRD`, passed to `-c` |
| `gid-index` | `auto` | `auto` detects a RoCEv2 GID index via sysfs, `none` omits `-x` entirely (pure InfiniBand fabrics), or an explicit number |
| `cpu-pin` | `none` | perftest has no native CPU/NUMA-affinity flags, so this wraps the whole invocation in `taskset --cpu-list` instead: `numa` uses the NIC's NUMA node's CPU list, `cpu` uses an explicit core list from `cpu-pin-list`, `none` applies no pinning |
| `cpu-pin-list` | `none` | CPU list for `cpu-pin=cpu`, e.g. `0-3,8` |
| `rdma-cm` | `false` | `true` adds `-R` (use RDMA Connection Manager instead of the default out-of-band socket exchange) |

## Sample run file

```json
{
    "benchmarks": [
        {
            "name": "perftest",
            "ids": "1",
            "mv-params": {
                "global-options": [
                    {
                        "name": "common-params",
                        "params": [
                            { "arg": "verb", "vals": ["write"], "role": "all" },
                            { "arg": "ifname", "vals": ["ens6f0"], "role": "all" },
                            { "arg": "duration", "vals": ["10"], "role": "all" }
                        ]
                    }
                ],
                "sets": [
                    { "include": ["common-params"], "params": [] }
                ]
            }
        }
    ],
    "tags": {},
    "tool-params": [],
    "endpoints": [
        {
            "type": "remotehosts",
            "settings": { "osruntime": "podman" },
            "remotes": [
                {
                    "engines": [
                        { "role": "client", "ids": "1" },
                        { "role": "server", "ids": "1" }
                    ],
                    "config": {
                        "host": "testhost.example.com",
                        "settings": {
                            "cpu-partitioning": false,
                            "podman-settings": {
                                "device": ["/dev/infiniband/uverbs0", "/dev/infiniband/rdma_cm"]
                            }
                        }
                    }
                }
            ]
        }
    ]
}
```

## Metrics

Rather than running for a fixed duration and reporting one final
number, this benchmark runs perftest with `--run_infinitely`, which
reprints a full BW/msgrate report every `report-interval` seconds for
as long as the test runs -- the same live-logging approach crucible's
other throughput benchmarks use (e.g. iperf3's `-i`, uperf's `-a`).
Each periodic report becomes one timestamped sample:

- `bw-avg-Gbps` (primary) -- bandwidth average over the preceding
  `report-interval`, from perftest's `BW_average` column (Gb/sec via
  `--report_gbits`)
- `msgrate-Mmsg-sec` -- message rate in millions/sec over the
  preceding interval, from `MsgRate`
- `bw-peak-Gbps` -- the max of the `bw-avg-Gbps` samples across the
  whole run (one value, not a time series). perftest's own peak
  tracking is disabled by `--run_infinitely` (it always reports `0.00`
  in this mode), so this is computed in post-process instead of read
  from perftest's `BW_peak` column.

`duration` controls the length of the actual measurement window, timed
from when perftest's periodic reporting begins rather than from
process launch (RDMA connection setup happens first and takes a
variable amount of time); `report-interval` controls sampling cadence.
Only the client's captured output feeds these metrics -- the server
reports the same transfer from its own side and would double-count if
both were indexed.
