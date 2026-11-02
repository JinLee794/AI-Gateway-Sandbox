"""
Hyperlight executor (EXPERIMENTAL, Code mode scenario): runs the model's script in a Hyperlight micro-VM instead of a subprocess.

Runs on a small Linux VM with KVM (nested virtualization) in the sandbox VNet, reachable only from the Container Apps
subnet (infra/hyperlight.bicep). The sandbox UI stays the host of every tool call: when the script calls a stub, the
guest calls the host tool "gw", this service parks the call on the job, and the UI long-polls /jobs/<id>/next, makes the
real MCP tools/call through API Management with the caller's key, and posts the result back to /jobs/<id>/reply.
So the APIM key never enters the VM and the micro-VM has no network at all.

One worker thread owns the sandbox (Hyperlight sandboxes are bound to the thread that made them). It builds the micro-VM
once, snapshots it after a warm-up run, and restores that snapshot before every job, so each script starts from a
clean VM. A job with "fresh": true builds a new micro-VM instead (the cold start).

  GET  /health                 {"ok", "kvm", "mshv", "coldMs", "busy", "error"}
  POST /run                    {code, functions, generic, data, maxOutput, fresh?} -> {"id"}
  GET  /jobs/<id>/next         long poll: {"call", "args"} | {"done", "output", "error", "startMs", "runMs", "fresh"} | {"wait": true}
  POST /jobs/<id>/reply        {"result": ...} | {"fatal": "..."}

Spike limits: one job at a time, and a script that never ends keeps the worker busy (the guest has no time limit yet).

Standard library plus hyperlight-sandbox (wasm backend + Python guest).
"""
import json, os, queue, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
CALL_TIMEOUT_S = 90      # how long a guest waits for the UI to answer one tool call
JOB_TTL_S = 300
POLL_S = 20

# Runs in the guest before the script: the stubs the model was given, each one a call to the host tool "gw".
PREAMBLE = r'''
import json as _j
def _gw(tool, args):
    r = _j.loads(call_tool("gw", payload=_j.dumps({"call": tool, "args": args})))
    if "fatal" in r:
        raise RuntimeError(r["fatal"])
    return r["result"]
def _setup(functions, generic, data):
    g = globals()
    def make(tool, params):
        def stub(*args, **kwargs):
            a = dict(zip(params, args))
            a.update(kwargs)
            return _gw(tool, a)
        return stub
    for fn, (tool, params) in functions.items():
        g[fn] = make(tool, params)
    if generic:
        def call(op, args=None, **kwargs):
            a = dict(args or {})
            a.update(kwargs)
            return _gw(str(op), a)
        g["call"] = call
    g.update(data)
_setup(_j.loads(%(functions)r), %(generic)r, _j.loads(%(data)r))
'''


class Job:
    def __init__(self, spec):
        self.id, self.spec, self.created = uuid.uuid4().hex, spec, time.time()
        self.outbox, self.inbox = queue.Queue(), queue.Queue()


JOBS, JOBS_LOCK, PENDING = {}, threading.Lock(), queue.Queue()
STATE = {"coldMs": None, "busy": False, "error": None, "runs": 0}
CURRENT = {"job": None}


def gw(**kwargs):
    """The one host tool: forwards a stub call to the UI through the job and waits for its result."""
    job = CURRENT["job"]
    if job is None:
        return json.dumps({"fatal": "no job is running"})
    payload = json.loads(kwargs.get("payload") or "{}")
    job.outbox.put({"call": payload.get("call"), "args": payload.get("args") or {}})
    try:
        return json.dumps(job.inbox.get(timeout=CALL_TIMEOUT_S))
    except queue.Empty:
        return json.dumps({"fatal": f"the sandbox UI did not answer the tool call within {CALL_TIMEOUT_S} s"})


def build():
    from hyperlight_sandbox import Sandbox
    began = time.perf_counter()
    sandbox = Sandbox(backend="wasm", module="python_guest.path")
    sandbox.register_tool("gw", gw)
    sandbox.run("None")
    snapshot = sandbox.snapshot()
    return sandbox, snapshot, round((time.perf_counter() - began) * 1000)


def worker():
    sandbox = snapshot = None
    try:
        sandbox, snapshot, STATE["coldMs"] = build()
    except Exception as error:  # no KVM, or the backend failed to load: /health says why
        STATE["error"] = f"{type(error).__name__}: {error}"
    while True:
        job = PENDING.get()
        STATE["busy"], CURRENT["job"] = True, job
        spec = job.spec
        try:
            began = time.perf_counter()
            if spec.get("fresh") or sandbox is None:
                sandbox = snapshot = None
                sandbox, snapshot, STATE["coldMs"] = build()
                STATE["error"] = None
            else:
                sandbox.restore(snapshot)
            start_ms = round((time.perf_counter() - began) * 1000)
            code = PREAMBLE % {"functions": json.dumps(spec.get("functions") or {}), "generic": bool(spec.get("generic")),
                               "data": json.dumps(spec.get("data") or {})} + "\n" + spec["code"]
            ran = time.perf_counter()
            result = sandbox.run(code)
            run_ms = round((time.perf_counter() - ran) * 1000)
            output, error, cap = result.stdout or "", None, int(spec.get("maxOutput") or 4000)
            if result.exit_code != 0:
                lines = [l for l in (result.stderr or "").strip().splitlines() if l.strip()]
                error = lines[-1] if lines else f"the script exited with code {result.exit_code}"
            if len(output) > cap:
                output, error = output[:cap], error or "RuntimeError: the script printed too much output"
            job.outbox.put({"done": True, "output": output, "error": error, "startMs": start_ms, "runMs": run_ms,
                            "fresh": bool(spec.get("fresh")), "coldMs": STATE["coldMs"]})
        except Exception as e:
            job.outbox.put({"done": True, "output": "", "error": f"{type(e).__name__}: {e}"})
        finally:
            STATE["busy"], CURRENT["job"] = False, None
            STATE["runs"] += 1


def reap():
    while True:
        time.sleep(30)
        with JOBS_LOCK:
            for jid in [j for j, job in JOBS.items() if time.time() - job.created > JOB_TTL_S]:
                del JOBS[jid]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def _job(self, suffix):
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) != 3 or parts[0] != "jobs" or parts[2] != suffix:
            return None
        with JOBS_LOCK:
            return JOBS.get(parts[1])

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": STATE["error"] is None and STATE["coldMs"] is not None, "kvm": os.path.exists("/dev/kvm"),
                                    "mshv": os.path.exists("/dev/mshv"), **STATE, "queued": PENDING.qsize()})
        job = self._job("next")
        if not job:
            return self._send(404, {"error": "unknown job"})
        try:
            msg = job.outbox.get(timeout=POLL_S)
        except queue.Empty:
            return self._send(200, {"wait": True})
        if msg.get("done"):
            with JOBS_LOCK:
                JOBS.pop(job.id, None)
        self._send(200, msg)

    def do_POST(self):
        if self.path == "/run":
            spec = self._body()
            if not isinstance(spec.get("code"), str):
                return self._send(400, {"error": "code is required"})
            job = Job(spec)
            with JOBS_LOCK:
                JOBS[job.id] = job
            PENDING.put(job)
            return self._send(200, {"id": job.id, "queued": PENDING.qsize()})
        job = self._job("reply")
        if not job:
            return self._send(404, {"error": "unknown job"})
        job.inbox.put(self._body())
        self._send(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reap, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
