import pytest


def pytest_addoption(parser):
    parser.addoption("--mcp-image", default="", help="built mcp image under test")


@pytest.fixture(scope="session")
def image(request) -> str:
    name = request.config.getoption("--mcp-image")
    if not name:
        pytest.fail("--mcp-image names the built mcp image under test")
    return name
