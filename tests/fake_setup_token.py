"""Stands in for `claude setup-token` in tests: Ink-like output, waits for the code, prints a token."""
import sys

w = sys.stdout.write
w("\x1b[?25l\x1b[1mWelcome\x1b[1Cto\x1b[1CClaude\x1b[1CCode\x1b[22m\r\n")
w("Browser didn't open? Use the url below to sign in\r\n\r\n")
w("\x1b[2mhttps://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz\x1b[22m\r\n\r\n")
w("Paste code here if prompted > ")
sys.stdout.flush()
code = sys.stdin.readline().strip()
if code == "good#state":
    w("\r\n\x1b[32m✓ Long-lived authentication token created successfully!\x1b[39m\r\n\r\n")
    w("Your OAuth token (valid for 1 year):\r\n\r\n\x1b[1msk-ant-oat01-FAKEtoken_abcdefghijklmnopqrstuvwxyz0123\x1b[22m\r\n")
else:
    w("\r\n*************\r\nOAuth error: Request failed with status code 400\r\nPress Enter to retry.\r\n")
    sys.stdout.flush()
    sys.stdin.readline()   # the real CLI keeps waiting here
sys.stdout.flush()
