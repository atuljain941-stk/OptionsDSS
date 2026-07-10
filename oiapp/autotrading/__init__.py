"""Auto-trading tools.

The Schwab EOD module is intentionally separate from the scanner UI.  It
builds broker-resident entry + OCO plans so the app does not have to stream
prices for every symbol throughout the trading day.
"""
