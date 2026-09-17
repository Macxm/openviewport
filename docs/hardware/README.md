# Supported hardware

The viewport talks to cameras and NVRs through **source adapters**. Pick the most specific
one that fits: a native adapter knows a device's quirks, ONVIF is the standard fallback, and
plain RTSP always works if you can write the URL.

| Adapter | `type` | Use it for | Gives you |
|---|---|---|---|
| Reolink | `reolink` | Reolink NVRs, Home Hubs and cameras | Real camera names, the configured codec (H.265 included), HTTP-FLV where it helps, and **detection** (person / vehicle / animal) |
| ONVIF | `onvif` | Most other brands: Hikvision, Dahua, Amcrest, Axis, Uniview, Annke, Foscam… | Automatic discovery of channels, resolutions and stream URLs |
| Plain RTSP | `rtsp` | Anything else, or a stream from another system | Exactly the URLs you give it |

## Tested devices

| Device | Firmware | Adapter | Result |
|---|---|---|---|
| [Reolink NVS8](reolink-nvs8.md) | v3.4.0.318_24053118 | `reolink` | **Works.** 3 cameras, 4K H.265 main streams, 0 dropped frames |
| Reolink NVS8 | v3.4.0.318_24053118 | `onvif` | **Works**, with the caveats below |

Tried something else? A note in `docs/hardware/` with the model, firmware and anything
surprising is the most useful thing you can contribute.

## Choosing between a native adapter and ONVIF

On the same NVS8, ONVIF discovery was correct but poorer than the native adapter:

- **Camera names** came back as `Profile000_MainStream` rather than "Driveway": Reolink names
  its ONVIF profiles generically, and ONVIF has nowhere else to look.
- **Codec**: ONVIF advertised the `h264Preview_*` URLs and reported H.264, although the
  cameras are configured for H.265. The native adapter uses the configured encoding.
- **Empty NVR slots** are not listed over ONVIF at all — a small advantage over the native
  API, which reports every slot (the Reolink adapter filters them).
- **Detection** is not implemented over ONVIF yet; the native adapter has it.

So: use `reolink` for Reolink, and `onvif` elsewhere.

## Configuring a source

```yaml
sources:
  - id: nvr                 # ONVIF: most brands
    type: onvif
    host: 192.0.2.10
    port: 8000              # the ONVIF service port, not the web UI's
    https: false            # ONVIF is usually plain HTTP on its own port
    username: viewer
    password: ${NVR_PASSWORD}

  - id: extra               # plain RTSP: anything you can write a URL for
    type: rtsp
    username: viewer        # added to URLs that have no credentials of their own
    password: ${NVR_PASSWORD}
    cameras_urls:
      - name: Front gate
        main: rtsp://192.0.2.40:554/stream1
        sub: rtsp://192.0.2.40:554/stream2
      - name: Workshop
        main: rtsp://192.0.2.41:554/h264
```

Several sources can run at once; camera ids are `<source id>:<channel>`.

## Gotchas that apply to any brand

- **Use a view-only account.** The agent only reads.
- **Sub streams matter.** The wall plays low-resolution streams everywhere except the large
  tile. A camera with no sub stream forces a full-resolution decode per tile.
- **ONVIF timestamps.** Devices reject a login whose timestamp is far from their own clock.
  The adapter reads the device's clock first and matches it, so a wrong clock is not fatal.
- **`https` is about the vendor's own API**, not ONVIF. An ONVIF service on port 8000 is
  normally plain HTTP even when the device's web UI is HTTPS-only.

## The device running the viewport

| | |
|---|---|
| Developed on | macOS on Apple Silicon with Docker Desktop |
| Target | Raspberry Pi 5 (H.265 in hardware, no H.264 hardware decoder) |
| Needs | Docker, a wired network to the NVR if you can, and a screen |
