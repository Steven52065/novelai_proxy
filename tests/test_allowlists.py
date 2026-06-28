from app.allowlists import (
    ALLOWED_ENDPOINT_CHOICES,
    DEFAULT_ALLOWED_ENDPOINTS,
    AllowedEndpoints,
    AllowedUpstreams,
)


def test_allowed_endpoints_parse_empty_falls_back_to_default():
    assert AllowedEndpoints.parse(None).as_list() == [DEFAULT_ALLOWED_ENDPOINTS]
    assert AllowedEndpoints.parse("").as_list() == [DEFAULT_ALLOWED_ENDPOINTS]
    assert AllowedEndpoints.parse(" , ,").as_list() == [DEFAULT_ALLOWED_ENDPOINTS]


def test_allowed_endpoints_parse_keeps_order_and_duplicates():
    parsed = AllowedEndpoints.parse(" upscale , generate-image,upscale")
    assert parsed.as_list() == ["upscale", "generate-image", "upscale"]
    assert parsed.as_frozenset() == frozenset({"upscale", "generate-image"})


def test_allowed_endpoints_serialize_strips_dedupes_and_falls_back():
    assert AllowedEndpoints.of([" upscale ", "upscale", "generate-image"]).serialize() == "upscale,generate-image"
    assert AllowedEndpoints.of(None).serialize() == DEFAULT_ALLOWED_ENDPOINTS
    assert AllowedEndpoints.of(["  "]).serialize() == DEFAULT_ALLOWED_ENDPOINTS


def test_allowed_endpoints_parse_serialize_roundtrip_normalizes():
    assert AllowedEndpoints.parse("upscale, upscale ,generate-image").serialize() == "upscale,generate-image"


def test_allowed_endpoints_unknown_reports_sorted_unknown_items():
    assert AllowedEndpoints.of(["generate-image", "zzz", "abc"]).unknown() == ["abc", "zzz"]
    assert AllowedEndpoints.of(list(ALLOWED_ENDPOINT_CHOICES)).unknown() == []


def test_allowed_upstreams_empty_means_unrestricted():
    assert AllowedUpstreams.parse(None).as_list() == []
    assert AllowedUpstreams.parse(" , ").as_frozenset() == frozenset()
    assert AllowedUpstreams.of([]).serialize() is None
    assert AllowedUpstreams.of([" ", ""]).serialize() is None


def test_allowed_upstreams_serialize_strips_and_dedupes():
    assert AllowedUpstreams.of([" opus-a ", "opus-b", "opus-a"]).serialize() == '["opus-a","opus-b"]'
    assert AllowedUpstreams.parse("opus-a, opus-a ,opus-b").serialize() == '["opus-a","opus-b"]'


def test_allowed_upstreams_json_roundtrip_preserves_arbitrary_ids():
    value = AllowedUpstreams.of(["账号/一,二 [] !", '["looks-like-json"]']).serialize()

    assert value == '["账号/一,二 [] !","[\\"looks-like-json\\"]"]'
    assert AllowedUpstreams.parse(value).as_list() == ["账号/一,二 [] !", '["looks-like-json"]']
