from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_telemetry(app: FastAPI) -> TracerProvider:
    # Attach stable service information to every span so the backend can distinguish
    # APOD traces from GraphQL and traces produced by other Cosmofy microservices.
    resource = Resource.create(
        {
            "service.name": "apod",
            "service.namespace": "cosmofy",
            "service.version": app.version,
        }
    )

    # The provider creates and owns every trace produced by this FastAPI service.
    provider = TracerProvider(resource=resource)

    # BatchSpanProcessor exports spans in the background instead of delaying the
    # user's request. OTLP sends them to the private collector running on Oracle Cloud;
    # that collector can then forward them to AWS X-Ray. OTLPSpanExporter uses the
    # standard local endpoint by default and accepts standard OTEL_* deployment
    # environment variables when Oracle Cloud needs a different collector address.
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter()
        )
    )

    # Make this provider available to all OpenTelemetry-instrumented libraries.
    trace.set_tracer_provider(provider)

    # When GraphQL calls this service, FastAPI instrumentation extracts GraphQL's
    # W3C `traceparent` header and continues the same distributed trace. This is what
    # lets the trace viewer draw GraphQL -> APOD as one connected request path.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

    # HTTPX instrumentation records outgoing calls and injects `traceparent` when
    # another instrumented service is called. It will cover our NASA HTTP requests.
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)

    # Redis instrumentation creates child spans for commands such as GET, SET, and PING.
    RedisInstrumentor().instrument(tracer_provider=provider)

    # The SDK registers process-exit shutdown for this provider, which flushes its
    # remaining buffered spans. Returning it also keeps explicit lifecycle control
    # available if deployment requirements change later.
    return provider
