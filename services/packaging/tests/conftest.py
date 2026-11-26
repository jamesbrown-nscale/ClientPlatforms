"""Test-wide setup, read before any test file. Tokens the tests make last a day rather than the site's
two minutes: they're made when a test file loads, and a slow build (several services at once on the
VM) can take longer than two minutes to reach the last test."""
import os

os.environ.setdefault("CP_TOKEN_SECONDS", "86400")
