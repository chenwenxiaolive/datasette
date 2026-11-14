---
name: verify-datasette
description: "Runs Datasette from a node checkout for proof: builds the environment, serves the test fixtures database, drives the table page, the JSON API, SQL, CSV export and the write API, and captures the result. Use when a change to Datasette needs an acceptance run, or a review has to launch it."
---

# Verify Datasette

Datasette is a web server over SQLite files with a CLI in front. Nothing below
needs a GPU, a model or another service, so a CPU-only verifier can make every
run.

Run the commands from the node checkout, in one bash shell: a new shell has
lost `RUN`, `PORT` and the helper functions, so over ssh they go in one script,
`ssh sh-gate 'cd -- <checkout> && bash -s' < <script>`. They are written for
`sh-gate`, a shared machine, which is why the server takes a free port.

## Environment

Once per checkout:

```bash
uv sync
```

It builds `.venv` with Datasette installed from the checkout in editable mode.
The repository has no lock file: the first sync in a checkout writes a `uv.lock`
that Git ignores, so a checkout keeps the versions of the day it was built.

## Launch

```bash
export CUDA_VISIBLE_DEVICES= DATASETTE_SECRET=not_a_secret
RUN=$(mktemp -d /volume/tmp/datasette-verify-$(date +%Y%m%d)-XXXXXX)
git rev-parse HEAD > $RUN/commit.txt
.venv/bin/python -m pip freeze > $RUN/versions.txt
.venv/bin/python tests/fixtures.py $RUN/fixtures.db
.venv/bin/datasette serve $RUN/fixtures.db -p 0 -s permissions.create-table.id editor -s permissions.insert-row.id editor > $RUN/serve.out 2>&1 & echo $! > $RUN/serve.pid
for i in $(seq 100); do grep -q 'Uvicorn running on' $RUN/serve.out && break; sleep 0.1; done
PORT=$(grep -o 'Uvicorn running on http://127.0.0.1:[0-9]*' $RUN/serve.out | grep -o '[0-9]*$')
get() { curl -sS --noproxy '*' -o $RUN/$1 -w "$1 http %{http_code}\n" "http://127.0.0.1:$PORT$2" | tee -a $RUN/status.txt; }
```

- `tests/fixtures.py` writes the database the test suite uses, so the values under Drive are the suite's.
- The server is ready when `$RUN/serve.out` holds `Uvicorn running on http://127.0.0.1:<port>`. With `-p 0` that line is the only place the port is named.
- `DATASETTE_SECRET` is the Justfile's test value. It signs the token the write feature uses, and the two `-s` settings let the actor `editor` create a table and insert rows.

## Check

```bash
git status --short --branch; git rev-parse HEAD; cat $RUN/commit.txt; .venv/bin/python -c "import datasette; print(datasette.__file__)"; grep 'Started server process' $RUN/serve.out; cat $RUN/serve.pid; curl -sS --noproxy '*' http://127.0.0.1:$PORT/-/versions.json | head -c 60; echo
```

Worth driving when the status lists no file, both commits are the one under
test, `datasette` is imported from the checkout, the started process is the PID
in `serve.pid`, and the last line begins `{"ok": true`. A server started before
the checkout moved still runs the old code: its `commit.txt` then differs.

## Drive

```bash
get table.html /fixtures/facetable
get facet.json '/fixtures/facetable.json?_facet=state&_size=2'
get sql.json '/fixtures/-/query.json?sql=select+41+%2B+1+as+answer'
get table.csv '/fixtures/facetable.csv?_size=2'
create() { curl -sS --noproxy '*' -o $RUN/$1 -w "$1 http %{http_code}\n" -X POST "${@:2}" -H 'Content-Type: application/json' -d '{"table": "verify_run", "rows": [{"id": 1, "name": "written by the verify run"}], "pk": "id"}' http://127.0.0.1:$PORT/fixtures/-/create | tee -a $RUN/status.txt; }
create denied.json
create create.json -H "Authorization: Bearer $(.venv/bin/datasette create-token editor)"
get stored.json '/fixtures/verify_run.json?_shape=array'
```

| Feature | Line in `$RUN/status.txt` | End state |
|---|---|---|
| Table page | `table.html http 200` | `grep -o '<title>[^<]*</title>' $RUN/table.html` prints `<title>fixtures: facetable: 15 rows</title>` |
| JSON API with a facet and paging | `facet.json http 200` | `jq -c '[.ok, .next, [.rows[].pk], [.facet_results.results.state.results[].count]]' $RUN/facet.json` prints `[true,"2",[1,2],[10,4,1]]`: two rows, a next page, and the facet's counts for CA, MI and MC |
| SQL query | `sql.json http 200` | `$RUN/sql.json` is `{"ok": true, "rows": [{"answer": 42}], "truncated": false}` |
| CSV export | `table.csv http 200` | `$RUN/table.csv` has three lines: a header beginning `pk,created,planet_int`, then rows 1 and 2 |
| Write API | `denied.json http 403` without a token, `create.json http 201` with one, then `stored.json http 200` | `$RUN/stored.json` is `[{"id": 1, "name": "written by the verify run"}]`: the row was stored and read back |

One request needs no server: `.venv/bin/datasette $RUN/fixtures.db --get '/fixtures/-/query.json?sql=select+41+%2B+1+as+answer'`
prints the SQL feature's JSON and exits 0.

The test suite is the project's gate rather than part of this run:
`.venv/bin/python -m pytest -p no:cacheprovider -n 16 -q`. The Justfile's
`-n auto` would start a worker for each of sh-gate's 224 cores. It took 98 s and
ended `3072 passed, 188 skipped, 6 xfailed, 15 xpassed` at be0ce3e8 (2026-11-14).

## Evidence

`$RUN` holds `commit.txt`, `versions.txt`, `fixtures.db`, `serve.out`,
`serve.pid`, `status.txt` and the output file of each request. Keep the directory and name
it where the result is reported.

## Cleanup

```bash
kill $(cat $RUN/serve.pid); wait $(cat $RUN/serve.pid); tail -n 1 $RUN/serve.out
for p in /proc/[0-9]*; do [ "$(readlink $p/cwd 2>/dev/null)" = "$(pwd -P)" ] && [ ${p#/proc/} != $$ ] && ps -o pid=,cmd= -p ${p#/proc/}; done
```

The log ends with `Finished server process [<pid>]`, and the loop prints nothing
when no process is left running from the checkout. The environment stays.
