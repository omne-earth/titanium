Keep a ledger, one command per entry. Follow these rules exactly:

1. Run twelve commands, one at a time, each as its own separate command
   and nothing else. Command number N (for N from 1 to 12) is exactly:

       echo step-N >> /app/ledger.txt

   with N replaced by the number, so the first command is
   `echo step-1 >> /app/ledger.txt` and the twelfth is
   `echo step-12 >> /app/ledger.txt`. Never combine two entries in one
   command, never use a loop, and never skip or reorder a number.
2. Only after all twelve entries, write the exact text `done` to
   `/app/results.txt` as a thirteenth separate command.
