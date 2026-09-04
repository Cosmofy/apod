import os


# Unit tests use in-memory span exporters where tracing behavior is under test.
# Prevent the application-wide exporter from contacting a local Collector.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
