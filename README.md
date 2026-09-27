# Mita Home Assistant Add-on

Home Assistant app/add-on repository for **mita**, the server component of the [mieru](https://github.com/enfein/mieru) proxy protocol.

## Install

1. In Home Assistant open **Settings → Apps/Add-ons → App/Add-on Store → Repositories**.
2. Add this repository URL.
3. Install **Mita (mieru server)**.
4. Replace the default username and password before starting.
5. Configure the host TCP port in the app's **Network** section if needed.
6. Forward that TCP port on your router to the Home Assistant host.

The image downloads the official mita 3.38.0 package from the upstream mieru release during build.
