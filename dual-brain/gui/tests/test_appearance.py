from pathlib import Path

from gui.appearance import AppearanceSettings, load_appearance, save_appearance


def test_defaults_are_frosted_and_safe() -> None:
    settings = AppearanceSettings()
    assert settings.profile == "frosted"
    assert settings.dock_enabled is True
    assert settings.compositor_enabled is False


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "appearance.toml"
    expected = AppearanceSettings(profile="classic", overview_enabled=True)
    save_appearance(expected, path)
    assert load_appearance(path) == expected


def test_invalid_values_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "appearance.toml"
    path.write_text('profile = "unknown"\ncompositor_enabled = "yes"\n')
    assert load_appearance(path) == AppearanceSettings()
