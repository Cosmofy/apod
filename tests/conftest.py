import os


# Unit tests use in-memory span exporters where tracing behavior is under test.
# Prevent the application-wide exporter from contacting a local Collector.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

# Local production routing must never bypass the shared-connection test doubles.
# Dedicated EO routing tests explicitly override these values as needed.
os.environ["EO_DATABASE_URL"] = ""
os.environ["EO_AUTH_TOKEN"] = ""
