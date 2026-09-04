# Project Guidance

## Service purpose

This repository contains Cosmofy's APOD microservice. It is a FastAPI service consumed by Cosmofy's Java GraphQL server rather than directly owning the public client interface.

Its responsibilities are:

- Retrieve NASA's Astronomy Picture of the Day for an exact date, defaulting to the current Mountain Time date.
- Serve today's APOD through Redis when cached, then fall back to Turso and finally NASA.
- Persist exact APOD records and one OpenAI embedding per APOD in Turso.
- Provide hybrid APOD discovery using lexical/BM25 matching plus semantic vector similarity.
- Add each new daily APOD and embedding through a scheduled request.
- Expose liveness/readiness endpoints and telemetry needed by the wider Cosmofy platform.

The Java GraphQL edge/gateway, mobile or web interface, LangChain/agent behavior, and the separate planetary/cosmic knowledge graph are outside this service's ownership. This service provides reliable APOD data and search capabilities to those consumers.

## Observability end goal

Build toward one request-explorer interface that can reconstruct an entire request path with millisecond timings:

- Capture the originating client IP and country using trusted edge-provided metadata, with appropriate privacy controls.
- Show the nearest GraphQL edge location and whether each part of the response was served at the edge or forwarded.
- Continue one W3C trace context from the GraphQL layer into every called microservice.
- Show fan-out through services and dependencies such as Redis, Turso, NASA, and OpenAI as a trace waterfall or service diagram.
- Correlate every trace with structured logs using `trace_id`, `span_id`, and `request_id`.
- Provide a logging view containing timestamps, severity, service, action, duration, and useful failure details.
- Prefer OpenTelemetry and an established trace/log backend over building custom collection and storage. A custom Cosmofy presentation layer may be added over established backend APIs later.

The services are deployed on Oracle Cloud. Send telemetry to a private OpenTelemetry Collector colocated with the OCI workloads; the collector may export to AWS X-Ray and CloudWatch or another selected backend. Never place AWS credentials in an application service or expose the OTLP collector port publicly.

Do not implement the final dashboard until its backend and edge-provider requirements have been explicitly selected.

## Observability interface design direction

The primary visual inspiration is Palantir's Ontology System overview: a clean, spacious, layered operational canvas rather than a conventional chart-heavy monitoring dashboard. Use the structural idea without copying Palantir branding or artwork.

Map that visual language to Cosmofy as follows:

- The central plane is a live or historical request graph containing the client, edge location, Java GraphQL operation, called microservices, caches, databases, external APIs, and returned response branches.
- Nodes use compact object cards, status indicators, and millisecond durations.
- Edges use typed relationship labels such as `ROUTED_THROUGH`, `INVOKED`, `CACHE_HIT`, `CACHE_MISS`, `QUERIED`, `CALLED`, `RETURNED`, and `EMITTED_LOG`.
- Lower layers represent telemetry sources and systems: edge metadata, OpenTelemetry spans, structured logs, infrastructure, databases, and external providers.
- Upper layers represent request analytics, investigation workflows, alerts/automations, and developer/operator interfaces.
- Selecting any node opens its attributes and automatically filters the correlated logs using `trace_id`, `span_id`, and `request_id`.
- Support both a live request view and historical replay with a timeline, while preserving a calm, minimal, ontology-style presentation.

## Git commit convention

Every commit message must begin with one relevant emoji followed by a concise, entirely lowercase description.

Examples:

- `✨ add hybrid search`
- `🐛 fix redis lock release`
- `🧪 add vector search tests`
- `👷 improve ci test reporting`
- `📝 document deployment architecture`
