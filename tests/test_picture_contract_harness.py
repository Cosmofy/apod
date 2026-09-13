"""Exercise the live checker entirely offline, including safe failure output."""

import json
from unittest.mock import Mock

import httpx
import pytest

from scripts import check_picture_contracts as harness


def picture(day="2025-01-02", source="earth_observatory", media_type="image"):
    item = {"date": day, "title": "PRIVATE TITLE", "explanation": "PRIVATE BODY",
            "media_type": media_type, "url": "https://nasa.example/private.jpg",
            "url_fallback": None, "credit": None, "copyright": None, "source": source}
    if source == "earth_observatory":
        item.update(article_url="https://science.nasa.gov/private", image_date=None,
                    location_name=None, latitude=None, longitude=None)
    return item


def payload(case):
    if case.kind == "error":
        return {"error": {"code": case.error_code, "message": "PRIVATE ERROR"}} if case.error_code else {"detail": [{"msg": "PRIVATE ERROR"}]}
    if case.kind == "lookup":
        return picture(case.params.get("date", "2026-09-11"), case.source, case.media_type or "image")
    item = picture(source=case.source)
    item["relevance_score"] = 1.0 if case.kind == "search" else 0.75
    if case.kind == "search":
        if case.source == "astronomy":
            item["match_types"] = ["lexical", "semantic"]
        return {"query": case.params["q"], "search_mode": "hybrid", "results": [item]}
    return {"date": case.params["date"], "results": [item]}


def all_cases():
    # Fixture video date is synthetic, not a claim about the live archive.
    return harness.build_cases(["2025-01-01", "2026-09-11"], "2024-03-01", "2025-01-01")


def test_complete_matrix_get_only_request_ids_and_no_bodies():
    cases = all_cases()
    requests = []

    def handler(request):
        case = cases[len(requests)]
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/pictures" + case.path
        return httpx.Response(case.status, json=payload(case), headers={"x-request-id": request.headers["x-request-id"]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = harness.run_checks(client, "http://node-a/pictures/", cases)
    assert summary["passed"] is True
    assert summary["failed"] == 0
    assert len(requests) == len(cases)
    assert len({r.headers["x-request-id"] for r in requests}) == len(cases)
    output = json.dumps(summary)
    for private in ("PRIVATE", "nasa.example", "science.nasa.gov", "private.jpg"):
        assert private not in output
    similar_default = next(c for c in cases if c.name == "eo.similar.default")
    assert similar_default.params == {"date": "2025-01-01"}
    assert next(c for c in cases if c.name == "eo.similar.limit50").limit == 50
    assert len([c for c in cases if c.name.startswith("eo.lookup.") and c.status == 200]) == 3


@pytest.mark.parametrize("change,expected", [
    ({"source": "astronomy"}, "source"),
    ({"url": "javascript:secret"}, "url"),
    ({"url_fallback": "not a URL"}, "url_fallback"),
    ({"url_fallback": "https://nasa.example/private.jpg"}, "duplicate_fallback"),
    ({"s3_object_key": "SECRET"}, "private_fields_exposed"),
    ({"media_type": {}}, "media_type"),
    ({"date": "yesterday"}, "date"),
    ({"latitude": 91}, "latitude"),
    ({"longitude": True}, "longitude"),
    ({"relevance_score": float("nan")}, "bounded_score"),
    ({"relevance_score": float("inf")}, "bounded_score"),
    ({"relevance_score": 1.1}, "bounded_score"),
    ({"relevance_score": -0.1}, "bounded_score"),
    ({"relevance_score": True}, "bounded_score"),
])
def test_rejects_bad_picture_fields_without_echoing_values(change, expected):
    item = {**picture(), "relevance_score": 0.75, **change}
    errors = []
    harness.validate_picture(item, "earth_observatory", errors, "item", scored=True)
    assert f"item.{expected}" in errors
    assert "SECRET" not in json.dumps(errors)


def test_order_duplicates_exclusion_and_limit():
    case = harness.Case("similar", "/earth-observatory/similar", {"date": "2025-01-01"}, kind="similar", limit=1)
    results = [{**picture("2025-01-01"), "relevance_score": 0.1},
               {**picture("2025-01-01"), "relevance_score": 0.9}]
    errors, _, _ = harness.validate_payload({"date": "2025-01-01", "results": results}, case)
    assert set(errors) >= {"response.limit_exceeded", "response.duplicate_dates", "response.source_not_excluded", "response.scores_not_descending"}


@pytest.mark.parametrize("kind", ["search", "similar"])
def test_empty_results_are_valid_with_warning(kind):
    case = next(c for c in all_cases() if c.kind == kind)
    body = payload(case)
    body["results"] = []
    errors, warnings, metrics = harness.validate_payload(body, case)
    assert errors == []
    assert warnings == ["empty_results_not_corpus_verification"]
    assert metrics["result_count"] == 0


def test_degraded_search_strict_policy():
    case = next(c for c in all_cases() if c.kind == "search")
    body = payload(case)
    body["search_mode"] = "lexical"
    errors, warnings, _ = harness.validate_payload(body, case)
    assert not errors
    assert warnings == ["degraded_search"]
    assert "response.hybrid_required" in harness.validate_payload(body, case, require_hybrid=True)[0]


def test_s3_path_media_type_and_embed_policy():
    case = all_cases()[0]
    body = payload(case)
    assert "picture.s3_image_required" in harness.validate_payload(body, case, require_s3=True)[0]
    body.update(url="https://bucket.s3.eu-west-2.amazonaws.com/eo/image/" + "a" * 64 + ".jpg",
                url_fallback="https://nasa.example/original.jpg")
    assert harness.validate_payload(body, case, require_s3=True)[0] == []
    body["url"] = body["url"].replace("/image/", "/video/")
    assert "picture.archive_path" in harness.validate_payload(body, case)[0]
    video = next(c for c in all_cases() if c.media_type == "video")
    body = payload(video)
    body["url"] = "https://www.youtube.com/embed/example"
    assert harness.validate_payload(body, video, require_s3=True)[0] == []


@pytest.mark.parametrize("body", [None, [], "secret", {"results": "secret"}, {"results": [None, []]}])
def test_malformed_response_returns_errors_not_crash(body):
    case = next(c for c in all_cases() if c.kind == "similar")
    assert harness.validate_payload(body, case)[0]


def test_transport_errors_non_json_and_correlation_failures_continue():
    cases = all_cases()[:3]
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ConnectError("SECRET credentials", request=request)
        if count == 2:
            return httpx.Response(502, text="SECRET HTML")
        return httpx.Response(200, json=payload(cases[2]), headers={"x-request-id": "wrong SECRET"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = harness.run_checks(client, "http://node-a", cases)
    assert summary["failed"] == 3
    assert count == 3
    assert "transport.ConnectError" in summary["checks"][0]["errors"]
    assert "http.invalid_json" in summary["checks"][1]["errors"]
    assert "http.request_id_not_preserved" in summary["checks"][2]["errors"]
    assert "SECRET" not in json.dumps(summary)


@pytest.mark.parametrize("passed,exit_code", [(True, 0), (False, 1)])
def test_cli_json_and_exit_status(monkeypatch, capsys, passed, exit_code):
    run = Mock(return_value={"passed": passed})
    monkeypatch.setattr(harness, "run_checks", run)
    assert harness.main(["--base-url", "http://node-a", "--eo-video-date", "2024-03-01"]) == exit_code
    assert json.loads(capsys.readouterr().out) == {"passed": passed}


def test_similarity_success_cases_use_selected_archive_fixture():
    cases = harness.build_cases(["2025-01-02", "2026-09-11"], "2021-04-22", "2025-01-01")
    similarity = [case for case in cases if case.name in {"eo.similar.default", "eo.similar.limit50"}]
    assert len(similarity) == 2
    assert all(case.params["date"] == "2025-01-02" for case in similarity)


@pytest.mark.parametrize("extra", [
    ["--base-url", "file:///tmp/test"], ["--base-url", "http://user:secret@node-a"],
    ["--base-url", "http://node-a?secret=1"], ["--base-url", "http://node-a:999999"],
    ["--timeout", "nan"], ["--timeout", "0"], ["--eo-video-date", "bad"],
    ["--eo-video-date", "2025-01-01"],
])
def test_cli_rejects_invalid_configuration_without_network(monkeypatch, extra):
    client = Mock(side_effect=AssertionError("must not create client"))
    monkeypatch.setattr(harness.httpx, "Client", client)
    with pytest.raises(SystemExit) as error:
        harness.main(["--base-url", "http://node-a", "--eo-video-date", "2024-03-01", *extra])
    assert error.value.code == 2
    client.assert_not_called()
