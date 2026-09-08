# AppFlowy-CI

Cloud integration tests use `https://localhost`. The workflow generates a short-lived
localhost certificate, installs its CA in the runner's system trust store, and
configures the server and Flutter client to use HTTPS/WSS. Public Form routes
reject plaintext HTTP, so changing the cloud URL back to HTTP breaks submission
tests. Keep certificate verification enabled when reproducing this setup.

Run the workflow regression checks with Python 3, PyYAML, and OpenSSL installed:

```sh
python3 -B -m unittest discover -s .github/scripts
```

The Cloud test-matrix coverage gate recognizes standard Rust, Tokio, and SQLx
test attributes. New test-bearing modules must be assigned to an integration or
commercial matrix entry, including modules containing only `#[sqlx::test]` cases.
Run the coverage-checker regressions with Python 3:

```sh
python3 -B -m unittest discover -s scripts -p 'test_check_test_module_coverage.py'
```

Each Cloud root integration lane ends with `script/test_final_account_deletion.sh` from
the checked-out Cloud source. It explicitly runs an ignored API test that deletes the shared
`eva@appflowy.io` snapshot account and verifies private-space recovery. Each matrix job has its
own restored database; keep this step after all ordinary tests. Package-only lanes and the
commercial reset suite do not use this final fixture check. Older Cloud refs without the runner
remain supported.
