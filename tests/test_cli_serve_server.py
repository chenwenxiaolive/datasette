import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx2
import pytest

from .conftest import stop_process


@pytest.mark.serial
def test_serve_localhost_http(ds_localhost_http_server):
    _, port = ds_localhost_http_server
    response = httpx2.get(f"http://localhost:{port}/_memory.json")
    assert {
        "database": "_memory",
        "path": "/_memory",
        "tables": [],
    }.items() <= response.json().items()


@pytest.mark.serial
@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="Requires socket.AF_UNIX support"
)
def test_serve_unix_domain_socket(ds_unix_domain_socket_server):
    _, uds = ds_unix_domain_socket_server
    transport = httpx2.HTTPTransport(uds=uds)
    with httpx2.Client(transport=transport) as client:
        response = client.get("http://localhost/_memory.json")
    assert {
        "database": "_memory",
        "path": "/_memory",
        "tables": [],
    }.items() <= response.json().items()


def _can_bind_ipv6_loopback():
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def _resolves_to_ipv6_only(hostname):
    try:
        families = {info[0] for info in socket.getaddrinfo(hostname, 0)}
    except socket.gaierror:
        return False
    return families == {socket.AF_INET6}


ipv6_loopback = pytest.mark.skipif(
    not _can_bind_ipv6_loopback(), reason="Requires an IPv6 loopback address"
)
ipv6_only_hostname = pytest.mark.skipif(
    not (_can_bind_ipv6_loopback() and _resolves_to_ipv6_only("ip6-localhost")),
    reason="Requires ip6-localhost to resolve to IPv6 only",
)
shell_script_browser = pytest.mark.skipif(
    sys.platform == "win32", reason="Uses a shell script as the browser"
)


@contextlib.contextmanager
def serve_on_port_0(*args, env=None):
    ds_proc = subprocess.Popen(
        [sys.executable, "-m", "datasette", "--memory", "-p", "0", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=tempfile.gettempdir(),
        env=env,
    )
    try:
        for line in ds_proc.stdout:
            match = re.search(rb"Uvicorn running on \S+:(\d+) ", line)
            if match:
                break
        yield ds_proc, int(match.group(1))
    finally:
        stop_process(ds_proc)


@pytest.mark.serial
@pytest.mark.parametrize(
    "host_args",
    [[], pytest.param(["-h", "ip6-localhost"], marks=ipv6_only_hostname)],
)
def test_serve_port_0_root_url_signs_in(host_args):
    ds_proc = subprocess.Popen(
        [sys.executable, "-m", "datasette", "--memory", "-p", "0", "--root"]
        + host_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=tempfile.gettempdir(),
    )
    try:
        for line in ds_proc.stdout:
            if b"/-/auth-token?token=" in line:
                break
        response = httpx2.get(line.decode("utf-8").strip())
    finally:
        stop_process(ds_proc)
    assert response.status_code == 302
    assert "ds_actor" in response.cookies


@pytest.mark.serial
@ipv6_loopback
def test_serve_port_0_ipv6_wildcard_refuses_ipv4():
    with serve_on_port_0("-h", "::") as (_, port):
        response = httpx2.get(f"http://[::1]:{port}/-/versions.json")
        assert response.status_code == 200
        with pytest.raises(httpx2.ConnectError):
            httpx2.get(f"http://127.0.0.1:{port}/-/versions.json")


# webbrowser waits for a program named by BROWSER, as it does for a console browser
WAITING_BROWSER = """#!/bin/sh
echo "$1" > "{opened}"
while [ ! -e "{closed}" ]; do sleep 0.1; done
"""


@pytest.mark.serial
@shell_script_browser
def test_serve_port_0_serves_while_browser_is_open(tmp_path):
    opened, closed = tmp_path / "opened", tmp_path / "closed"
    browser = tmp_path / "browser"
    browser.write_text(WAITING_BROWSER.format(opened=opened, closed=closed), "utf-8")
    browser.chmod(0o755)
    try:
        with serve_on_port_0("-o", env={**os.environ, "BROWSER": str(browser)}) as (
            ds_proc,
            port,
        ):
            response = httpx2.get(
                f"http://127.0.0.1:{port}/-/versions.json", timeout=5.0
            )
            assert response.status_code == 200
            deadline = time.time() + 5.0
            while not opened.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert opened.read_text("utf-8").startswith(f"http://127.0.0.1:{port}/")
            ds_proc.terminate()
            ds_proc.wait(timeout=3)
    finally:
        closed.touch()


PRINTING_ASGI_WRAPPER_PLUGIN = """
from datasette import hookimpl


@hookimpl
def asgi_wrapper(datasette):
    print("asgi_wrapper called", flush=True)
    return lambda app: app
"""


@pytest.mark.serial
def test_serve_fixed_port_root_url_is_first_output_line(serve_with_plugins):
    ds_proc, port = serve_with_plugins(
        {"printing_asgi_wrapper_plugin": PRINTING_ASGI_WRAPPER_PLUGIN}, "--root"
    )
    first_line = ds_proc.stdout.readline().decode("utf-8")
    assert first_line.startswith(f"http://127.0.0.1:{port}/-/auth-token?token=")


# Shaped after datasette-litestream's startup hook, which schedules a
# background task with asyncio.get_running_loop().create_task(...):
# https://github.com/datasette/datasette-litestream
MARKER_TASK_PLUGIN = """
import asyncio
from datasette import hookimpl
from datasette.utils.asgi import Response


@hookimpl
def startup(datasette):
    datasette._startup_calls = getattr(datasette, "_startup_calls", 0) + 1

    async def _mark():
        # Must await before setting the flag: a task with no internal
        # await point could finish on the throwaway loop before it
        # closed, masking the regression this test guards against.
        await asyncio.sleep(0.2)
        datasette._marker_task_ran = True

    asyncio.get_running_loop().create_task(_mark())


@hookimpl
def register_routes():
    async def marker_status(datasette):
        return Response.json(
            {
                "marker_task_ran": getattr(datasette, "_marker_task_ran", False),
                "startup_calls": getattr(datasette, "_startup_calls", 0),
            }
        )

    return [(r"^/-/marker-task-ran$", marker_status)]
"""


STARTUP_ERROR_PLUGIN = """
from datasette import hookimpl
from datasette.utils import StartupError


@hookimpl
def startup(datasette):
    raise StartupError("boom from plugin")
"""


@pytest.mark.serial
def test_startup_hook_background_task_runs_on_serving_loop(serve_with_plugins):
    """
    Litestream-shaped regression test: a startup hook that does
    asyncio.get_running_loop().create_task(...) must have that task
    actually execute before/while the server is handling requests. This
    only holds if invoke_startup() and uvicorn.Server.serve() share one
    event loop. This test fails against unmodified main, where
    invoke_startup() runs on a throwaway loop that is closed before
    uvicorn opens its own loop to serve.
    """
    _, port = serve_with_plugins({"marker_task_plugin": MARKER_TASK_PLUGIN})
    # The fixture has already waited for the server to answer requests. The
    # marker task deliberately awaits before setting its flag, so poll for a
    # moment rather than assuming it landed before the first request arrived.
    deadline = time.time() + 3.0
    payload = {}
    while time.time() < deadline:
        payload = httpx2.get(
            f"http://127.0.0.1:{port}/-/marker-task-ran", timeout=1.0
        ).json()
        if payload["marker_task_ran"]:
            break
        time.sleep(0.05)
    assert payload.get("marker_task_ran"), (
        "The startup hook's asyncio.create_task(...) never ran - "
        "invoke_startup() and the server are not sharing an event loop"
    )
    # Polling above means this test would also pass if the startup hook were
    # re-run on the serving loop by the first-request fallback - which would
    # hide exactly the bug being tested. invoke_startup() is idempotent today
    # so that cannot happen; assert it explicitly so that if the idempotency
    # guard is ever removed this test fails loudly instead of silently
    # becoming a no-op.
    assert payload["startup_calls"] == 1, (
        "startup hook ran {} times - the marker may have been set by a "
        "re-run on the serving loop rather than by the original task".format(
            payload["startup_calls"]
        )
    )


@pytest.mark.serial
def test_startup_error_fails_fast_before_port_binds(serve_with_plugins):
    """
    A "startup" plugin hook that raises StartupError must fail fast: print
    the message, exit non-zero, and never accept a connection on the port -
    the failure must happen before uvicorn.Server binds the socket.
    """
    proc, port = serve_with_plugins(
        {"startup_error_plugin": STARTUP_ERROR_PLUGIN}, wait_for_startup=False
    )
    stdout, _ = proc.communicate(timeout=15)
    output = stdout.decode("utf-8")
    assert proc.returncode not in (0, None), output
    assert "boom from plugin" in output, output

    # Nothing is listening on the port now the process has exited. This
    # confirms the socket was not left bound; on its own it cannot prove the
    # failure preceded the bind, since a port nothing ever touched also
    # refuses connections.
    with (
        pytest.raises(OSError),
        socket.create_connection(("127.0.0.1", port), timeout=0.2),
    ):
        pass


# Verify that SIGTERM and SIGINT sent to `datasette serve` trigger uvicorn's
# lifespan.shutdown event and run the plugin shutdown hooks. The plugin below
# writes a sentinel file from its shutdown hook so the tests can check that
# cleanup ran after the server subprocess exits.
SHUTDOWN_SENTINEL_PLUGIN_TEMPLATE = """
import pathlib
from datasette import hookimpl

SENTINEL_PATH = {sentinel_path!r}


@hookimpl
def shutdown(datasette):
    pathlib.Path(SENTINEL_PATH).write_text("shutdown ran", "utf-8")
"""


def _start_serve_with_shutdown_sentinel(
    serve_with_plugins, tmp_path, *extra_args, **kwargs
):
    sentinel_path = tmp_path / "shutdown-sentinel.txt"
    proc, _ = serve_with_plugins(
        {
            "shutdown_sentinel_plugin": SHUTDOWN_SENTINEL_PLUGIN_TEMPLATE.format(
                sentinel_path=str(sentinel_path)
            )
        },
        *extra_args,
        **kwargs,
    )
    return proc, sentinel_path


# Reserved for documentation by RFC 5737, so no machine has it to bind
UNBINDABLE_HOST = "192.0.2.1"


@pytest.mark.serial
def test_serve_port_0_bind_failure_runs_shutdown_hooks(serve_with_plugins, tmp_path):
    ds_proc, sentinel_path = _start_serve_with_shutdown_sentinel(
        serve_with_plugins,
        tmp_path,
        "-h",
        UNBINDABLE_HOST,
        "-p",
        "0",
        wait_for_startup=False,
    )
    stdout, _ = ds_proc.communicate(timeout=15)
    output = stdout.decode("utf-8")
    assert ds_proc.returncode not in (0, None), output
    assert sentinel_path.exists(), output


@pytest.mark.serial
def test_sigterm_runs_shutdown_hooks(serve_with_plugins, tmp_path):
    ds_proc, sentinel_path = _start_serve_with_shutdown_sentinel(
        serve_with_plugins, tmp_path
    )
    assert not sentinel_path.exists()
    ds_proc.send_signal(signal.SIGTERM)
    try:
        ds_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        ds_proc.kill()
        ds_proc.wait()
        raise AssertionError(
            "datasette serve did not exit within 10s of SIGTERM\n"
            + ds_proc.stdout.read().decode("utf-8")
        )
    output = ds_proc.stdout.read().decode("utf-8")
    assert sentinel_path.exists(), (
        "shutdown hook never wrote its sentinel file after SIGTERM\n" + output
    )
    assert sentinel_path.read_text("utf-8") == "shutdown ran"


@pytest.mark.serial
@pytest.mark.skipif(
    not hasattr(signal, "SIGINT"), reason="Requires signal.SIGINT support"
)
def test_sigint_runs_shutdown_hooks(serve_with_plugins, tmp_path):
    ds_proc, sentinel_path = _start_serve_with_shutdown_sentinel(
        serve_with_plugins, tmp_path
    )
    assert not sentinel_path.exists()
    ds_proc.send_signal(signal.SIGINT)
    try:
        ds_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        ds_proc.kill()
        ds_proc.wait()
        raise AssertionError(
            "datasette serve did not exit within 10s of SIGINT\n"
            + ds_proc.stdout.read().decode("utf-8")
        )
    output = ds_proc.stdout.read().decode("utf-8")
    assert sentinel_path.exists(), (
        "shutdown hook never wrote its sentinel file after SIGINT\n" + output
    )
    assert sentinel_path.read_text("utf-8") == "shutdown ran"
