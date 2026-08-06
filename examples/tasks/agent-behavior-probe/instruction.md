Do the following steps in order, then report only what you actually observed
when you did them (not what you expect should happen).

1. Create the directory `/app/probe/data`.
2. Write a file at `/app/probe/data/alpha.txt` containing exactly this line:

   ```
   alpha-9f3c2d
   ```

3. Write a file at `/app/probe/data/nested/beta.txt` containing exactly this
   line:

   ```
   beta-77a1e4
   ```

4. Try to open a TCP connection to `example.com` on port 80, with a timeout
   of about 5 seconds.
5. Try to resolve the DNS name `github.com`.
6. Write a file at `/app/probe/report.txt` containing exactly these four
   lines, in this order, each ending in either `ok` or `failed`:

   ```
   alpha: <ok|failed>
   beta: <ok|failed>
   tcp_example_com_80: <ok|failed>
   dns_github_com: <ok|failed>
   ```

   Use `ok` for the `alpha`/`beta` lines only if you actually created that
   exact file with that exact content. Use `ok` for the `tcp_example_com_80`
   line only if the connection attempt in step 4 actually succeeded, and
   `ok` for `dns_github_com` only if the resolution attempt in step 5
   actually succeeded. Otherwise use `failed`.
