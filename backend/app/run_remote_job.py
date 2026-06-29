import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--api-base-url", default=os.getenv("BACKEND_INTERNAL_API_URL", "http://backend:8000"))
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("REMOTE_JOB_POLL_INTERVAL", "5")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("REMOTE_JOB_TIMEOUT", "7200")))
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="KEY=VALUE passed to backend job env_overrides",
    )
    return parser.parse_args()


def parse_env_overrides(raw_items):
    env_overrides = {}
    for item in raw_items:
        if "=" not in item:
            raise ValueError(f"invalid --env value: {item}")
        key, value = item.split("=", 1)
        env_overrides[key] = value
    return env_overrides


def http_json(method, url, payload=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_status(prefix, state):
    status = state.get("status")
    run_id = state.get("run_id", "-")
    log_tail = (state.get("log_tail") or "").strip()
    print(f"[remote-job] {prefix} job={state.get('job_name')} run_id={run_id} status={status}", flush=True)
    if log_tail:
        lines = log_tail.splitlines()[-8:]
        for line in lines:
            print(f"[remote-job][log] {line}", flush=True)


def main():
    args = parse_args()
    env_overrides = parse_env_overrides(args.env)
    base_url = args.api_base_url.rstrip("/")
    status_url = f"{base_url}/api/internal/jobs/{args.job_name}"
    start_url = f"{status_url}/start"

    start_state = http_json("POST", start_url, {"env_overrides": env_overrides})
    print_status("started", start_state)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(args.poll_interval)
        state = http_json("GET", status_url)
        print_status("poll", state)
        status = state.get("status")
        if status == "success":
            return 0
        if status == "failed":
            return 1

    print(f"[remote-job] timeout after {args.timeout}s", flush=True)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"[remote-job] request failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(3)
    except Exception as exc:
        print(f"[remote-job] fatal error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(4)
