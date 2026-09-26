#!/bin/bash
(
{{EXPORTS}}runuser -u {{USER}} -- bash {{STEP_PATH}}
) >> $R/{{PHASE}}/stdout 2>> $R/{{PHASE}}/stderr
rc=$?
if [ $rc -ne 0 ]; then
  echo $rc > $R/{{PHASE}}/rc
  exit $rc
fi
