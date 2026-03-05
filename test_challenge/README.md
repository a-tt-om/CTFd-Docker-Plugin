# Test Challenge — Multi-Container RCE + Pivot

## Architecture

```
Player
  │
  ▼ HTTP
┌─────────────────────────────────┐
│  web  (port 5000, exposed)      │
│  Flask "Network Diagnostic"     │
│  /ping?host=<INJECTABLE>        │
└────────────────┬────────────────┘
                 │ private Docker bridge
                 ▼
┌─────────────────────────────────┐
│  target  (port 80, internal)    │
│  Python HTTP server             │
│  GET /flag.txt → CTF{...}       │
└─────────────────────────────────┘
```

## Build Images

```bash
cd test_challenge
docker build -t ctf-test-web:latest ./web
docker build -t ctf-test-target:latest ./target
```

## Local Test (docker-compose)

```bash
docker-compose up --build
# Open http://localhost:5000
```

## Exploit Path

1. Visit the web app and notice the `/ping` endpoint.
2. Inject a command after the host:
   ```
   /ping?host=; curl http://target/flag.txt
   ```
3. The output shows the flag from the internal container.

## CTFd Challenge Setup

1. Build both images on the Docker host:
   ```bash
   docker build -t ctf-test-web:latest ./web
   docker build -t ctf-test-target:latest ./target
   ```

2. In CTFd Admin → Challenges → New Challenge:
   - **Type**: Container
   - **Name**: Internal Network Pivot
   - **Connection type**: http
   - **Compose Config**: paste contents of `compose_config.yaml`

3. Flag: set a static flag (the `FLAG` env var is injected into the *entry* container,
   but for this demo the target has a hardcoded flag in `/srv/flag.txt`).
   You can also set the target's flag dynamically — see notes below.

## Dynamic Flag (optional)

To make the target serve the dynamically generated flag instead of a hardcoded one,
you'd need a small init script in the target that reads `FLAG` from a shared secret
or from a file mounted at runtime. The simplest approach for a CTF is to just put
the real flag in the web container's env and have a file on the target serve as
a pivot indicator only.
