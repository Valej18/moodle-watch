# Deploying behind a reverse proxy

The server binds `127.0.0.1` and nothing else. Exposure is held by the proxy and
the firewall, never by the bind. Templates are in [`deploy/`](../deploy/).

```
client
  └─ HTTPS   your.host/<secret segment>/mcp
       └─ reverse proxy — access_log off, force SSL
            └─ private tunnel
                 └─ socket unit (FreeBind) :3040
                      └─ systemd-socket-proxyd → 127.0.0.1:3041
                           └─ moodle-watch-server --http
```

## The five things that actually bite

**1. The `Host` header, or a silent `421`.** The MCP SDK validates `Host` to
prevent DNS rebinding. Behind a proxy that header carries the public name, so it
must be listed in `MOODLE_WATCH_HOSTS` or every request is rejected. Both the
bare name and `name:port` are registered for you; the proxy must set
`proxy_set_header Host <that name>`.

**2. Three lines or streamable HTTP does not work.** `proxy_http_version 1.1`,
`proxy_buffering off`, `proxy_read_timeout 3600s`. Missing any of them gives
connections that open and then hang.

**3. Do not include a generic proxy snippet in the location block.** It adds a
second `proxy_pass`, which fails the *entire* web-server configuration,
certificates included, not just this host.

**4. `access_log off` on the secret location.** Without it the secret URL
segment sits in plain text in the access log for months.

**5. A path with a space in `Environment=` is cut at the first blank** and read
as two assignments. Use `EnvironmentFile`, or a mirror path without spaces.
`systemd-analyze verify` catches it, if you run it.

## What actually guards this

Two things, and it is worth being precise because a third would be decorative.
The **firewall** means only your proxy can reach the port at all, and the
**secret URL segment** means only holders of the full URL can speak to the
endpoint. moodle-watch validates no shared header, so do not inject one: a guard
nobody checks looks like protection without being any.

The segment is therefore a password. Treat it as one.

## Firewall

If your host's firewall policy is default-permissive, opening a port needs both
an `allow from <the proxy>` rule and a `deny` for everything else. A bind on a
tunnel address is not a firewall on its own, and container runtimes routinely
bypass host firewall rules.

## Checking it

```bash
moodle-watch doctor             # layers 1 to 3, from the server's own machine
moodle-watch doctor --remote    # and the public door, from elsewhere
```

Each layer is tested from the machine that can legitimately reach it. A timeout
from the wrong machine is the firewall working, not a fault.

Reading the codes on layer 4: `405` or `406` on a `GET` is correct, the endpoint
wants a `POST`. `404` means a wrong segment or a missing proxy host. `421` means the public name is not in
`MOODLE_WATCH_HOSTS`.

## Revoking access

The secret URL segment **is** the password, so revoking means changing it in
every place at once: the environment file on the server, the proxy host's
configuration, and any client that holds the URL. Write it in none of your
notes.
