from __future__ import annotations

from dataclasses import replace

from app.restaurant_cli import (
    build_opentable_booking_url,
    choose_best_restaurant_result,
    ensure_restaurant_cli_state,
    extract_opentable_time_labels,
    normalize_restaurant_provider,
    restaurant_provider_sequence,
)
from app.settings import Settings
from app.workspace import Workspace


def test_build_opentable_booking_url_uses_restref_client_shape() -> None:
    url = build_opentable_booking_url(
        restaurant_id="1046758",
        date="2026-05-18",
        time="19:00",
        party_size=2,
    )
    assert url == (
        "https://www.opentable.com/restref/client"
        "?rid=1046758&restref=1046758&partysize=2&datetime=2026-05-18T19%3A00"
    )


def test_ensure_restaurant_cli_state_writes_resy_env_token_ref(tmp_path) -> None:
    workspace = Workspace(str(tmp_path))
    settings = replace(
        Settings(),
        restaurant_cli_timezone="America/New_York",
        resy_api_key_param="/resy/key",
        resy_auth_token_param="/resy/token",
    )
    secrets = {
        "/resy/key": "resy-public-key",
        "/resy/token": "resy-auth-token",
    }
    object.__setattr__(settings, "secret", lambda parameter_name: secrets.get(parameter_name, ""))

    config_path = ensure_restaurant_cli_state(settings, workspace)
    payload = config_path.read_text(encoding="utf-8")

    assert '"provider": "resy"' in payload
    assert '"apiKey": "resy-public-key"' in payload
    assert '"source": "env"' in payload
    assert '"id": "RESY_AUTH_TOKEN"' in payload


def test_normalize_restaurant_provider_defaults_to_resy() -> None:
    assert normalize_restaurant_provider("") == "resy"
    assert normalize_restaurant_provider("   ") == "resy"
    assert normalize_restaurant_provider("OpenTable") == "opentable"


def test_restaurant_provider_sequence_defaults_to_resy_then_opentable() -> None:
    assert restaurant_provider_sequence("") == ("resy", "opentable")
    assert restaurant_provider_sequence("resy") == ("resy", "opentable")
    assert restaurant_provider_sequence("opentable") == ("opentable", "resy")


def test_restaurant_provider_sequence_respects_explicit_only_and_auto() -> None:
    assert restaurant_provider_sequence("resy only") == ("resy",)
    assert restaurant_provider_sequence("opentable only") == ("opentable",)
    assert restaurant_provider_sequence("auto") == ("resy", "opentable")


def test_choose_best_restaurant_result_prefers_exact_bungalow_over_bowery_variant() -> None:
    results = [
        {"name": "Bowery Bungalow NYC", "city": "New York"},
        {"name": "Bungalow", "city": "New York"},
    ]
    best = choose_best_restaurant_result("bungalow the Indian restaurant", results)
    assert best is not None
    assert best["name"] == "Bungalow"


def test_extract_opentable_time_labels_filters_and_deduplicates_visible_buttons() -> None:
    labels = extract_opentable_time_labels(
        [
            "Find a table",
            "7:00 PM",
            "7:15 PM",
            "7:00 pm",
            "Standard",
            "Notify me",
        ]
    )
    assert labels == ["7:00 PM", "7:15 PM"]
