from orcaslicer_mcp.errors import error_from_status, Unauthorized, Validation, Conflict, NotFound


def test_maps_401():
    assert isinstance(error_from_status(401, {"error": "unauthorized"}), Unauthorized)


def test_maps_422_with_errors():
    e = error_from_status(422, {"error": "config_rejected", "applied": [],
                                "errors": {"k": "unknown_key"}})
    assert isinstance(e, Validation)
    assert e.errors == {"k": "unknown_key"}
    assert str(e) == "config_rejected"


def test_422_without_a_token_from_an_older_build_is_config_rejected_too():
    # Builds before the config_rejected token sent only applied/errors.
    e = error_from_status(422, {"applied": [], "errors": {"k": "unknown_key"}})
    assert isinstance(e, Validation) and str(e) == "config_rejected"
    assert e.errors == {"k": "unknown_key"}


def test_422_with_another_token_keeps_it():
    e = error_from_status(422, {"error": "bad_view"})
    assert str(e) == "bad_view" and e.errors == {}


def test_maps_409_and_404():
    assert isinstance(error_from_status(409, {"error": "already_slicing"}), Conflict)
    assert isinstance(error_from_status(404, {"error": "not_found"}), NotFound)


# --- F7: distinguish resource-404 (unknown_object/unknown_preset) from route-404 ---

def test_404_with_resource_error_preserves_message_and_is_not_route_missing():
    e = error_from_status(404, {"error": "unknown_object"})
    assert isinstance(e, NotFound)
    assert str(e) == "unknown_object"
    assert e.route_missing is False


def test_404_route_level_is_route_missing():
    # dispatch fallback body {"error":"not_found"} and empty body both mean
    # "this endpoint doesn't exist on this build"
    for body in ({"error": "not_found"}, {}):
        e = error_from_status(404, body)
        assert isinstance(e, NotFound)
        assert e.route_missing is True


def test_404_file_not_found_is_not_route_missing():
    # POST /model answers this for a path that does not exist; the route is there.
    e = error_from_status(404, {"error": "file_not_found", "path": "C:/x.stl"})
    assert isinstance(e, NotFound)
    assert str(e) == "file_not_found"
    assert e.route_missing is False
