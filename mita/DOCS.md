# Mita (mieru server)

This Home Assistant app runs the `mita` server from the mieru project.

## Configuration

Add one or more users in the app configuration:

```yaml
users:
  - username: gleb
    password: "use-a-long-random-password"
log_level: INFO
prefer_ipv4: true
allow_private_ip: false
allow_loopback_ip: false
```

Set `allow_private_ip: true` only if you also want clients to reach private/LAN IPs through Mita.

The container always listens on `2022/TCP`. Change the **host port** from the app's **Network** section in Home Assistant if you want another LAN port.

## Router

Forward a TCP port from your router to the Home Assistant host port.

Example:

```text
Internet TCP 8443 -> Home Assistant IP TCP 2022
```

If you change the Home Assistant host port to `8443`, use:

```text
Internet TCP 8443 -> Home Assistant IP TCP 8443
```

Do not forward TCP 443 to this app if TCP 443 is already used by Caddy on the same public IP.

## Client

Use the same public hostname/IP, public TCP port, username and password in a mieru-compatible client such as ClashMi.
