#!/bin/bash
mkdir -p $R/{{PHASE}}
touch $R/{{PHASE}}/stdout $R/{{PHASE}}/stderr
{{BUDGET}}bash {{RUNNER_DIR}}/phases/{{PHASE}}.sh
[ -f $R/{{PHASE}}/rc ] || echo 124 > $R/{{PHASE}}/rc
