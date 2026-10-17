# FreeLLMAPI vendor snapshot

This directory contains the MIT-licensed FreeLLMAPI source from:
https://github.com/tashfeenahmed/freellmapi

Upstream commit: `3a9ee5d8c58666fb60648949b53e345b0150593d`

Render-specific changes bootstrap the gateway key, provider keys, and first admin from environment variables; accept Render-generated Base64 encryption keys; and keep the dashboard generator in build-only dependencies. `package-lock.json` includes compatible dependency security updates.
