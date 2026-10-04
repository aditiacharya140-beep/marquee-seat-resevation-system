"""Burst a live deployment and prove the invariants held.

    ./burst.sh https://<host>            # or: python burst/burst.py <BASE_URL>

Creates its own show, so every count it checks is exact rather than approximate.
Exits non-zero if any invariant is violated.

Phases: warm /readyz -> create show -> mint guests -> stampede (random seats, with
reconciliation sampled mid-flight) -> hot-seat storm (barrier-released, one seat) ->
idempotent retries (one key, fired concurrently) -> limit probe -> reconcile.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx

PER_USER_LIMIT = 4
HOT_SEAT = "HOT"
IDEM_SEATS = ["IDEM1", "IDEM2"]
LIMIT_PROBE_ATTEMPTS = PER_USER_LIMIT + 6
IDEMPOTENT_DUPLICATES = 20
WARM_TIMEOUT_SECONDS = 120


@dataclass
class Result:
    status: int
    code: str | None
    seats: list[str]
    reservation_id: str | None
    replay: bool
    seconds: float

    @property
    def outcome(self) -> str:
        if self.status == 201:
            return "201 created"
        if self.status == 200:
            return "200 idempotent_replay"
        return f"{self.status} {self.code or 'unknown'}"


@dataclass
class Run:
    client: httpx.AsyncClient
    limiter: asyncio.Semaphore
    show_id: str = ""
    results: list[Result] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def check(self, ok: bool, message: str) -> None:
        print(f"  [{'ok' if ok else 'FAIL'}] {message}")
        if not ok:
            self.violations.append(message)

    async def reserve(self, token: str, seats: list[str], key: str | None = None) -> Result:
        started = time.perf_counter()
        try:
            async with self.limiter:
                # Timed from here, not from before the limiter: time spent queued in
                # this script is not the service's latency.
                started = time.perf_counter()
                response = await self.client.post(
                    f"/shows/{self.show_id}/reserve",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": key or uuid.uuid4().hex,
                    },
                    json={"seats": seats},
                )
            try:
                body = response.json() if response.content else {}
            except ValueError:
                # Not this service's envelope. Every response the service writes carries
                # X-Request-ID, so its absence means the platform's proxy answered instead.
                origin = "service" if "X-Request-ID" in response.headers else "platform"
                body = {"error": {"code": f"non_json_from_{origin}"}}
                print(f"  non-JSON {response.status_code} from {origin}: {response.text[:120]!r}")
            result = Result(
                status=response.status_code,
                code=(body.get("error") or {}).get("code"),
                seats=body.get("seats", []),
                reservation_id=body.get("reservation_id"),
                replay=response.headers.get("Idempotent-Replay") == "true",
                seconds=time.perf_counter() - started,
            )
        except httpx.HTTPError as exc:
            # A dropped connection is a failure of the service under load, counted as one.
            result = Result(599, type(exc).__name__, [], None, False, time.perf_counter() - started)
        self.results.append(result)
        return result

    async def counts(self) -> dict[str, int]:
        return dict((await self.client.get(f"/shows/{self.show_id}")).json()["counts"])

    async def metric(self, series: str) -> float:
        text = (await self.client.get("/metrics")).text
        return sum(
            float(m.group(1))
            for m in re.finditer(rf"^{re.escape(series)}(?:{{[^}}]*}})? (\S+)$", text, re.MULTILINE)
        )


async def warm(run: Run) -> None:
    """A free-tier instance may be asleep; measuring its cold start would be noise."""
    deadline = time.monotonic() + WARM_TIMEOUT_SECONDS
    while True:
        try:
            if (await run.client.get("/readyz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            sys.exit(f"/readyz was not 200 within {WARM_TIMEOUT_SECONDS}s")
        await asyncio.sleep(2)


async def create_show(run: Run, general: list[str], probe: list[str]) -> None:
    login = await run.client.post(
        "/auth/login",
        json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]},
    )
    if login.status_code != 200:
        sys.exit(f"admin login failed: {login.status_code} {login.text}")
    created = await run.client.post(
        "/shows",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={
            "name": f"burst-{uuid.uuid4().hex[:8]}",
            "seats": [*general, HOT_SEAT, *IDEM_SEATS, *probe],
            "price_paise": 25000,
            "per_user_limit": PER_USER_LIMIT,
        },
    )
    if created.status_code != 201:
        sys.exit(f"show creation failed: {created.status_code} {created.text}")
    run.show_id = created.json()["show_id"]


async def mint_guests(run: Run, count: int) -> list[str]:
    async def one() -> str:
        async with run.limiter:
            response = await run.client.post("/auth/guest")
        response.raise_for_status()
        return str(response.json()["access_token"])

    return list(await asyncio.gather(*(one() for _ in range(count))))


async def stampede(run: Run, tokens: list[str], general: list[str], rng: random.Random) -> None:
    total = len(general) + 1 + len(IDEM_SEATS) + LIMIT_PROBE_ATTEMPTS
    samples: list[dict[str, int]] = []
    done = asyncio.Event()

    async def sample() -> None:
        while not done.is_set():
            samples.append(await run.counts())
            await asyncio.sleep(0.05)

    sampler = asyncio.create_task(sample())
    await asyncio.gather(
        *(run.reserve(token, rng.sample(general, rng.randint(1, 2))) for token in tokens)
    )
    done.set()
    await sampler
    broken = [s for s in samples if s["available"] + s["held"] + s["confirmed"] != total]
    run.check(
        not broken,
        f"reconciliation held in all {len(samples)} samples taken during the stampede",
    )


async def hot_seat_storm(run: Run, tokens: list[str]) -> None:
    """Every contender is parked on the barrier before any request is sent, so they
    arrive together. No connection is held while waiting: a barrier wider than a
    connection pool, taken after acquiring, never releases."""
    barrier = asyncio.Barrier(len(tokens))

    async def contend(token: str) -> Result:
        await barrier.wait()
        return await run.reserve(token, [HOT_SEAT])

    results = await asyncio.gather(*(contend(token) for token in tokens))
    tally = Counter(r.outcome for r in results)
    run.check(tally["201 created"] == 1, f"hot seat: exactly one 201 (got {tally['201 created']})")
    run.check(
        tally["409 SEAT_TAKEN"] == len(tokens) - 1,
        f"hot seat: {len(tokens) - 1} x 409 SEAT_TAKEN (got {dict(tally)})",
    )


async def idempotent_retries(run: Run, token: str) -> None:
    key = uuid.uuid4().hex
    results = await asyncio.gather(
        *(run.reserve(token, IDEM_SEATS, key) for _ in range(IDEMPOTENT_DUPLICATES))
    )
    created = [r for r in results if r.status == 201]
    replays = [r for r in results if r.status == 200 and r.replay]
    run.check(len(created) == 1, f"one key x{IDEMPOTENT_DUPLICATES}: one 201 (got {len(created)})")
    run.check(
        len(replays) == IDEMPOTENT_DUPLICATES - 1,
        f"one key x{IDEMPOTENT_DUPLICATES}: the rest replay as 200 (got {len(replays)})",
    )
    run.check(
        len({r.reservation_id for r in results}) == 1,
        "one key: every response names the same reservation",
    )


async def limit_probe(run: Run, token: str, probe: list[str]) -> None:
    results = await asyncio.gather(*(run.reserve(token, [seat]) for seat in probe))
    won = sum(r.status == 201 for r in results)
    declined = sum(r.code == "PER_USER_LIMIT" for r in results)
    run.check(
        (won, declined) == (PER_USER_LIMIT, len(probe) - PER_USER_LIMIT),
        f"limit probe: exactly {PER_USER_LIMIT} of {len(probe)} parallel requests win "
        f"(got {won} won, {declined} PER_USER_LIMIT)",
    )


async def reconcile(run: Run, total: int, faults_before: float) -> None:
    final = await run.counts()
    sold = [label for r in run.results if r.status == 201 for label in r.seats]
    server_errors = [r for r in run.results if r.status >= 500]
    run.check(
        final["available"] + final["held"] + final["confirmed"] == final["total"] == total,
        f"available + held + confirmed == total ({final})",
    )
    run.check(len(sold) == len(set(sold)), f"no seat sold twice ({len(sold)} seats sold)")
    run.check(
        final["confirmed"] == len(sold),
        f"API confirmed count ({final['confirmed']}) == seats in 201 responses ({len(sold)})",
    )
    run.check(not server_errors, f"zero 5xx and zero dropped requests (got {len(server_errors)})")
    gauge = await run.metric(f'seats_available{{show_id="{run.show_id}"}}')
    run.check(
        gauge == final["available"],
        f"metrics seats_available ({gauge:.0f}) == API available ({final['available']})",
    )
    faults = await run.metric("unhandled_exceptions_total") - faults_before
    run.check(faults == 0, f"unhandled_exceptions_total did not move (delta {faults:.0f})")


def report(run: Run, elapsed: float) -> None:
    latencies = sorted(r.seconds * 1000 for r in run.results)
    cuts = statistics.quantiles(latencies, n=100)
    print(f"\n{len(run.results)} reserve requests in {elapsed:.1f}s")
    print(f"latency ms: p50 {cuts[49]:.0f}  p95 {cuts[94]:.0f}  p99 {cuts[98]:.0f}")
    print("outcomes:")
    for outcome, count in sorted(Counter(r.outcome for r in run.results).items()):
        print(f"  {count:6d}  {outcome}")


async def main(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    general = [f"G{i:04d}" for i in range(args.seats)]
    probe = [f"LIMIT{i:02d}" for i in range(LIMIT_PROBE_ATTEMPTS)]
    total = len(general) + 1 + len(IDEM_SEATS) + len(probe)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=args.timeout,
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=args.concurrency),
    ) as client:
        run = Run(client=client, limiter=asyncio.Semaphore(args.concurrency))
        print(f"target {args.base_url}")
        await warm(run)
        await create_show(run, general, probe)
        faults_before = await run.metric("unhandled_exceptions_total")
        tokens = await mint_guests(run, args.users + args.hot + 2)
        buyers, rest = tokens[: args.users], tokens[args.users :]
        print(
            f"show {run.show_id}: {total} seats, {len(buyers)} buyers, {args.hot} on the hot seat\n"
        )

        started = time.perf_counter()
        await stampede(run, buyers, general, rng)
        await hot_seat_storm(run, rest[: args.hot])
        await idempotent_retries(run, rest[-1])
        await limit_probe(run, rest[-2], probe)
        elapsed = time.perf_counter() - started
        await reconcile(run, total, faults_before)

    report(run, elapsed)
    if run.violations:
        print(f"\nFAILED: {len(run.violations)} invariant violation(s)")
        return 1
    print("\nPASSED: every invariant held")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("base_url")
    parser.add_argument("--users", type=int, default=400, help="buyers in the stampede")
    parser.add_argument("--seats", type=int, default=200, help="general seats in the show")
    parser.add_argument("--hot", type=int, default=150, help="contenders for the one hot seat")
    parser.add_argument("--concurrency", type=int, default=200, help="requests in flight at once")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=7)
    sys.exit(asyncio.run(main(parser.parse_args())))
