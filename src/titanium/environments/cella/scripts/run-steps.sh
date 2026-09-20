#!/bin/bash
R={{RESULT_ROOT}}
cd {{CWD}} || cd /
{{STEP_BLOCKS}}
echo 0 > $R/{{PHASE}}/rc
