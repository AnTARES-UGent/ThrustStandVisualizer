import json
import os
import serial
import serial.tools.list_ports
import csv
from datetime import datetime

# Set this to the device name (e.g. ttyACM0) or full path (/dev/ttyACM0).
# If left empty, the script will try to auto-detect a candidate port.
PORT = "/dev/ttyACM0"
BAUD = 115200

# A port saved in config.json (set from the website's home page) wins over PORT above.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_port(port):
    cfg = load_config()
    cfg["port"] = (port or "").strip()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_port():
    """Port from config.json if set, otherwise the PORT constant."""
    return load_config().get("port", "").strip() or PORT


def resolve_port(port):
    # If user provided a full path, use it
    if not port:
        port = None

    if port and port.startswith("/"):
        return port

    # Try adding /dev/ prefix if a bare name was given
    if port:
        candidate = os.path.join("/dev", port)
        if os.path.exists(candidate):
            return candidate

    # Try to auto-detect common serial devices
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return None

    # Prefer devices with ACM or USB in the name
    for p in ports:
        if "ACM" in p.device or "USB" in p.device or "ttyACM" in p.device or "ttyUSB" in p.device:
            return p.device

    # Fallback to first available
    return ports[0].device


def parse_packet(line):
    """Return (time_since_start, force_N), or None."""
    try:
        time_text, force_text = line.split(";")

        time_since_start = float(time_text.strip().replace(",", "."))
        force = float(force_text.strip().replace(",", "."))

        return time_since_start, force

    except (ValueError, IndexError):
        return None


if __name__ == "__main__":
    resolved = resolve_port(get_port())

    if resolved is None:
        print("No serial ports found. Is your Arduino connected?")
        ports = list(serial.tools.list_ports.comports())
        if ports:
            print("Available ports:")
            for p in ports:
                print(f"  {p.device} - {p.description}")
        else:
            print("  (none)")
        raise SystemExit(1)

    print(f"Using serial port: {resolved}")
    ser = serial.Serial(resolved, BAUD, timeout=1)

    file = None
    writer = None

    try:
        print(f"Listening on {resolved}...")

        while True:
            line = ser.readline().decode("utf-8", errors="ignore").strip()

            if not line:
                continue

            print(line)

            packet = parse_packet(line)

            if packet is None:
                print(f"Could not parse: {line}")
                continue

            time_since_start, force = packet

            # First valid packet → create filename
            if file is None:
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")[:-3]

                filename = f"arduino_data_{timestamp}.csv"

                file = open(filename, "w", newline="")
                writer = csv.writer(file)

                writer.writerow(["time_since_start_s", "force_N"])

                print(f"Recording to {filename}")

            # Save only time since start + force
            writer.writerow([
                f"{time_since_start:.3f}",
                f"{force:.4f}"
            ])

            file.flush()

            print(
                f"t = {time_since_start:.3f} s    "
                f"F = {force:.4f} N"
            )

    except KeyboardInterrupt:
        print("\nStopped.")

    finally:
        ser.close()

        if file is not None:
            file.close()