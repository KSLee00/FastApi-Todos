def pytest_configure(config):
    # @since("4.0.0") 마커 등록 — 등록하지 않으면 PytestUnknownMarkWarning 이 뜬다
    config.addinivalue_line("markers", "since(version): 해당 버전부터 있는 기능")
