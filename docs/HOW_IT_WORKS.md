# How This Works (a walkthrough for junior devs)

This doc explains the *concepts* behind distributed tracing and then walks
through *this exact repo*, file by file, so you can see where each concept
lives in real code. If you already know what a span/trace is, skip to
["Following one request through the code"](#following-one-request-through-the-code).

## The problem, restated simply

One button click ("checkout") triggers a chain of HTTP calls across five
separate processes:

```
gateway -> cart -> inventory -> payment -> order
```

If the whole thing takes 1.4 seconds, which of those five processes used up
the time? You can't tell from the outside — the browser only sees "1.4s,
gateway responded." You need visibility *inside* the chain, per hop.

That's what tracing gives you. Everything else in this repo exists to answer
one question: **which service, and which specific piece of work inside it,
is slow?**

## Core concepts (read this before the code)

### Span

A span is a record of one unit of work: a name, a start time, an end time,
and some key/value metadata ("attributes"). "cart called GET
/inventory/reserve, took 30ms" is a span. A single incoming HTTP request to
one service usually produces *at least* one span (often more, e.g. one for
the inbound request and one for each outbound call it makes).

### Trace

A trace is just "all the spans that belong to one logical request,"
strung together into a tree by parent/child relationships. The gateway's
span is the root; the cart span it triggered is a child; the inventory span
cart triggered is a child of that, and so on.

### Trace ID and context propagation

For spans created in five *different processes* to be recognized as
belonging to one trace, they all need to share the same trace ID. This
happens automatically: when `gateway` calls `cart` over HTTP, the
OpenTelemetry library injects an extra header —

```
traceparent: 00-<32-hex-char-trace-id>-<16-hex-char-parent-span-id>-01
```

— into the outgoing request. When `cart`'s web framework receives that
request, its OpenTelemetry instrumentation reads the header back out and
starts its own span as a *child* of the one described in it. Repeat at every
hop and the trace ID rides along the entire five-service chain for free. You
never touch this header yourself — the instrumentation libraries do it.

### Instrumentation: auto vs. manual

"Instrumentation" just means "code that creates spans." You can write this
by hand (`tracer.start_span(...)` around every function you care about), or
use **auto-instrumentation**, where a library monkey-patches well-known
frameworks (FastAPI, `httpx`, `requests`, database drivers, ...) so spans get
created for you with zero code changes. This repo leans on auto-instrumentation
for the request/response skeleton of every span — see
[below](#1-the-service-code) for exactly how — and adds a small amount of
*manual* instrumentation in one place
([`services/payment/app.py`](../services/payment/app.py)) where
business-logic detail (*why* a charge was declined) needs attaching to a
span that auto-instrumentation already started. See
["Manual instrumentation"](#7-manual-instrumentation-annotating-a-span-auto-instrumentation-cant)
below.

### Resource attributes

Metadata attached to *every* span coming from one process — usually "what
produced this," e.g. `service.name=inventory`,
`deployment.environment=docker-compose-demo`. This is how a trace viewer
knows to label a span "inventory" instead of just showing a raw span name.

## Following one request through the code

Here's `curl http://localhost:8000/checkout` traced through every file that
touches it, in order.

### 1. The service code

[`services/gateway/app.py`](../services/gateway/app.py) is a plain FastAPI
app — there is no OpenTelemetry import anywhere in it:

```python
@app.get("/checkout")
async def checkout():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{CART_URL}/cart/checkout")
    return {"service": "gateway", "downstream": resp.json()}
```

All five services (`services/gateway`, `services/cart`,
`services/inventory`, `services/payment`, `services/order`) look like this:
receive a request, maybe do a little work (`asyncio.sleep(...)` stands in for
"real work" like a DB query), call the next service, return its response.
Nothing here creates a span. So where do spans come from?

### 2. The Dockerfile — this is where auto-instrumentation gets wired in

Every service's `Dockerfile` (e.g.
[`services/gateway/Dockerfile`](../services/gateway/Dockerfile)) does this:

```dockerfile
RUN pip install --no-cache-dir -r requirements.txt \
    && opentelemetry-bootstrap -a install

CMD ["opentelemetry-instrument", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

Two things matter here:

- `opentelemetry-bootstrap -a install` looks at what's in `requirements.txt`
  (`fastapi`, `httpx`) and installs the matching instrumentation packages
  (`opentelemetry-instrumentation-fastapi`,
  `opentelemetry-instrumentation-httpx`) automatically.
- `opentelemetry-instrument` is a wrapper executable. Instead of running
  `uvicorn app:app` directly, we run
  `opentelemetry-instrument uvicorn app:app` — the wrapper patches FastAPI
  and httpx *before* your app code even runs, so every inbound request and
  every outbound `httpx` call automatically becomes a span, with the
  `traceparent` header handled for you.

This is the entire reason `app.py` can stay free of tracing code.

### 3. Environment variables — telling each service where to send spans

In [`docker-compose.yml`](../docker-compose.yml), an anchor block
(`x-otel-env`, lines 1–6) is reused by every service:

```yaml
x-otel-env: &otel-env
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
  OTEL_EXPORTER_OTLP_PROTOCOL: http/protobuf
  OTEL_TRACES_EXPORTER: otlp
  OTEL_METRICS_EXPORTER: none
  OTEL_LOGS_EXPORTER: none
```

Plus each service sets its own `OTEL_SERVICE_NAME` (e.g. `inventory`) — this
becomes the `service.name` resource attribute that shows up as the label on
every span from that container. `opentelemetry-instrument` reads all of
these env vars automatically; nothing in `app.py` reads them.

**In one sentence: `inventory` doesn't know or care about Tempo, Grafana, or
S3 — it just ships spans to whatever's listening at
`otel-collector:4318`.** That indirection is deliberate; see the next
section.

### 4. The collector — the single place spans get bundled and forwarded

[`otel-collector/otel-collector-config.yaml`](../otel-collector/otel-collector-config.yaml)
defines a pipeline with three stages:

```yaml
receivers: [otlp]              # accept spans over gRPC (4317) and HTTP (4318)
processors: [resource, batch]  # tag every span, then group them into batches
exporters: [otlp, debug]       # forward the batch to Tempo (+ log a summary)
```

Why not have every service send spans straight to Tempo? Two reasons:

1. **One choke point to change.** If you swap Tempo for Jaeger or Honeycomb
   tomorrow, you edit this one YAML file — not 100+ services.
2. **Batching.** Sending one network call per span would be enormously
   wasteful at scale. The `batch` processor accumulates spans for a short
   window and ships them together.

In a real Kubernetes cluster, this collector config runs as a **DaemonSet**
— Kubernetes automatically runs one copy on every node, and each collector
only handles spans from pods scheduled on its own node. Docker Compose has
no concept of "nodes," so here it's a single container — same config,
smaller footprint.

### 5. Tempo — where spans actually live

[`tempo/tempo.yaml`](../tempo/tempo.yaml) has one section that matters most:

```yaml
storage:
  trace:
    backend: s3
    s3:
      bucket: tempo-traces
      endpoint: minio:9000
      ...
```

Tempo doesn't run its own database. It buffers incoming spans briefly (the
Write-Ahead Log, or WAL — `/var/tempo/wal` in the compose file), groups them
into a compressed "block," and pushes that block straight to an S3 bucket.
Here, [MinIO](https://min.io/) plays the role of S3 (`docker-compose.yml`'s
`minio` service) — it speaks the same API, so nothing in `tempo.yaml` would
need to change to point at real AWS S3 in production. Object storage is used
because trace data is huge in volume but rarely read — cheap, durable,
practically-unlimited storage is exactly the right trade-off, versus paying
for a traditional database's random-write performance you'll never use.

### 6. Grafana — turning stored spans back into a picture

[`grafana/provisioning/datasources/tempo.yaml`](../grafana/provisioning/datasources/tempo.yaml)
pre-registers Tempo as a datasource so there's no manual setup. When you
open **Explore → Tempo** and search or paste a trace ID, Grafana asks Tempo
"give me every span for this trace ID," gets back the whole tree, and lays
it out as the waterfall you see in the [README](../README.md#reproducing-checkout-got-slower)
screenshot — one bar per span, width proportional to duration, nested to
show parent/child.

### 7. Manual instrumentation: annotating a span auto-instrumentation can't

Auto-instrumentation only knows generic HTTP facts: method, path, status
code, whether an exception escaped the handler. It has no idea *why* your
business logic decided something failed. [`services/payment/app.py`](../services/payment/app.py)
shows the pattern for filling that gap:

```python
span = trace.get_current_span()

if random.random() < FAILURE_RATE:
    error = RuntimeError("payment provider declined the charge")
    span.record_exception(error)
    span.set_attribute("payment.outcome", "declined")
    span.set_status(Status(StatusCode.ERROR, str(error)))
    raise HTTPException(status_code=402, detail="payment declined")
```

`trace.get_current_span()` reaches into the span FastAPI's instrumentation
already opened for this request — you don't create a new one, you enrich the
one that exists. `record_exception` attaches the exception (type, message,
stack trace) as a span event; `set_attribute` adds a searchable key/value
(`payment.outcome=declined`) that TraceQL can filter on later; `set_status`
marks the span as an error explicitly, independent of the HTTP status code
returned. This is the same three-call pattern (`record_exception` /
`set_attribute` / `set_status`) you'd reach for anywhere business logic
decides something is exceptional but doesn't itself throw an unhandled
Python exception.

Every service that calls downstream also does `resp.raise_for_status()` on
the response it gets back. That's what turns "payment returned 402" into an
actual raised exception at each hop — `gateway`'s call to `cart` fails
because `cart`'s call to `inventory` failed because `inventory`'s call to
`payment` failed. FastAPI's instrumentation marks each of *those* spans as
an error too (it caught an unhandled exception), but only the `payment` span
carries the `payment.outcome` attribute and the original exception — the
rest are just relaying a failure that started downstream. See
[README: Reproducing a failed payment](../README.md#reproducing-a-failed-payment)
to trigger it and see both kinds of error side by side in one trace.

## Putting it together: why the trace ID makes this all "just work"

Nothing in this repo has a central place that says "here's every span for
request X, go collect them." Instead:

1. `gateway` starts a span, generates a fresh trace ID (since it's the
   root), and stamps it into the `traceparent` header of its call to `cart`.
2. `cart`'s instrumentation reads that header, starts its own span as a
   child using the *same* trace ID, and repeats the stamping for its call to
   `inventory`. Same for `inventory` → `payment` → `payment` → `order`.
3. Every span, from every service, independently gets shipped to the
   collector, batched, and written to Tempo — with no coordination between
   services beyond that one header.
4. Tempo just stores spans tagged with a trace ID. Reassembly only happens
   at *query time*, when Grafana asks Tempo for "everything with trace ID
   `X`."

This is why the architecture scales past 5 or even 100 services with zero
extra design: every hop only needs to know "pass the header along," and
storage/reassembly is centralized without requiring services to know about
each other.

## Try it yourself

1. `docker compose up --build -d`
2. `curl http://localhost:8000/checkout`
3. Open Grafana (`localhost:3000`) → Explore → Tempo → Search → click the
   trace that shows up.
4. Now run
   `INVENTORY_EXTRA_DELAY_MS=1200 docker compose up -d --build inventory`
   and repeat steps 2–3. Compare the two traces: same shape, wildly
   different `inventory` span width. That's the whole point.
5. Reset (`docker compose up -d --build inventory`), then run
   `PAYMENT_FAILURE_RATE=1 docker compose up -d --build payment` and repeat
   steps 2–3. Now every span in the trace is flagged as an error, but only
   `payment`'s carries the actual exception and a `payment.outcome`
   attribute — the rest are just reporting that their downstream call
   failed. See [section 7](#7-manual-instrumentation-annotating-a-span-auto-instrumentation-cant)
   above for why.

## Glossary

| Term | Meaning |
|---|---|
| Span | One recorded unit of work: name + start/end time + attributes |
| Trace | A tree of spans that share a trace ID — one logical request |
| Trace ID | 32-hex-char ID shared by every span in one trace |
| `traceparent` header | Carries the trace ID + parent span ID across an HTTP call |
| Instrumentation | Code that creates spans; "auto" = a library does it for you |
| Span status | Explicit OK/ERROR flag on a span, set automatically on an unhandled exception or manually via `span.set_status(...)` |
| Span event | A timestamped note attached to a span, e.g. an exception recorded via `span.record_exception(...)` |
| Resource attribute | Metadata tagged on every span from one process, e.g. `service.name` |
| OTLP | OpenTelemetry Protocol — the wire format spans are sent in |
| Collector | A process that receives, transforms, and forwards telemetry |
| DaemonSet | Kubernetes primitive: run one pod copy per cluster node |
