#!/usr/bin/env python3
"""GET-only live Pictures contract checks; never print response/source bodies.

Example (run after deployment, with a known EO video publication date):
  python scripts/check_picture_contracts.py --base-url http://node-a:8000 \
      --eo-video-date YYYY-MM-DD --require-hybrid --require-s3

Requires httpx, not application settings/credentials. Does not deploy, query a DB,
generate embeddings directly, or fetch media URLs. GETs may trigger ordinary
server-side cache/storage fills; text search may invoke OpenAI. Empty search or
similarity is contract-valid but reported as a warning, not corpus verification.
Exit 0: all contracts pass; 1: contract/transport failure; 2: invalid CLI options.
"""

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import math
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


COMMON_FIELDS = {"date", "title", "explanation", "media_type", "url", "url_fallback", "credit", "copyright", "source"}
EO_FIELDS = COMMON_FIELDS | {"article_url", "image_date", "location_name", "latitude", "longitude"}
PRIVATE_FIELDS = {"hdurl", "s3_object_key", "embedding", "s3_url", "archive_sha256"}


@dataclass
class Case:
    name: str
    path: str
    params: dict = field(default_factory=dict)
    kind: str = "lookup"
    source: str = "earth_observatory"
    status: int = 200
    error_code: str | None = None
    limit: int = 10
    media_type: str | None = None


def iso_date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def web_url(value):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parts = urlsplit(value)
        parts.port  # Reject malformed/out-of-range ports before constructing a client request.
        return parts.scheme in {"http", "https"} and bool(parts.hostname) and parts.username is None and parts.password is None
    except ValueError:
        return False


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_picture(item, source, errors, prefix, *, scored=False, match_types=False, require_s3=False):
    def check(condition, name):
        if not condition:
            errors.append(f"{prefix}.{name}")

    if not isinstance(item, dict):
        errors.append(f"{prefix}.object_required")
        return
    fields = EO_FIELDS if source == "earth_observatory" else COMMON_FIELDS
    check(fields <= item.keys(), "required_fields")
    check(not PRIVATE_FIELDS.intersection(item), "private_fields_exposed")
    check(item.get("source") == source, "source")
    check(iso_date(item.get("date")), "date")
    for key in ("title", "explanation"):
        check(isinstance(item.get(key), str), key)
    for key in ("credit", "copyright"):
        check(item.get(key) is None or isinstance(item[key], str), key)
    media_type = item.get("media_type")
    check(media_type in ("image", "video") if source == "earth_observatory" else isinstance(media_type, str) and bool(media_type), "media_type")
    # Historical APOD 'other' entries may legitimately have no media URL.
    check(web_url(item.get("url")) or (source == "astronomy" and media_type not in ("image", "video") and item.get("url") == ""), "url")
    fallback = item.get("url_fallback")
    check(fallback is None or web_url(fallback), "url_fallback")
    check(fallback is None or fallback != item.get("url"), "duplicate_fallback")
    if source == "earth_observatory":
        check(web_url(item.get("article_url")), "article_url")
        check(item.get("image_date") is None or iso_date(item["image_date"]), "image_date")
        check(item.get("location_name") is None or isinstance(item["location_name"], str), "location_name")
        for key, bound in (("latitude", 90), ("longitude", 180)):
            value = item.get(key)
            check(value is None or (number(value) and -bound <= value <= bound), key)
        if web_url(item.get("url")):
            parts = urlsplit(item["url"])
            archived = parts.hostname.endswith(".amazonaws.com") and ".s3." in parts.hostname
            if archived:
                check(bool(re.fullmatch(rf"/eo/{media_type}/[0-9a-f]{{64}}\.[a-z0-9]+", parts.path)), "archive_path")
                check(web_url(fallback), "archive_fallback")
            if require_s3 and media_type == "image":
                check(archived, "s3_image_required")
    else:
        check("article_url" not in item, "private_article_url_exposed")
    if scored:
        score = item.get("relevance_score")
        check(number(score) and 0 <= score <= 1, "bounded_score")
    if match_types:
        types = item.get("match_types")
        check(isinstance(types, list) and bool(types) and all(t in ("lexical", "semantic") for t in types) and len(set(types)) == len(types), "match_types")


def validate_payload(body, case, *, require_hybrid=False, require_s3=False):
    errors, warnings, metrics = [], [], {}
    if not isinstance(body, dict):
        return ["response.object_required"], warnings, metrics
    if case.kind == "error":
        if case.error_code:
            error = body.get("error")
            if not isinstance(error, dict) or error.get("code") != case.error_code or not isinstance(error.get("message"), str):
                errors.append("response.error_envelope")
        elif not isinstance(body.get("detail"), list) or not body["detail"]:
            errors.append("response.validation_detail")
        return errors, warnings, metrics
    if case.kind == "lookup":
        validate_picture(body, case.source, errors, "picture", require_s3=require_s3 and case.name != "eo.latest")
        if "date" in case.params and body.get("date") != case.params["date"]:
            errors.append("picture.requested_date")
        if case.media_type and body.get("media_type") != case.media_type:
            errors.append("picture.expected_media_type")
        return errors, warnings, metrics

    results = body.get("results")
    if not isinstance(results, list):
        return ["response.results_array_required"], warnings, metrics
    metrics["result_count"] = len(results)
    if len(results) > case.limit:
        errors.append("response.limit_exceeded")
    if not results:
        warnings.append("empty_results_not_corpus_verification")
    if case.kind == "search":
        if body.get("query") != " ".join(case.params["q"].split()):
            errors.append("response.normalized_query")
        mode = body.get("search_mode")
        if mode not in ("hybrid", "lexical", "semantic"):
            errors.append("response.search_mode")
        else:
            metrics["search_mode"] = mode
            if mode != "hybrid":
                warnings.append("degraded_search")
                if require_hybrid:
                    errors.append("response.hybrid_required")
    elif body.get("date") != case.params["date"]:
        errors.append("response.source_date")
    dates, scores = [], []
    for index, item in enumerate(results):
        validate_picture(item, case.source, errors, f"results[{index}]", scored=True,
                         match_types=case.source == "astronomy" and case.kind == "search", require_s3=require_s3)
        if not isinstance(item, dict):
            continue
        day, score = item.get("date"), item.get("relevance_score")
        if iso_date(day):
            dates.append(day)
        if number(score):
            scores.append(score)
    if len(set(dates)) != len(dates):
        errors.append("response.duplicate_dates")
    if case.kind == "similar" and case.params["date"] in dates:
        errors.append("response.source_not_excluded")
    if scores != sorted(scores, reverse=True):
        errors.append("response.scores_not_descending")
    # Do not enforce date order for equal public scores: search rounds scores,
    # similarity clamps them, and distinct raw scores can therefore appear tied.
    if case.kind == "search" and scores and scores[0] != 1.0:
        errors.append("response.search_top_score_not_normalized")
    if scores:
        metrics.update(score_min=min(scores), score_max=max(scores))
    return errors, warnings, metrics


def build_cases(image_dates, video_date, apod_date, *, skip_latest=False):
    cases = [Case(f"eo.lookup.image{index}", "/earth-observatory", {"date": day}, media_type="image")
             for index, day in enumerate(image_dates, 1)]
    cases.append(Case("eo.lookup.video", "/earth-observatory", {"date": video_date}, media_type="video"))
    if not skip_latest:
        cases.append(Case("eo.latest", "/earth-observatory"))
    cases.extend([
        Case("eo.search.moon", "/earth-observatory/search", {"q": "moon"}, kind="search"),
        Case("eo.similar.default", "/earth-observatory/similar", {"date": image_dates[0]}, kind="similar"),
        Case("eo.similar.limit50", "/earth-observatory/similar", {"date": image_dates[0], "limit": 50}, kind="similar", limit=50),
        Case("eo.lookup.invalid_date", "/earth-observatory", {"date": "2025-02-30"}, kind="error", status=422, error_code="INVALID_DATE_FORMAT"),
        Case("eo.search.invalid_query", "/earth-observatory/search", {"q": "___!?"}, kind="error", status=422, error_code="INVALID_SEARCH_QUERY"),
        Case("eo.search.missing_query", "/earth-observatory/search", kind="error", status=422),
    ])
    for value in (0, 51, "abc", "1.5"):
        cases.append(Case(f"eo.similar.invalid_limit.{value}", "/earth-observatory/similar", {"date": "2025-01-01", "limit": value}, kind="error", status=422, error_code="INVALID_SIMILARITY_REQUEST"))
        cases.append(Case(f"eo.search.invalid_limit.{value}", "/earth-observatory/search", {"q": "moon", "limit": value}, kind="error", status=422))
    for label, params in (("missing", {}), ("invalid", {"date": "2025-02-30"})):
        cases.append(Case(f"eo.similar.date.{label}", "/earth-observatory/similar", params, kind="error", status=422, error_code="INVALID_DATE_FORMAT"))
    cases.extend([
        Case("apod.lookup", "/apod", {"date": apod_date}, source="astronomy"),
        Case("apod.search.moon", "/vector/search", {"q": "moon"}, kind="search", source="astronomy"),
        Case("apod.similar.default", "/vector/similar", {"date": apod_date}, kind="similar", source="astronomy"),
        Case("apod.similar.limit50", "/vector/similar", {"date": apod_date, "limit": 50}, kind="similar", source="astronomy", limit=50),
        Case("apod.lookup.invalid_date", "/apod", {"date": "2025-02-30"}, kind="error", status=422, error_code="INVALID_DATE_FORMAT"),
        Case("apod.similar.invalid_limit", "/vector/similar", {"date": apod_date, "limit": 51}, kind="error", status=422, error_code="INVALID_SIMILARITY_REQUEST"),
    ])
    return cases


def run_checks(client, base_url, cases, *, require_hybrid=False, require_s3=False):
    checks = []
    for case in cases:
        request_id = "picture-contract-" + uuid4().hex
        started = time.monotonic()
        record = {"name": case.name, "path": case.path, "params": case.params,
                  "request_id": request_id, "expected_status": case.status,
                  "status": None, "errors": [], "warnings": []}
        try:
            response = client.get(base_url.rstrip("/") + case.path, params=case.params,
                                  headers={"x-request-id": request_id, "accept": "application/json"})
            record["status"] = response.status_code
            if response.status_code != case.status:
                record["errors"].append("http.unexpected_status")
            if response.headers.get("x-request-id") != request_id:
                record["errors"].append("http.request_id_not_preserved")
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                record["errors"].append("http.content_type")
            try:
                body = response.json()
            except (ValueError, UnicodeError):
                record["errors"].append("http.invalid_json")
            else:
                errors, warnings, metrics = validate_payload(body, case, require_hybrid=require_hybrid, require_s3=require_s3)
                record["errors"].extend(errors)
                record["warnings"].extend(warnings)
                record.update(metrics)
        except httpx.HTTPError as error:
            # Exception messages/response bodies can contain secrets or source text.
            record["errors"].append("transport." + type(error).__name__)
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        record["passed"] = not record["errors"]
        checks.append(record)
    failed = sum(not check["passed"] for check in checks)
    return {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
            "passed": failed == 0, "total": len(checks), "failed": failed,
            "warnings": sum(bool(check["warnings"]) for check in checks),
            "policy": {"require_hybrid": require_hybrid, "require_s3_images": require_s3},
            "notes": ["GET-only; no deployment or direct storage writes", "GET handlers may populate caches/storage; text search may call OpenAI",
                      "No media downloads, raw cosine verification, or corpus-completeness verification", "Source bodies, titles, explanations, and URLs are not emitted"],
            "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="HTTP(S) Pictures service root; optional path prefix, no credentials/query/fragment")
    parser.add_argument("--eo-image-dates", nargs=2, default=["2025-01-01", "2026-09-11"], metavar=("DATE1", "DATE2"))
    parser.add_argument("--eo-video-date", required=True, help="Known EO video publication date (YYYY-MM-DD)")
    parser.add_argument("--apod-date", default="2025-01-01")
    parser.add_argument("--timeout", type=float, default=45.0, help="Per-request timeout in seconds (default: 45)")
    parser.add_argument("--skip-latest", action="store_true", help="Skip latest EO GET to avoid its cache-fill path")
    parser.add_argument("--require-hybrid", action="store_true", help="Fail degraded search instead of warning")
    parser.add_argument("--require-s3", action="store_true", help="Require archived EO image URLs except latest; video embeds remain valid")
    args = parser.parse_args(argv)
    if not web_url(args.base_url):
        parser.error("base URL must be HTTP(S) without credentials")
    parsed = urlsplit(args.base_url)
    if parsed.query or parsed.fragment:
        parser.error("base URL must not include a query or fragment")
    if not all(iso_date(day) for day in [*args.eo_image_dates, args.eo_video_date, args.apod_date]):
        parser.error("all fixture dates must use YYYY-MM-DD")
    if len(set([*args.eo_image_dates, args.eo_video_date])) != 3:
        parser.error("the three EO lookup dates must be distinct")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be positive and finite")
    cases = build_cases(args.eo_image_dates, args.eo_video_date, args.apod_date, skip_latest=args.skip_latest)
    # No redirects/retries: preserve node targeting and avoid repeated costly GETs.
    with httpx.Client(timeout=args.timeout, follow_redirects=False, trust_env=False) as client:
        summary = run_checks(client, args.base_url, cases, require_hybrid=args.require_hybrid, require_s3=args.require_s3)
    print(json.dumps(summary, indent=2, allow_nan=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
