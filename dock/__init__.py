"""Lean controller for the Ajazz AKP03E (Stream Dock) — no bloatware.

Drives the device directly over its vendor HID interface (VID 0x0300 / PID 0x3002).
Protocol reverse-engineered from 4ndv/mirajazz + 4ndv/opendeck-akp03 and verified
against real hardware.
"""

__version__ = "2.0.0"
