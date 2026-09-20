# Environment probe report

Write `/app/report.json` with exactly these keys, each from what
the machine shows:

- `egress_tcp_denied`: whether TCP to `1.1.1.1:443` (short timeout)
  failed to connect
- `task_type`: the contents of `/titanium/task-type`, stripped
- `net_interfaces`: sorted entries of `/sys/class/net`
- `pid1_comm`: the contents of `/proc/1/comm`, stripped
- `uid`: the numeric user id the probe ran as
- `writable_workdir` / `writable_tmp`: whether create-and-delete
  works in `/app` and `/tmp`
- `cpu_hypervisor`: whether `/proc/cpuinfo` lists `hypervisor`
- `nproc`: the CPU count the guest observes
- `mem_total_kb`: `MemTotal` in kB, as the guest reports it
- `fs_total_kb`: the root filesystem's size in kB via statvfs
- `root_device_is_vda`: whether `/proc/cmdline` names `root=/dev/vda`
- `kernel_release`: `uname -r`, stripped

The report is valid JSON even when a probe fails.
