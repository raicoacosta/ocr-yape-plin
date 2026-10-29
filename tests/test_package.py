import importlib


def test_package_is_importable_from_src_layout() -> None:
    package = importlib.import_module("payments_bot")

    assert package.__name__ == "payments_bot"
