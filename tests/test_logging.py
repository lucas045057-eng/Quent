import logging

from quant_phase1.logging import SecretRedactingFilter, configure_logging, redact_secrets


def test_redact_secrets_removes_api_credentials():
    message = "apiKey=abc secretKey=def passphrase=ghi token=jkl"
    redacted = redact_secrets(message)
    assert "abc" not in redacted
    assert "def" not in redacted
    assert "ghi" not in redacted
    assert "jkl" not in redacted


def test_redacting_filter_preserves_formatted_runtime_errors(caplog):
    logger = logging.getLogger("quant_phase1.test_logging")
    redacting_filter = SecretRedactingFilter()
    logger.addFilter(redacting_filter)
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            logger.warning("canonical input unavailable reason=%s", "database refused")
    finally:
        logger.removeFilter(redacting_filter)
    assert "database refused" in caplog.text


def test_configure_logging_emits_structured_context(caplog):
    configure_logging()
    with caplog.at_level(logging.INFO, logger="quant_phase1"):
        logging.getLogger("quant_phase1").info("collector_ready", extra={"symbol": "BTCUSDT"})
    assert "collector_ready" in caplog.text
    assert "BTCUSDT" in caplog.text
