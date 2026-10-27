import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # Expose each phase's report on the item so the page fixture can screenshot failures.
    outcome = yield
    setattr(item, f"rep_{call.when}", outcome.get_result())
