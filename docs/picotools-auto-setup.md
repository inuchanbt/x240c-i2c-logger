# PicoXTools automatic I2C Sniffer setup

Starting with logger v0.4.5, `--picotools` configures I2C Sniffer before opening the capture WebSocket. `--no-picotools-setup` preserves the previous behavior for manually configured devices.

## Protocol evidence

The setup request was identified in the web UI JavaScript embedded in the [official PicoXTools 3.3.1 RP2350 firmware archive](https://www.cnsee.net/dl/PicoXTools_rp2350.uf2_3.3.1.zip), linked from the [official changelog](https://www.cnsee.net/guide/changelog.html). The archive was inspected locally without installing firmware. No vendor code or firmware is included in this repository.

The web UI's `config_i2c_addr` function sends a JSON body via HTTP POST to `/api/setup?type=i2c`. Its mode selector defines Master as `0`, Slave as `1`, and Sniffer as `2`. Selecting Sniffer fixes the pin selection to SCL GPIO9 / SDA GPIO8, consistent with the [official Sniffer guide](https://www.cnsee.net/guide/i2c/i2c_sniffer.html).

The logger sends:

```http
POST /api/setup?type=i2c
Content-Type: text/plain;charset=UTF-8

{"clk_pin": 9, "sda_pin": 8, "clock": 100000, "i2c_type": 2, "slave_addr": 49}
```

`clock` and `slave_addr` mirror the web UI's default values; they do not switch the logger to Master or Slave mode. HTTP proxy settings are bypassed for the USB-connected device. Setup uses a minimum timeout of five seconds, independently of the default one-second WebSocket receive timeout.

The response must be a JSON object containing a nonnegative numeric `result`. Negative values, missing or invalid results, HTTP errors, and malformed JSON abort startup. The UI also reads a `sniffer` field: `1` indicates that SPI Sniffer was stopped. The logger reports that condition.

After setup succeeds, the logger connects to `/ws/i2c` (or the explicitly supplied WebSocket URL). For `wss://` input, the setup request and WebSocket origin use HTTPS. On exit, it closes its WebSocket and leaves the device's Sniffer configuration enabled.

## Verification

Automated tests use a local HTTP server and a simulated WebSocket to verify the exact setup request, ordering before capture, binary decoding, failure handling, and opt-out behavior. Existing v0.4.4 SET regression tests also apply.

At implementation time, the user's device at `192.168.33.1` was unreachable. The startup API is supported by the inspected 3.3.1 web UI, but capture on the user's installed firmware has not yet been verified. To verify, start with Sniffer disabled in the web UI, run `py x240c_i2c_logger.py --picotools`, generate normal target I2C traffic, and confirm that the transaction CSV contains captured rows.
