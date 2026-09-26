#!/bin/bash
R={{RESULT_ROOT}}
end() { sync; reboot -f; echo 1 > /proc/sys/kernel/sysrq; echo b > /proc/sysrq-trigger; }
if [ -f "$R/done" ]; then end; fi
mkdir -p $R /logs/agent /logs/verifier /logs/artifacts
{{PAYLOAD_DIRS}}{{WIRE_PRELUDE}}{{PHASE_LINES}}
{{FOLD}}touch $R/done
end
