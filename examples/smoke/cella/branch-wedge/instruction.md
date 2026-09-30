Do these three things, in order, as separate commands:

1. As your very first command, run exactly `date +%s%N | tee /app/token.txt`
   and nothing else. Do not combine it with any other command.
2. Then run the command `sleep 180` to completion. Do not background the
   sleep, do not shorten it, and do not write the result file early: the
   sleep standing in for a long computation is the point of the task.
3. Only after the sleep finishes, write the exact text `done` to
   `/app/results.txt`.
